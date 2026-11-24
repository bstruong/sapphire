"""Deterministic payee → spend-category rules (bulk suggest B1).

Exact match on normalized payee (lower + collapsed whitespace). Ambiguous
duplicate patterns fail loud on insert. Never invents payees.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg

from app.models.exceptions import TokenError

logger = logging.getLogger("app.spend.rules")

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SpendLabelRule:
    """One enabled or disabled payee_pattern → category mapping."""

    rule_id: UUID
    payee_pattern: str
    spend_category_id: UUID
    slug: str
    display_name: str
    enabled: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """Successful exact rule hit for a payee."""

    rule_id: UUID
    spend_category_id: UUID
    slug: str
    display_name: str
    payee_normalized: str
    evidence: str


def normalize_payee(payee: str) -> str:
    """Lowercase + collapse whitespace. Fail loud on empty / non-str."""
    if not isinstance(payee, str):
        raise TokenError("PAYEE_REQUIRED: payee must be a non-empty str")
    stripped = payee.strip()
    if not stripped:
        raise TokenError("PAYEE_REQUIRED: payee must be a non-empty str")
    return _WHITESPACE.sub(" ", stripped).lower()


def insert_spend_label_rule(
    conn: psycopg.Connection,
    *,
    payee_pattern: str,
    spend_category_id: UUID,
    enabled: bool = True,
) -> SpendLabelRule:
    """Insert a rule. Duplicate normalized pattern → SPEND_LABEL_RULE_AMBIGUOUS."""
    if not isinstance(spend_category_id, UUID):
        raise TokenError("spend_category_id must be a UUID")
    pattern = normalize_payee(payee_pattern)
    cat = conn.execute(
        """
        SELECT spend_category_id, slug, display_name
        FROM spend_categories
        WHERE spend_category_id = %s
        """,
        (str(spend_category_id),),
    ).fetchone()
    if cat is None:
        raise TokenError(
            f"SPEND_CATEGORY_UNKNOWN: spend_category_id={spend_category_id}"
        )

    existing = conn.execute(
        "SELECT rule_id FROM spend_label_rules WHERE payee_pattern = %s",
        (pattern,),
    ).fetchone()
    if existing is not None:
        raise TokenError(
            "SPEND_LABEL_RULE_AMBIGUOUS: "
            f"payee_pattern already mapped (rule_id={existing[0]})"
        )

    rule_id = uuid4()
    created_at = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO spend_label_rules (
            rule_id, payee_pattern, spend_category_id, enabled, created_at
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            str(rule_id),
            pattern,
            str(spend_category_id),
            bool(enabled),
            created_at,
        ),
    )
    logger.info(
        "Spend label rule inserted",
        extra={
            "rule_id": str(rule_id),
            "spend_category_slug": str(cat[1]),
            "enabled": bool(enabled),
        },
    )
    return SpendLabelRule(
        rule_id=rule_id,
        payee_pattern=pattern,
        spend_category_id=UUID(str(cat[0])),
        slug=str(cat[1]),
        display_name=str(cat[2]),
        enabled=bool(enabled),
        created_at=created_at,
    )


def list_enabled_rules(conn: psycopg.Connection) -> list[SpendLabelRule]:
    """Return enabled rules ordered by pattern."""
    rows = conn.execute(
        """
        SELECT r.rule_id, r.payee_pattern, r.spend_category_id, c.slug,
               c.display_name, r.enabled, r.created_at
        FROM spend_label_rules r
        JOIN spend_categories c ON c.spend_category_id = r.spend_category_id
        WHERE r.enabled IS TRUE
        ORDER BY r.payee_pattern
        """
    ).fetchall()
    return [
        SpendLabelRule(
            rule_id=UUID(str(row[0])),
            payee_pattern=str(row[1]),
            spend_category_id=UUID(str(row[2])),
            slug=str(row[3]),
            display_name=str(row[4]),
            enabled=bool(row[5]),
            created_at=row[6],
        )
        for row in rows
    ]


def match_payee_rule(conn: psycopg.Connection, payee: str) -> RuleMatch | None:
    """Exact normalized match against enabled rules. None = no hit."""
    normalized = normalize_payee(payee)
    rows = conn.execute(
        """
        SELECT r.rule_id, r.spend_category_id, c.slug, c.display_name
        FROM spend_label_rules r
        JOIN spend_categories c ON c.spend_category_id = r.spend_category_id
        WHERE r.enabled IS TRUE AND r.payee_pattern = %s
        """,
        (normalized,),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise TokenError(
            "SPEND_LABEL_RULE_AMBIGUOUS: "
            f"multiple enabled rules for pattern ({len(rows)} rows)"
        )
    row = rows[0]
    return RuleMatch(
        rule_id=UUID(str(row[0])),
        spend_category_id=UUID(str(row[1])),
        slug=str(row[2]),
        display_name=str(row[3]),
        payee_normalized=normalized,
        evidence=f"exact map · rule {row[0]}",
    )
