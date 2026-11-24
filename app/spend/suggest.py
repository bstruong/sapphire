"""Bulk spend-label suggestions: stage → Accept / Reject (B2 + Vault lane).

Suggest-only writes: nothing lands in ``transaction_spend_labels`` until Accept.
Opaque payees (NULL) are ineligible. Vault is optional; downtime is loud.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol
from uuid import UUID, uuid4

import psycopg

from app.models.exceptions import InferenceError, TokenError
from app.spend.categories import LabelSource, set_spend_label
from app.spend.rules import match_payee_rule, normalize_payee

logger = logging.getLogger("app.spend.suggest")


class VaultClientProtocol(Protocol):
    def is_available(self) -> bool: ...

    def extract_structured(
        self, text: str, schema_description: str
    ) -> dict[str, Any]: ...


def default_vault_client() -> VaultClientProtocol:
    """Construct the production Vault client (tests monkeypatch this name)."""
    from app.core.inference import VaultInferenceClient as _Client

    return _Client()


# Alias kept for HTMX tests that monkeypatch ``VaultInferenceClient``.
VaultInferenceClient = default_vault_client


@dataclass(frozen=True, slots=True)
class SuggestionGroup:
    """One payee cluster in the review queue."""

    group_key: str
    payee_display: str
    proposed_slug: str
    display_name: str
    origin: str
    evidence: str
    txn_count: int
    abs_cents_total: int
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuggestBatch:
    """Result of Run suggest (staging only)."""

    batch_id: UUID
    pie_year: int | None
    pie_month: int | None
    unlabeled_count: int
    opaque_count: int
    suggestable_count: int
    vault_available: bool
    vault_skipped_reason: str | None
    statement_id: UUID | None = None
    rule_groups: list[SuggestionGroup] = field(default_factory=list)
    vault_groups: list[SuggestionGroup] = field(default_factory=list)


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    if not isinstance(year, int) or not isinstance(month, int):
        raise TokenError("pie_year and pie_month must be ints")
    if year < 1970 or year > 2100:
        raise TokenError(f"Invalid pie_year={year}")
    if month < 1 or month > 12:
        raise TokenError(f"Invalid pie_month={month}")
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def _require_batch(conn: psycopg.Connection, batch_id: UUID) -> None:
    if not isinstance(batch_id, UUID):
        raise TokenError("batch_id must be a UUID")
    row = conn.execute(
        "SELECT 1 FROM spend_label_suggestions WHERE batch_id = %s LIMIT 1",
        (str(batch_id),),
    ).fetchone()
    if row is None:
        raise TokenError(f"SUGGEST_BATCH_NOT_FOUND: batch_id={batch_id}")


def _group_rows(
    rows: list[tuple[Any, ...]],
) -> list[SuggestionGroup]:
    """Group staged suggestion rows by (origin, payee_normalized, proposed_slug)."""
    buckets: dict[tuple[str, str, str], list[tuple[Any, ...]]] = {}
    for row in rows:
        key = (str(row[3]), str(row[2]), str(row[4]))  # origin, norm, slug
        buckets.setdefault(key, []).append(row)
    groups: list[SuggestionGroup] = []
    for (origin, norm, slug), items in sorted(
        buckets.items(), key=lambda kv: (-sum(int(r[7]) for r in kv[1]), kv[0][1])
    ):
        txn_ids = tuple(str(r[0]) for r in items)
        abs_total = sum(int(r[7]) for r in items)
        display = str(items[0][1])
        evidence = str(items[0][5])
        display_name = str(items[0][6])
        group_key = f"{origin}:{norm}:{slug}"
        groups.append(
            SuggestionGroup(
                group_key=group_key,
                payee_display=display,
                proposed_slug=slug,
                display_name=display_name,
                origin=origin,
                evidence=evidence,
                txn_count=len(items),
                abs_cents_total=abs_total,
                transaction_ids=txn_ids,
            )
        )
    return groups


def _load_batch_groups(
    conn: psycopg.Connection, batch_id: UUID
) -> tuple[list[SuggestionGroup], list[SuggestionGroup]]:
    rows = conn.execute(
        """
        SELECT s.transaction_id, s.payee_display, s.payee_normalized,
               s.origin, s.proposed_slug, s.evidence, c.display_name,
               s.abs_amount_cents
        FROM spend_label_suggestions s
        JOIN spend_categories c ON c.slug = s.proposed_slug
        WHERE s.batch_id = %s
        ORDER BY s.origin, s.payee_normalized
        """,
        (str(batch_id),),
    ).fetchall()
    rule_rows = [r for r in rows if str(r[3]) == "rule"]
    vault_rows = [r for r in rows if str(r[3]) == "vault_suggest"]
    return _group_rows(rule_rows), _group_rows(vault_rows)


def month_scope_counts(
    conn: psycopg.Connection, *, year: int, month: int
) -> tuple[int, int, int]:
    start, end = _month_bounds(year, month)
    counts = conn.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE amount_cents < 0 AND label_missing) AS unlabeled,
          COUNT(*) FILTER (
            WHERE amount_cents < 0 AND label_missing AND payee IS NULL
          ) AS opaque,
          COUNT(*) FILTER (
            WHERE amount_cents < 0 AND label_missing AND payee IS NOT NULL
          ) AS suggestable
        FROM (
          SELECT t.amount_cents, t.payee,
                 (l.transaction_id IS NULL) AS label_missing
          FROM transactions t
          LEFT JOIN transaction_spend_labels l
            ON l.transaction_id = t.transaction_id
          WHERE t.post_date >= %s AND t.post_date < %s
        ) scoped
        """,
        (start, end),
    ).fetchone()
    if counts is None:
        return 0, 0, 0
    return int(counts[0]), int(counts[1]), int(counts[2])


def statement_scope_counts(
    conn: psycopg.Connection, *, statement_id: UUID
) -> tuple[int, int, int]:
    if not isinstance(statement_id, UUID):
        raise TokenError("statement_id must be a UUID")
    counts = conn.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE amount_cents < 0 AND label_missing) AS unlabeled,
          COUNT(*) FILTER (
            WHERE amount_cents < 0 AND label_missing AND payee IS NULL
          ) AS opaque,
          COUNT(*) FILTER (
            WHERE amount_cents < 0 AND label_missing AND payee IS NOT NULL
          ) AS suggestable
        FROM (
          SELECT t.amount_cents, t.payee,
                 (l.transaction_id IS NULL) AS label_missing
          FROM transactions t
          LEFT JOIN transaction_spend_labels l
            ON l.transaction_id = t.transaction_id
          WHERE t.statement_id = %s
        ) scoped
        """,
        (str(statement_id),),
    ).fetchone()
    if counts is None:
        return 0, 0, 0
    return int(counts[0]), int(counts[1]), int(counts[2])


def _require_statement(conn: psycopg.Connection, statement_id: UUID) -> tuple[int, int]:
    """Fail loud on unknown statement; return display year/month from end date."""
    if not isinstance(statement_id, UUID):
        raise TokenError("statement_id must be a UUID")
    row = conn.execute(
        """
        SELECT statement_end_date
        FROM statements
        WHERE statement_id = %s
        """,
        (str(statement_id),),
    ).fetchone()
    if row is None:
        raise TokenError(f"STATEMENT_NOT_FOUND: statement_id={statement_id}")
    end_date = row[0]
    if not isinstance(end_date, date):
        raise TokenError(f"STATEMENT_END_DATE_INVALID: statement_id={statement_id}")
    return int(end_date.year), int(end_date.month)


def load_batch(
    conn: psycopg.Connection,
    *,
    batch_id: UUID,
    year: int | None = None,
    month: int | None = None,
    statement_id: UUID | None = None,
    vault_available: bool,
    vault_skipped_reason: str | None,
) -> SuggestBatch:
    """Rebuild a SuggestBatch view for an existing batch_id."""
    if not isinstance(batch_id, UUID):
        raise TokenError("batch_id must be a UUID")
    # Q4: prefer statement when dual scope is present.
    if statement_id is not None:
        display_year, display_month = _require_statement(conn, statement_id)
        unlabeled, opaque, suggestable = statement_scope_counts(
            conn, statement_id=statement_id
        )
        rule_groups, vault_groups = _load_batch_groups(conn, batch_id)
        return SuggestBatch(
            batch_id=batch_id,
            pie_year=display_year,
            pie_month=display_month,
            unlabeled_count=unlabeled,
            opaque_count=opaque,
            suggestable_count=suggestable,
            vault_available=vault_available,
            vault_skipped_reason=vault_skipped_reason,
            statement_id=statement_id,
            rule_groups=rule_groups,
            vault_groups=vault_groups,
        )
    if year is None or month is None:
        raise TokenError(
            "SUGGEST_SCOPE_REQUIRED: provide statement_id or pie_year/month"
        )
    unlabeled, opaque, suggestable = month_scope_counts(conn, year=year, month=month)
    rule_groups, vault_groups = _load_batch_groups(conn, batch_id)
    return SuggestBatch(
        batch_id=batch_id,
        pie_year=year,
        pie_month=month,
        unlabeled_count=unlabeled,
        opaque_count=opaque,
        suggestable_count=suggestable,
        vault_available=vault_available,
        vault_skipped_reason=vault_skipped_reason,
        statement_id=None,
        rule_groups=rule_groups,
        vault_groups=vault_groups,
    )


def load_latest_batch(
    conn: psycopg.Connection,
    *,
    year: int | None = None,
    month: int | None = None,
    statement_id: UUID | None = None,
) -> SuggestBatch | None:
    """Load the newest staged batch for a scope, if any."""
    # Q4: prefer statement when dual scope is present.
    if statement_id is not None:
        _require_statement(conn, statement_id)
        row = conn.execute(
            """
            SELECT batch_id
            FROM spend_label_suggestions
            WHERE statement_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(statement_id),),
        ).fetchone()
        if row is None:
            return None
        batch_id = UUID(str(row[0]))
        _rule_groups, vault_groups = _load_batch_groups(conn, batch_id)
        vault_available = bool(vault_groups)
        reason = None if vault_available else "no_vault_groups_or_prior_cleared"
        return load_batch(
            conn,
            batch_id=batch_id,
            statement_id=statement_id,
            vault_available=vault_available,
            vault_skipped_reason=reason,
        )
    if year is None or month is None:
        raise TokenError(
            "SUGGEST_SCOPE_REQUIRED: provide statement_id or pie_year/month"
        )
    _month_bounds(year, month)
    row = conn.execute(
        """
        SELECT batch_id
        FROM spend_label_suggestions
        WHERE pie_year = %s AND pie_month = %s AND statement_id IS NULL
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (year, month),
    ).fetchone()
    if row is None:
        return None
    batch_id = UUID(str(row[0]))
    _rule_groups, vault_groups = _load_batch_groups(conn, batch_id)
    # Without a side table we cannot know prior JARVIS state; infer from staged Vault.
    vault_available = bool(vault_groups)
    reason = None if vault_available else "no_vault_groups_or_prior_cleared"
    return load_batch(
        conn,
        batch_id=batch_id,
        year=year,
        month=month,
        vault_available=vault_available,
        vault_skipped_reason=reason,
    )


def _closed_slug_schema(conn: psycopg.Connection) -> tuple[str, frozenset[str]]:
    rows = conn.execute("SELECT slug FROM spend_categories ORDER BY slug").fetchall()
    slugs = frozenset(str(r[0]) for r in rows)
    if not slugs:
        raise TokenError("SPEND_CATEGORY_EMPTY: no spend_categories seeded")
    enum_list = ", ".join(sorted(slugs))
    schema = (
        'Return a JSON object with key "slug" set to exactly one of: '
        f"{enum_list}. "
        'If the payee is insufficient to choose, return {"abstain": true}. '
        'Optional key "rationale" is a short non-PII reason string.'
    )
    return schema, slugs


def _insert_suggestion(
    conn: psycopg.Connection,
    *,
    batch_id: UUID,
    txn_id: Any,
    payee: str,
    payee_normalized: str,
    slug: str,
    origin: str,
    evidence: str,
    abs_cents: int,
    year: int | None,
    month: int | None,
    statement_id: UUID | None,
) -> None:
    conn.execute(
        """
        INSERT INTO spend_label_suggestions (
            suggestion_id, batch_id, transaction_id, payee_display,
            payee_normalized, proposed_slug, origin, evidence,
            pie_year, pie_month, abs_amount_cents, statement_id
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            str(uuid4()),
            str(batch_id),
            str(txn_id),
            payee,
            payee_normalized,
            slug,
            origin,
            evidence,
            year,
            month,
            abs_cents,
            str(statement_id) if statement_id is not None else None,
        ),
    )


def run_suggest(
    conn: psycopg.Connection,
    *,
    year: int | None = None,
    month: int | None = None,
    statement_id: UUID | None = None,
    vault_client: VaultClientProtocol | None = None,
) -> SuggestBatch:
    """Compute suggestions for unlabeled outflows in a scope.

    Scope rules (locked Q4/Q5):
    - ``statement_id`` present → prefer statement (ignore month).
    - month-only → calendar month with ``statement_id IS NULL`` staging.
    - neither → fail loud.

    Idempotent recompute: clears prior staging for the active scope, then
    inserts a new batch. Does not write ``transaction_spend_labels``.
    """
    client = vault_client if vault_client is not None else VaultInferenceClient()

    scope_statement_id: UUID | None = None
    scope_year: int | None = None
    scope_month: int | None = None

    # Q4: prefer statement when both scopes are sent.
    if statement_id is not None:
        scope_statement_id = statement_id
        scope_year, scope_month = _require_statement(conn, statement_id)
        conn.execute(
            """
            DELETE FROM spend_label_suggestions
            WHERE statement_id = %s
            """,
            (str(statement_id),),
        )
        rows = conn.execute(
            """
            SELECT t.transaction_id, t.payee, t.amount_cents
            FROM transactions t
            LEFT JOIN transaction_spend_labels l
              ON l.transaction_id = t.transaction_id
            WHERE t.amount_cents < 0
              AND t.statement_id = %s
              AND l.transaction_id IS NULL
            ORDER BY t.post_date, t.transaction_id
            """,
            (str(statement_id),),
        ).fetchall()
    else:
        if year is None or month is None:
            raise TokenError(
                "SUGGEST_SCOPE_REQUIRED: provide statement_id or pie_year/month"
            )
        start, end = _month_bounds(year, month)
        scope_year, scope_month = year, month
        conn.execute(
            """
            DELETE FROM spend_label_suggestions
            WHERE pie_year = %s AND pie_month = %s AND statement_id IS NULL
            """,
            (year, month),
        )
        rows = conn.execute(
            """
            SELECT t.transaction_id, t.payee, t.amount_cents
            FROM transactions t
            LEFT JOIN transaction_spend_labels l
              ON l.transaction_id = t.transaction_id
            WHERE t.amount_cents < 0
              AND t.post_date >= %s
              AND t.post_date < %s
              AND l.transaction_id IS NULL
            ORDER BY t.post_date, t.transaction_id
            """,
            (start, end),
        ).fetchall()

    unlabeled = len(rows)
    opaque_rows = [r for r in rows if r[1] is None]
    suggestable = [r for r in rows if r[1] is not None]
    opaque_count = len(opaque_rows)
    suggestable_count = len(suggestable)

    batch_id = uuid4()
    schema, closed_slugs = _closed_slug_schema(conn)
    vault_available = False
    vault_skipped_reason: str | None = None
    residual: list[tuple[Any, ...]] = []

    staged = 0
    for txn_id, payee, amount_cents in suggestable:
        abs_cents = abs(int(amount_cents))
        if abs_cents <= 0:
            raise TokenError(f"Invalid outflow abs cents for transaction_id={txn_id}")
        hit = match_payee_rule(conn, str(payee))
        if hit is None:
            residual.append((txn_id, payee, abs_cents))
            continue
        _insert_suggestion(
            conn,
            batch_id=batch_id,
            txn_id=txn_id,
            payee=str(payee),
            payee_normalized=hit.payee_normalized,
            slug=hit.slug,
            origin="rule",
            evidence=f"exact map · {hit.slug}",
            abs_cents=abs_cents,
            year=scope_year,
            month=scope_month,
            statement_id=scope_statement_id,
        )
        staged += 1

    try:
        vault_available = bool(client.is_available())
    except Exception as exc:  # noqa: BLE001 — treat probe failure as downtime
        vault_available = False
        vault_skipped_reason = f"is_available_error:{type(exc).__name__}"
        logger.warning(
            "Vault availability probe failed; skipping Vault lane",
            extra={"error_type": type(exc).__name__},
        )

    if not vault_available:
        if vault_skipped_reason is None:
            vault_skipped_reason = "jarvis_unavailable"
    else:
        # Group residual by normalized payee so one extract covers the cluster.
        by_payee: dict[str, list[tuple[Any, ...]]] = {}
        for txn_id, payee, abs_cents in residual:
            key = normalize_payee(str(payee))
            by_payee.setdefault(key, []).append((txn_id, payee, abs_cents))

        for items in by_payee.values():
            payee_display = str(items[0][1])
            try:
                parsed = client.extract_structured(
                    text=payee_display, schema_description=schema
                )
            except InferenceError as exc:
                logger.warning(
                    "Vault extract failed for residual payee cluster",
                    extra={
                        "error_type": type(exc).__name__,
                        "txn_count": len(items),
                    },
                )
                continue
            if parsed.get("abstain") is True:
                continue
            slug = parsed.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                logger.warning(
                    "Vault extract missing slug; skipping cluster",
                    extra={"txn_count": len(items)},
                )
                continue
            slug_clean = slug.strip()
            if slug_clean not in closed_slugs:
                logger.warning(
                    "Vault extract unknown slug; skipping cluster",
                    extra={"txn_count": len(items)},
                )
                continue
            rationale = parsed.get("rationale")
            evidence = (
                f"Vault closed-set map · {rationale}"
                if isinstance(rationale, str) and rationale.strip()
                else "Vault closed-set map"
            )
            for txn_id, payee, abs_cents in items:
                _insert_suggestion(
                    conn,
                    batch_id=batch_id,
                    txn_id=txn_id,
                    payee=str(payee),
                    payee_normalized=normalize_payee(str(payee)),
                    slug=slug_clean,
                    origin="vault_suggest",
                    evidence=evidence,
                    abs_cents=int(abs_cents),
                    year=scope_year,
                    month=scope_month,
                    statement_id=scope_statement_id,
                )
                staged += 1

    rule_groups, vault_groups = _load_batch_groups(conn, batch_id)
    logger.info(
        "Spend label suggest batch staged",
        extra={
            "batch_id": str(batch_id),
            "year": scope_year,
            "month": scope_month,
            "statement_id": (
                str(scope_statement_id) if scope_statement_id is not None else None
            ),
            "unlabeled_count": unlabeled,
            "opaque_count": opaque_count,
            "suggestable_count": suggestable_count,
            "staged_count": staged,
            "vault_available": vault_available,
            "rule_group_count": len(rule_groups),
            "vault_group_count": len(vault_groups),
        },
    )
    return SuggestBatch(
        batch_id=batch_id,
        pie_year=scope_year,
        pie_month=scope_month,
        unlabeled_count=unlabeled,
        opaque_count=opaque_count,
        suggestable_count=suggestable_count,
        vault_available=vault_available,
        vault_skipped_reason=vault_skipped_reason,
        statement_id=scope_statement_id,
        rule_groups=rule_groups,
        vault_groups=vault_groups,
    )


def _source_for_origin(origin: str) -> LabelSource:
    if origin == "rule":
        return "rule"
    if origin == "vault_suggest":
        return "vault_suggest_accepted"
    raise TokenError(f"Invalid suggestion origin {origin!r}")


def require_suggestable_transaction(
    conn: psycopg.Connection, transaction_id: UUID
) -> None:
    """Fail loud when a txn is missing or opaque (ineligible for suggest Accept)."""
    if not isinstance(transaction_id, UUID):
        raise TokenError("transaction_id must be a UUID")
    payee_row = conn.execute(
        "SELECT payee FROM transactions WHERE transaction_id = %s",
        (str(transaction_id),),
    ).fetchone()
    if payee_row is None:
        raise TokenError(f"TRANSACTION_NOT_FOUND: transaction_id={transaction_id}")
    if payee_row[0] is None:
        raise TokenError(f"OPAQUE_PAYEE_INELIGIBLE: transaction_id={transaction_id}")


def accept_suggestion_group(
    conn: psycopg.Connection,
    *,
    batch_id: UUID,
    group_key: str,
    slug: str,
) -> int:
    """Accept a staged group. Writes labels; clears staging rows."""
    _require_batch(conn, batch_id)
    if not isinstance(group_key, str) or not group_key.strip():
        raise TokenError("group_key required")

    parts = group_key.split(":", 2)
    if len(parts) != 3:
        raise TokenError(f"Invalid group_key {group_key!r}")
    origin, payee_normalized, _proposed = parts
    if origin not in ("rule", "vault_suggest"):
        raise TokenError(f"Invalid suggestion origin {origin!r}")

    staged = conn.execute(
        """
        SELECT suggestion_id, transaction_id, origin
        FROM spend_label_suggestions
        WHERE batch_id = %s
          AND origin = %s
          AND payee_normalized = %s
        """,
        (str(batch_id), origin, payee_normalized),
    ).fetchall()
    if not staged:
        raise TokenError(
            f"SUGGEST_GROUP_NOT_FOUND: batch_id={batch_id} group_key={group_key}"
        )

    source = _source_for_origin(origin)
    written = 0
    for _sid, txn_id, _origin in staged:
        require_suggestable_transaction(conn, UUID(str(txn_id)))
        set_spend_label(
            conn,
            transaction_id=UUID(str(txn_id)),
            slug=slug,
            source=source,
        )
        written += 1

    conn.execute(
        """
        DELETE FROM spend_label_suggestions
        WHERE batch_id = %s
          AND origin = %s
          AND payee_normalized = %s
        """,
        (str(batch_id), origin, payee_normalized),
    )
    logger.info(
        "Suggestion group accepted",
        extra={
            "batch_id": str(batch_id),
            "origin": origin,
            "spend_category_slug": slug,
            "written_count": written,
            "source": source,
        },
    )
    return written


def reject_suggestion_group(
    conn: psycopg.Connection,
    *,
    batch_id: UUID,
    group_key: str,
) -> int:
    """Drop a staged group without writing labels."""
    _require_batch(conn, batch_id)
    parts = group_key.split(":", 2)
    if len(parts) != 3:
        raise TokenError(f"Invalid group_key {group_key!r}")
    origin, payee_normalized, _slug = parts
    result = conn.execute(
        """
        DELETE FROM spend_label_suggestions
        WHERE batch_id = %s
          AND origin = %s
          AND payee_normalized = %s
        """,
        (str(batch_id), origin, payee_normalized),
    )
    dropped = result.rowcount if result.rowcount is not None else 0
    if dropped == 0:
        raise TokenError(
            f"SUGGEST_GROUP_NOT_FOUND: batch_id={batch_id} group_key={group_key}"
        )
    logger.info(
        "Suggestion group rejected",
        extra={
            "batch_id": str(batch_id),
            "origin": origin,
            "dropped_count": dropped,
        },
    )
    return int(dropped)


def accept_all_rule_suggestions(conn: psycopg.Connection, *, batch_id: UUID) -> int:
    """Accept every staged rule group. Never accepts Vault without scanning."""
    _require_batch(conn, batch_id)
    rule_groups, _vault = _load_batch_groups(conn, batch_id)
    total = 0
    for group in rule_groups:
        total += accept_suggestion_group(
            conn,
            batch_id=batch_id,
            group_key=group.group_key,
            slug=group.proposed_slug,
        )
    return total


def reject_all_vault_suggestions(conn: psycopg.Connection, *, batch_id: UUID) -> int:
    """Drop all Vault-staged suggestions for the batch."""
    _require_batch(conn, batch_id)
    result = conn.execute(
        """
        DELETE FROM spend_label_suggestions
        WHERE batch_id = %s AND origin = 'vault_suggest'
        """,
        (str(batch_id),),
    )
    dropped = int(result.rowcount or 0)
    logger.info(
        "All Vault suggestions rejected",
        extra={"batch_id": str(batch_id), "dropped_count": dropped},
    )
    return dropped
