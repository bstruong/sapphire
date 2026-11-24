"""Spend categories and manual transaction labels (integer-cents pie series).

Pipeline ``TransactionCategory`` stays untouched. Labels never store money —
amounts always come from ``transactions.amount_cents``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import psycopg

from app.models.exceptions import TokenError

logger = logging.getLogger("app.spend.categories")

LabelSource = Literal["manual", "rule", "vault_suggest_accepted"]

SYSTEM_SLUGS = frozenset(
    {
        "rent",
        "groceries",
        "dining",
        "utilities",
        "transport",
        "transfer",
        "other",
    }
)


@dataclass(frozen=True, slots=True)
class SpendCategory:
    """Closed taxonomy row for merchant/budget pie slices."""

    spend_category_id: UUID
    slug: str
    display_name: str
    is_system: bool


@dataclass(frozen=True, slots=True)
class TransactionSpendLabel:
    """One transaction → spend category assignment."""

    transaction_id: UUID
    spend_category_id: UUID
    slug: str
    display_name: str
    source: LabelSource
    labeled_at: datetime


def list_spend_categories(conn: psycopg.Connection) -> list[SpendCategory]:
    """Return spend categories ordered by display name."""
    rows = conn.execute(
        """
        SELECT spend_category_id, slug, display_name, is_system
        FROM spend_categories
        ORDER BY lower(display_name), slug
        """
    ).fetchall()
    return [
        SpendCategory(
            spend_category_id=UUID(str(row[0])),
            slug=str(row[1]),
            display_name=str(row[2]),
            is_system=bool(row[3]),
        )
        for row in rows
    ]


def _require_category_by_slug(conn: psycopg.Connection, slug: str) -> SpendCategory:
    cleaned = slug.strip() if isinstance(slug, str) else ""
    if not cleaned:
        raise TokenError("SPEND_CATEGORY_SLUG_REQUIRED: slug must be non-empty")
    row = conn.execute(
        """
        SELECT spend_category_id, slug, display_name, is_system
        FROM spend_categories
        WHERE slug = %s
        """,
        (cleaned,),
    ).fetchone()
    if row is None:
        raise TokenError(f"SPEND_CATEGORY_UNKNOWN: unknown slug {cleaned!r}")
    return SpendCategory(
        spend_category_id=UUID(str(row[0])),
        slug=str(row[1]),
        display_name=str(row[2]),
        is_system=bool(row[3]),
    )


def _require_transaction(conn: psycopg.Connection, transaction_id: UUID) -> None:
    row = conn.execute(
        "SELECT 1 FROM transactions WHERE transaction_id = %s",
        (str(transaction_id),),
    ).fetchone()
    if row is None:
        raise TokenError(
            f"TRANSACTION_NOT_FOUND: transaction_id={transaction_id} missing"
        )


def set_manual_label(
    conn: psycopg.Connection,
    *,
    transaction_id: UUID,
    slug: str,
) -> TransactionSpendLabel:
    """Upsert a manual spend label. Fail loud on unknown slug / missing txn."""
    if not isinstance(transaction_id, UUID):
        raise TokenError("transaction_id must be a UUID")
    _require_transaction(conn, transaction_id)
    category = _require_category_by_slug(conn, slug)
    labeled_at = datetime.now(UTC)
    conn.execute(
        """
        INSERT INTO transaction_spend_labels (
            transaction_id, spend_category_id, source, labeled_at
        ) VALUES (%s, %s, 'manual', %s)
        ON CONFLICT (transaction_id) DO UPDATE
        SET spend_category_id = EXCLUDED.spend_category_id,
            source = 'manual',
            labeled_at = EXCLUDED.labeled_at
        """,
        (str(transaction_id), str(category.spend_category_id), labeled_at),
    )
    logger.info(
        "Manual spend label set",
        extra={
            "transaction_id": str(transaction_id),
            "spend_category_slug": category.slug,
            "source": "manual",
        },
    )
    return TransactionSpendLabel(
        transaction_id=transaction_id,
        spend_category_id=category.spend_category_id,
        slug=category.slug,
        display_name=category.display_name,
        source="manual",
        labeled_at=labeled_at,
    )


def clear_manual_label(conn: psycopg.Connection, *, transaction_id: UUID) -> None:
    """Remove spend label (row becomes Uncategorized for the pie)."""
    if not isinstance(transaction_id, UUID):
        raise TokenError("transaction_id must be a UUID")
    _require_transaction(conn, transaction_id)
    conn.execute(
        "DELETE FROM transaction_spend_labels WHERE transaction_id = %s",
        (str(transaction_id),),
    )
    logger.info(
        "Spend label cleared",
        extra={"transaction_id": str(transaction_id)},
    )


def get_labels_by_transaction_ids(
    conn: psycopg.Connection,
    transaction_ids: list[UUID],
) -> dict[UUID, TransactionSpendLabel]:
    """Bulk-load labels for ledger rendering. Unknown ids simply omit."""
    if not transaction_ids:
        return {}
    # Fail loud on non-UUID inputs rather than string-coercing silently.
    for txn_id in transaction_ids:
        if not isinstance(txn_id, UUID):
            raise TokenError("transaction_ids must be UUID values")
    rows = conn.execute(
        """
        SELECT l.transaction_id, l.spend_category_id, c.slug, c.display_name,
               l.source, l.labeled_at
        FROM transaction_spend_labels l
        JOIN spend_categories c ON c.spend_category_id = l.spend_category_id
        WHERE l.transaction_id = ANY(%s)
        """,
        ([str(txn_id) for txn_id in transaction_ids],),
    ).fetchall()
    out: dict[UUID, TransactionSpendLabel] = {}
    for row in rows:
        source = str(row[4])
        if source not in ("manual", "rule", "vault_suggest_accepted"):
            raise TokenError(f"Invalid spend label source {source!r}")
        txn_id = UUID(str(row[0]))
        out[txn_id] = TransactionSpendLabel(
            transaction_id=txn_id,
            spend_category_id=UUID(str(row[1])),
            slug=str(row[2]),
            display_name=str(row[3]),
            source=source,  # type: ignore[arg-type]
            labeled_at=row[5],
        )
    return out

