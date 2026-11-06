"""Envelope balances and Ready-to-Assign (integer cents only)."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import psycopg

from app.db.repository import list_recent_statements
from app.models.enums import AccountDomain
from app.models.exceptions import AssignExceedsAvailableError, TokenError

logger = logging.getLogger("app.budget.envelopes")


@dataclass(frozen=True, slots=True)
class Envelope:
    """Named envelope with an integer-cent assigned balance."""

    envelope_id: UUID
    name: str
    balance_cents: int


def create_envelope(conn: psycopg.Connection, *, name: str) -> Envelope:
    """Create an envelope with zero balance. Fail loud on blank names."""
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned:
        raise TokenError("Envelope name must be a non-empty string")
    envelope_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO envelopes (envelope_id, name, balance_cents)
        VALUES (%s, %s, 0)
        """,
        (envelope_id, cleaned),
    )
    logger.info(
        "Created envelope",
        extra={"envelope_id": str(envelope_id), "name_len": len(cleaned)},
    )
    return Envelope(envelope_id=envelope_id, name=cleaned, balance_cents=0)


def list_envelopes(conn: psycopg.Connection) -> list[Envelope]:
    """Return envelopes ordered by name (stable UI)."""
    rows = conn.execute(
        """
        SELECT envelope_id, name, balance_cents
        FROM envelopes
        ORDER BY lower(name), envelope_id
        """
    ).fetchall()
    result: list[Envelope] = []
    for envelope_id, name, balance_cents in rows:
        if type(balance_cents) is not int:
            raise TokenError(
                f"envelope balance_cents must be int, got {type(balance_cents).__name__}"
            )
        result.append(
            Envelope(
                envelope_id=envelope_id,
                name=str(name),
                balance_cents=balance_cents,
            )
        )
    return result


def _vault_closing_cents(conn: psycopg.Connection) -> int:
    """Sum latest liquid (depository) closings only — Ready to Assign pool.

    Brokerage funds and revolving credit are excluded. Assignable cash is the
    same bucket as the overview ``Liquid cash`` KPI, not blended net worth.
    """
    seen: set[UUID] = set()
    total = 0
    for account, statement, _txn_count in list_recent_statements(conn, limit=50):
        if account.account_id in seen:
            continue
        seen.add(account.account_id)
        if account.account_domain != AccountDomain.DEPOSITORY:
            continue
        closing = statement.closing_balance_cents
        if type(closing) is not int:
            raise TokenError(
                f"closing_balance_cents must be int, got {type(closing).__name__}"
            )
        total += closing
    return total


def _sum_envelope_balances(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(balance_cents), 0)::bigint FROM envelopes"
    ).fetchone()
    if row is None:
        return 0
    total = row[0]
    # psycopg may return int already; reject non-int
    if type(total) is not int:
        total = int(total)
    return total


def ready_to_assign_cents(conn: psycopg.Connection) -> int:
    """Vault closings minus assigned envelope balances (integer cents)."""
    available = _vault_closing_cents(conn) - _sum_envelope_balances(conn)
    if type(available) is not int:
        raise TokenError("ready_to_assign_cents must be int")
    return available


def assign_to_envelope(
    conn: psycopg.Connection,
    *,
    envelope_id: UUID,
    amount_cents: Any,
) -> Envelope:
    """Move ``amount_cents`` from Ready to Assign into an envelope.

    Raises:
        TokenError: Non-int, non-positive amount, or missing envelope.
        AssignExceedsAvailableError: amount exceeds Ready to Assign.
    """
    if type(amount_cents) is not int or isinstance(amount_cents, bool):
        raise TokenError(
            f"amount_cents must be int cents, got {type(amount_cents).__name__}"
        )
    if amount_cents <= 0:
        raise TokenError("amount_cents must be a positive integer")

    row = conn.execute(
        """
        SELECT envelope_id, name, balance_cents
        FROM envelopes
        WHERE envelope_id = %s
        FOR UPDATE
        """,
        (envelope_id,),
    ).fetchone()
    if row is None:
        raise TokenError("ENVELOPE_NOT_FOUND")

    available = ready_to_assign_cents(conn)
    if amount_cents > available:
        raise AssignExceedsAvailableError(
            "ASSIGN_EXCEEDS_AVAILABLE: assign amount exceeds Ready to Assign"
        )

    new_balance = int(row[2]) + amount_cents
    conn.execute(
        """
        UPDATE envelopes
        SET balance_cents = %s
        WHERE envelope_id = %s
        """,
        (new_balance, envelope_id),
    )
    logger.info(
        "Assigned cents to envelope",
        extra={
            "envelope_id": str(envelope_id),
            "assign_ok": True,
        },
    )
    return Envelope(
        envelope_id=row[0],
        name=str(row[1]),
        balance_cents=new_balance,
    )
