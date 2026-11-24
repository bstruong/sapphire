#!/usr/bin/env python3
"""Seed multi-month synthetic statements for Overview History charts.

Provides liquid closing balance series + categorized income/spend months
for Manual UI QA of ``pitch_history_charts.md``.

Guarantees:
- Obviously fake names (SYNTHETIC …) and masks (*0000, *0001)
- Integer cents only
- Idempotent truncate + reseed of the dogfood Vault DB (never pytest DB)
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

from app.db.connection import get_db_connection, init_db


def _get_or_create_account(
    cur,
    institution: str,
    mask: str,
    domain: str,
    acc_type: str,
    currency: str = "USD",
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO accounts (
            account_id, institution, account_mask, account_domain, account_type, currency
        )
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (institution, account_mask, account_type)
        DO UPDATE SET currency = EXCLUDED.currency
        RETURNING account_id
        """,
        (uuid.uuid4(), institution, mask, domain, acc_type, currency),
    )
    return cur.fetchone()[0]


def _insert_statement_chain(
    cur,
    *,
    account_id: uuid.UUID,
    adapter_id: str,
    basename: str,
    start: date,
    end: date,
    opening_cents: int,
    closing_cents: int,
    now: datetime,
) -> uuid.UUID:
    payload_id = uuid.uuid4()
    run_id = uuid.uuid4()
    statement_id = uuid.uuid4()
    cur.execute(
        """
        INSERT INTO raw_payloads (
            raw_payload_id, content_sha256, byte_length, original_basename, ingested_at
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (
            payload_id,
            uuid.uuid4().hex + uuid.uuid4().hex,
            2048,
            basename,
            now,
        ),
    )
    cur.execute(
        """
        INSERT INTO extraction_runs (
            run_id, raw_payload_id, adapter_id, adapter_version, started_at, status
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (run_id, payload_id, adapter_id, "1.0", now, "canonical_persisted"),
    )
    cur.execute(
        """
        INSERT INTO statements (
            statement_id, account_id, run_id, raw_payload_id,
            statement_start_date, statement_end_date,
            opening_balance_cents, closing_balance_cents, net_change_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            statement_id,
            account_id,
            run_id,
            payload_id,
            start,
            end,
            opening_cents,
            closing_cents,
            closing_cents - opening_cents,
        ),
    )
    return statement_id


def _insert_txn(
    cur,
    statement_id: uuid.UUID,
    post_date: date,
    amount_cents: int,
    description: str,
    category: str,
) -> None:
    cur.execute(
        """
        INSERT INTO transactions (
            transaction_id, statement_id, post_date, amount_cents,
            description, transaction_category
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            uuid.uuid4(),
            statement_id,
            post_date,
            amount_cents,
            description,
            category,
        ),
    )


def seed() -> None:
    if not os.environ.get("DATABASE_URL"):
        # Dogfood Vault DB — never the pytest sapphire_test database.
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire"
        )

    init_db()
    with get_db_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE envelopes, raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        now = datetime.now(tz=UTC)

        checking = _get_or_create_account(
            cur, "Chase", "*0000", "depository", "checking"
        )
        savings = _get_or_create_account(
            cur, "Schwab", "*0001", "depository", "savings"
        )

        # Six aligned statement periods (Jun–Nov 2026) with rising liquid totals.
        periods = [
            # start, end, checking open/close, savings open/close,
            # deposit, spend_amount, spend_category, spend_desc
            (
                date(2026, 6, 1),
                date(2026, 6, 30),
                500_000,
                580_000,
                200_000,
                220_000,
                320_000,
                90_000,
                "withdrawal",
                "SYNTHETIC Rent",
            ),
            (
                date(2026, 7, 1),
                date(2026, 7, 31),
                580_000,
                660_000,
                220_000,
                240_000,
                320_000,
                100_000,
                "withdrawal",
                "SYNTHETIC Rent",
            ),
            (
                date(2026, 8, 1),
                date(2026, 8, 31),
                660_000,
                740_000,
                240_000,
                255_000,
                340_000,
                85_000,
                "purchase",
                "SYNTHETIC Grocer",
            ),
            (
                date(2026, 9, 1),
                date(2026, 9, 30),
                740_000,
                820_000,
                255_000,
                270_000,
                340_000,
                95_000,
                "withdrawal",
                "SYNTHETIC Utility",
            ),
            (
                date(2026, 10, 1),
                date(2026, 10, 31),
                820_000,
                900_000,
                270_000,
                290_000,
                360_000,
                110_000,
                "withdrawal",
                "SYNTHETIC Rent",
            ),
            (
                date(2026, 11, 1),
                date(2026, 11, 30),
                900_000,
                980_000,
                290_000,
                310_000,
                360_000,
                75_000,
                "purchase",
                "SYNTHETIC Hardware",
            ),
        ]

        for (
            start,
            end,
            chk_open,
            chk_close,
            sav_open,
            sav_close,
            deposit,
            spend,
            spend_cat,
            spend_desc,
        ) in periods:
            chk_stmt = _insert_statement_chain(
                cur,
                account_id=checking,
                adapter_id="chase_checking",
                basename=f"synthetic_chase_checking_{end.isoformat()}.pdf",
                start=start,
                end=end,
                opening_cents=chk_open,
                closing_cents=chk_close,
                now=now,
            )
            _insert_statement_chain(
                cur,
                account_id=savings,
                adapter_id="schwab_checking",
                basename=f"synthetic_schwab_savings_{end.isoformat()}.pdf",
                start=start,
                end=end,
                opening_cents=sav_open,
                closing_cents=sav_close,
                now=now,
            )
            mid = date(end.year, end.month, 5)
            spend_day = date(end.year, end.month, 12)
            _insert_txn(
                cur,
                chk_stmt,
                mid,
                deposit,
                "SYNTHETIC Payroll",
                "deposit",
            )
            # Depository spend: negative cents. Map card-only "purchase" → other_debit.
            resolved_spend_cat = "other_debit" if spend_cat == "purchase" else spend_cat
            spend_amount = -abs(spend)
            _insert_txn(
                cur,
                chk_stmt,
                spend_day,
                spend_amount,
                spend_desc,
                resolved_spend_cat,
            )
            cur.execute(
                """
                INSERT INTO statement_depository_summaries (
                    statement_id, deposits_cents, withdrawals_cents,
                    interest_paid_cents, fees_cents
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (chk_stmt, deposit, abs(spend_amount), 0, 0),
            )

        print(
            "[PASS] Seeded SYNTHETIC multi-month History charts "
            "(Chase *0000 + Schwab *0001, Jun–Nov 2026)."
        )


if __name__ == "__main__":
    seed()
