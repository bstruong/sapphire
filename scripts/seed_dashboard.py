#!/usr/bin/env python3
"""Seed the local Vault database with synthetic statements and envelopes.

Provides synthetic data for Manual UI QA of the refreshed dashboard.
Guarantees:
- Obviously fake names (SYNTHETIC Chase, SYNTHETIC Schwab)
- Masked account identifiers (*0000, *0001, *0002)
- Integer cents only (BIGINT in database)
- Zero real customer data, PII, or float values.
- Idempotent: can be run repeatedly without constraint violations.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime

from app.budget.envelopes import create_envelope
from app.db.connection import get_db_connection, init_db


def _get_or_create_account(
    cur, institution: str, mask: str, domain: str, acc_type: str, currency: str = "USD"
) -> uuid.UUID:
    cur.execute(
        """
        INSERT INTO accounts (account_id, institution, account_mask, account_domain, account_type, currency)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (institution, account_mask, account_type) DO UPDATE SET currency = EXCLUDED.currency
        RETURNING account_id
        """,
        (uuid.uuid4(), institution, mask, domain, acc_type, currency),
    )
    return cur.fetchone()[0]


def seed() -> None:
    if not os.environ.get("DATABASE_URL"):
        # Dogfood Vault DB — never the pytest sapphire_test database.
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire"
        )

    init_db()
    with get_db_connection() as conn, conn.cursor() as cur:
        # Reset so re-runs stay idempotent (Manual UI QA against the test DB).
        cur.execute(
            """
            TRUNCATE TABLE envelopes, raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        now = datetime.now(tz=UTC)

        # 1. Chase Checking Account (*0000)
        chase_acc_id = _get_or_create_account(
            cur, "Chase", "*0000", "depository", "checking"
        )
        chase_payload_id = uuid.uuid4()
        chase_run_id = uuid.uuid4()
        chase_stmt_id = uuid.uuid4()

        cur.execute(
            """
            INSERT INTO raw_payloads (raw_payload_id, content_sha256, byte_length, original_basename, ingested_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                chase_payload_id,
                uuid.uuid4().hex + uuid.uuid4().hex,
                2048,
                "synthetic_chase_checking.pdf",
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO extraction_runs (run_id, raw_payload_id, adapter_id, adapter_version, started_at, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                chase_run_id,
                chase_payload_id,
                "chase_checking",
                "1.0",
                now,
                "canonical_persisted",
            ),
        )
        cur.execute(
            """
            INSERT INTO statements (statement_id, account_id, run_id, raw_payload_id, statement_start_date, statement_end_date, opening_balance_cents, closing_balance_cents, net_change_cents)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                chase_stmt_id,
                chase_acc_id,
                chase_run_id,
                chase_payload_id,
                date(2026, 9, 1),
                date(2026, 9, 30),
                681240,
                833012,
                151772,
            ),
        )

        chase_txns = [
            (
                uuid.uuid4(),
                chase_stmt_id,
                date(2026, 9, 1),
                320000,
                "SYNTHETIC Payroll",
                "deposit",
            ),
            (
                uuid.uuid4(),
                chase_stmt_id,
                date(2026, 9, 3),
                -240000,
                "SYNTHETIC Rent Co",
                "withdrawal",
            ),
            (
                uuid.uuid4(),
                chase_stmt_id,
                date(2026, 9, 9),
                -14218,
                "SYNTHETIC Grocer",
                "purchase",
            ),
            (
                uuid.uuid4(),
                chase_stmt_id,
                date(2026, 9, 15),
                320000,
                "SYNTHETIC Payroll",
                "deposit",
            ),
            (
                uuid.uuid4(),
                chase_stmt_id,
                date(2026, 9, 21),
                -24012,
                "SYNTHETIC Utility",
                "withdrawal",
            ),
            (
                uuid.uuid4(),
                chase_stmt_id,
                date(2026, 9, 27),
                -209998,
                "SYNTHETIC Card Payment",
                "withdrawal",
            ),
        ]
        for t in chase_txns:
            cur.execute(
                """
                INSERT INTO transactions (transaction_id, statement_id, post_date, amount_cents, description, transaction_category)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                t,
            )
        cur.execute(
            """
            INSERT INTO statement_depository_summaries (
                statement_id, deposits_cents, withdrawals_cents, interest_paid_cents, fees_cents
            ) VALUES (%s, %s, %s, %s, %s)
            """,
            (chase_stmt_id, 640000, 488228, 0, 0),
        )

        # 2. Chase Credit Card Account (*0001)
        card_acc_id = _get_or_create_account(
            cur, "Chase", "*0001", "revolving_credit", "credit_card"
        )
        card_payload_id = uuid.uuid4()
        card_run_id = uuid.uuid4()
        card_stmt_id = uuid.uuid4()

        cur.execute(
            """
            INSERT INTO raw_payloads (raw_payload_id, content_sha256, byte_length, original_basename, ingested_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                card_payload_id,
                uuid.uuid4().hex + uuid.uuid4().hex,
                1536,
                "synthetic_chase_card.pdf",
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO extraction_runs (run_id, raw_payload_id, adapter_id, adapter_version, started_at, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                card_run_id,
                card_payload_id,
                "chase_card",
                "1.0",
                now,
                "canonical_persisted",
            ),
        )
        cur.execute(
            """
            INSERT INTO statements (statement_id, account_id, run_id, raw_payload_id, statement_start_date, statement_end_date, opening_balance_cents, closing_balance_cents, net_change_cents)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                card_stmt_id,
                card_acc_id,
                card_run_id,
                card_payload_id,
                date(2026, 9, 1),
                date(2026, 9, 30),
                -110418,
                -141293,
                -30875,
            ),
        )

        card_txns = [
            (
                uuid.uuid4(),
                card_stmt_id,
                date(2026, 9, 4),
                -8500,
                "SYNTHETIC Coffee",
                "purchase",
            ),
            (
                uuid.uuid4(),
                card_stmt_id,
                date(2026, 9, 12),
                -12575,
                "SYNTHETIC Books",
                "purchase",
            ),
            (
                uuid.uuid4(),
                card_stmt_id,
                date(2026, 9, 20),
                -9800,
                "SYNTHETIC Hardware",
                "purchase",
            ),
        ]
        for t in card_txns:
            cur.execute(
                """
                INSERT INTO transactions (transaction_id, statement_id, post_date, amount_cents, description, transaction_category)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                t,
            )
        cur.execute(
            """
            INSERT INTO statement_credit_summaries (
                statement_id, previous_balance_cents, payments_credits_cents,
                purchases_cents, cash_advances_cents, balance_transfers_cents,
                fees_charged_cents, interest_charged_cents, new_balance_cents,
                minimum_payment_due_cents, payment_due_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                card_stmt_id,
                -110418,
                0,
                30875,
                0,
                0,
                0,
                0,
                -141293,
                3500,
                date(2026, 10, 15),
            ),
        )

        # 3. Schwab Brokerage Account (*0002)
        schwab_acc_id = _get_or_create_account(
            cur, "Schwab", "*0002", "custodial_brokerage", "brokerage_cash"
        )
        schwab_payload_id = uuid.uuid4()
        schwab_run_id = uuid.uuid4()
        schwab_stmt_id = uuid.uuid4()

        cur.execute(
            """
            INSERT INTO raw_payloads (raw_payload_id, content_sha256, byte_length, original_basename, ingested_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                schwab_payload_id,
                uuid.uuid4().hex + uuid.uuid4().hex,
                4096,
                "synthetic_schwab_brokerage.pdf",
                now,
            ),
        )
        cur.execute(
            """
            INSERT INTO extraction_runs (run_id, raw_payload_id, adapter_id, adapter_version, started_at, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                schwab_run_id,
                schwab_payload_id,
                "schwab_brokerage",
                "1.0",
                now,
                "canonical_persisted",
            ),
        )
        cur.execute(
            """
            INSERT INTO statements (statement_id, account_id, run_id, raw_payload_id, statement_start_date, statement_end_date, opening_balance_cents, closing_balance_cents, net_change_cents)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                schwab_stmt_id,
                schwab_acc_id,
                schwab_run_id,
                schwab_payload_id,
                date(2026, 9, 1),
                date(2026, 9, 30),
                4039760,
                4129318,
                89558,
            ),
        )
        cur.execute(
            """
            INSERT INTO statement_brokerage_summaries (
                statement_id, opening_cash_cents, closing_cash_cents,
                opening_portfolio_cents, closing_portfolio_cents,
                realized_gains_cents, unrealized_gains_cents,
                income_dividends_cents, transfers_in_cents, transfers_out_cents
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                schwab_stmt_id,
                4039760,
                4129318,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
            ),
        )

        # Seed Envelopes (truncate above resets envelopes each run)
        starter_envelopes = [
            "SYNTHETIC Rent",
            "SYNTHETIC Groceries",
            "SYNTHETIC Utilities",
            "SYNTHETIC Travel",
            "SYNTHETIC Emergency Fund",
        ]
        for env_name in starter_envelopes:
            create_envelope(conn, name=env_name)

        print(
            "[PASS] Seeded synthetic Chase (*0000), Chase Card (*0001), Schwab (*0002), and starter envelopes."
        )


if __name__ == "__main__":
    seed()
