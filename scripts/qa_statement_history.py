#!/usr/bin/env python3
"""QA: Full statement history (Recent → View all, filters, Open → ledger).

Happy: seeded history lists by year, account/year filters narrow rows,
Open from history returns ledger with back-link to history.
Negative: malformed account/year filters fail loudly; empty filter match
shows an explicit empty state (no silent blank table).

Uses sapphire_test only — never truncates the dogfood Vault database.
"""

from __future__ import annotations

import datetime
import os
import sys
import uuid
from urllib.parse import urlparse

import psycopg

HX = {"HX-Request": "true"}


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _require_test_db(url: str) -> None:
    name = urlparse(url).path.lstrip("/").lower()
    if not name.endswith(("_test", "_pytest")):
        raise SystemExit(
            f"Refusing QA against non-test database {name!r}. "
            "Set TEST_DATABASE_URL (…/_test)."
        )


def _truncate(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute(
            """
            TRUNCATE TABLE envelopes, raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()


def _insert_account(
    conn: psycopg.Connection,
    *,
    institution: str,
    mask: str,
    domain: str = "depository",
    account_type: str = "checking",
) -> uuid.UUID:
    account_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO accounts (
            account_id, institution, account_mask, account_domain, account_type, currency
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (account_id, institution, mask, domain, account_type, "USD"),
    )
    return account_id


def _insert_statement(
    conn: psycopg.Connection,
    account_id: uuid.UUID,
    *,
    start: datetime.date,
    end: datetime.date,
    txn_count: int,
) -> uuid.UUID:
    payload_id = uuid.uuid4()
    run_id = uuid.uuid4()
    statement_id = uuid.uuid4()
    opening = 100_00
    closing = 120_00
    conn.execute(
        """
        INSERT INTO raw_payloads (
            raw_payload_id, content_sha256, byte_length, original_basename, ingested_at
        ) VALUES (%s, %s, %s, %s, NOW())
        """,
        (payload_id, uuid.uuid4().hex + uuid.uuid4().hex, 128, "qa_history.pdf"),
    )
    conn.execute(
        """
        INSERT INTO extraction_runs (
            run_id, raw_payload_id, adapter_id, adapter_version, started_at, status
        ) VALUES (%s, %s, %s, %s, NOW(), %s)
        """,
        (run_id, payload_id, "synthetic", "0.0.0", "canonical_persisted"),
    )
    conn.execute(
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
            opening,
            closing,
            closing - opening,
        ),
    )
    conn.execute(
        """
        INSERT INTO statement_depository_summaries (
            statement_id, deposits_cents, withdrawals_cents,
            interest_paid_cents, fees_cents
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (statement_id, closing - opening, 0, 0, 0),
    )
    for i in range(txn_count):
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id, statement_id, post_date, amount_cents,
                description, transaction_category
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                uuid.uuid4(),
                statement_id,
                start + datetime.timedelta(days=i + 1),
                10_00,
                "SYNTHETIC Payroll",
                "deposit",
            ),
        )
    return statement_id


def main() -> int:
    print("QA: Statement history — View all + filters + Open → ledger")
    failures = 0

    test_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres:sapphire@localhost:54329/sapphire_test",
    )
    try:
        _require_test_db(test_url)
    except SystemExit as exc:
        _fail(str(exc))
        print("\nResult: [FAIL]")
        return 1

    os.environ["DATABASE_URL"] = test_url
    os.environ["TEST_DATABASE_URL"] = test_url

    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db

    try:
        init_db()
        _truncate(test_url)
    except Exception as exc:  # noqa: BLE001
        _fail(f"database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    client = TestClient(create_app())

    recent = client.get("/api/v1/dashboard/statements", headers=HX)
    if recent.status_code == 200 and 'id="view-all-history"' in recent.text:
        _pass("Recent statements includes View all history CTA")
    else:
        _fail("Recent missing View all history CTA")
        failures += 1

    empty = client.get("/api/v1/dashboard/statements/history", headers=HX)
    if empty.status_code == 200 and "No statements yet" in empty.text:
        _pass("Empty history shows loud empty state")
    else:
        _fail("Empty history missing loud empty state")
        failures += 1

    with psycopg.connect(test_url) as conn:
        schwab = _insert_account(conn, institution="Charles Schwab Bank", mask="*4821")
        chase = _insert_account(
            conn,
            institution="SYNTHETIC Chase",
            mask="*0000",
            domain="revolving_credit",
            account_type="credit_card",
        )
        _insert_statement(
            conn,
            schwab,
            start=datetime.date(2025, 12, 1),
            end=datetime.date(2025, 12, 31),
            txn_count=2,
        )
        newest = _insert_statement(
            conn,
            schwab,
            start=datetime.date(2026, 3, 1),
            end=datetime.date(2026, 3, 31),
            txn_count=4,
        )
        _insert_statement(
            conn,
            chase,
            start=datetime.date(2026, 2, 1),
            end=datetime.date(2026, 2, 28),
            txn_count=3,
        )
        conn.commit()

    history = client.get("/api/v1/dashboard/statements/history", headers=HX)
    checks = [
        (history.status_code == 200, "history HTTP 200"),
        ("3 statements" in history.text, "history count"),
        ('class="year-label"' in history.text, "year groups present"),
        ("2026" in history.text and "2025" in history.text, "both years listed"),
        ("Charles Schwab Bank" in history.text, "Schwab row"),
        ("SYNTHETIC Chase" in history.text, "Chase row"),
        ("?from=history" in history.text, "Open preserves history origin"),
    ]
    for ok, label in checks:
        if ok:
            _pass(label)
        else:
            _fail(label)
            failures += 1

    filtered = client.get(
        f"/api/v1/dashboard/statements/history?account={schwab}&year=2026",
        headers=HX,
    )
    if filtered.status_code == 200 and "1 statement" in filtered.text:
        _pass("Account + year filter narrows to one statement")
    else:
        _fail("Combined filters did not narrow results")
        failures += 1

    miss = client.get(
        "/api/v1/dashboard/statements/history?year=2019",
        headers=HX,
    )
    if miss.status_code == 200 and "No statements match this filter" in miss.text:
        _pass("Non-matching filter shows loud empty state")
    else:
        _fail("Non-matching filter silent or wrong")
        failures += 1

    bad_account = client.get(
        "/api/v1/dashboard/statements/history?account=not-a-uuid",
        headers=HX,
    )
    if bad_account.status_code == 400 and "INVALID_ACCOUNT_FILTER" in bad_account.text:
        _pass("Malformed account filter rejected loudly")
    else:
        _fail("Malformed account filter not rejected")
        failures += 1

    bad_year = client.get(
        "/api/v1/dashboard/statements/history?year=nope",
        headers=HX,
    )
    if bad_year.status_code == 400 and "INVALID_YEAR_FILTER" in bad_year.text:
        _pass("Malformed year filter rejected loudly")
    else:
        _fail("Malformed year filter not rejected")
        failures += 1

    ledger = client.get(
        f"/api/v1/dashboard/statements/{newest}?from=history",
        headers=HX,
    )
    if (
        ledger.status_code == 200
        and "Vault Ledger" in ledger.text
        and 'hx-get="/api/v1/dashboard/statements/history"' in ledger.text
        and "Back to statement history" in ledger.text
        and "SYNTHETIC Payroll" in ledger.text
    ):
        _pass("Open from history → ledger with back link")
    else:
        _fail("Open from history ledger path broken")
        failures += 1

    no_hx = client.get("/api/v1/dashboard/statements/history")
    if no_hx.status_code == 400:
        _pass("History requires HTMX")
    else:
        _fail("History should require HTMX")
        failures += 1

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} check(s) failed)")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
