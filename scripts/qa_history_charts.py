#!/usr/bin/env python3
"""QA: Overview History charts (balances + category income/spend).

Happy: multi-month seed → Overview History shows balance polyline + category bars.
Negative: single period → HISTORY_BALANCE_INSUFFICIENT; no txns → HISTORY_CATEGORY_EMPTY;
         empty vault → no History section / overview empty state.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HX = {"HX-Request": "true"}


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _truncate(url: str) -> None:
    import psycopg

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


def main() -> int:
    print("=" * 70)
    print("QA: History charts (Overview balances + category income/spend)")
    print("=" * 70)
    failures = 0

    # Prefer isolated pytest DB when available so dogfood Vault stays intact.
    if os.environ.get("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    elif not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire_test"
        )

    db_url = os.environ["DATABASE_URL"]
    if not db_url.rstrip("/").endswith(("_test", "_pytest")):
        _fail(
            "Refusing to run QA truncate against a non-test database "
            f"(got path ending {db_url.rsplit('/', 1)[-1]!r})"
        )
        print("\nResult: [FAIL]")
        return 1

    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db
    from scripts.seed_history_charts import seed

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    client = TestClient(create_app())

    # --- Negative: empty vault ---
    print("\n[Step 1: Empty vault — no History section]")
    _truncate(db_url)
    empty = client.get("/api/v1/dashboard/overview", headers=HX)
    if (
        empty.status_code == 200
        and "No persisted statements yet" in empty.text
        and 'id="overview-history"' not in empty.text
    ):
        _pass("Empty vault → overview empty state, no History block")
    else:
        _fail("Empty vault did not show overview empty / leaked History")
        failures += 1

    # --- Happy: multi-month seed ---
    print("\n[Step 2: Multi-month seed — both chart series]")
    seed()
    happy = client.get("/api/v1/dashboard/overview", headers=HX)
    html = happy.text
    checks = [
        (happy.status_code == 200, "overview HTTP 200"),
        ('id="overview-history"' in html, "History section present"),
        ("Closing balances" in html, "Balance chart heading"),
        (
            "Category income" in html or "income / spend" in html.lower(),
            "Category heading",
        ),
        ("history-line-liquid" in html, "Liquid balance SVG polyline"),
        ("history-bar-income" in html and "history-bar-spend" in html, "Category bars"),
        ("style=" not in html, "No inline style= (CSS classes only)"),
        ("cdn." not in html.lower(), "No CDN references"),
        ("HISTORY_BALANCE_INSUFFICIENT" not in html, "Balance series populated"),
        ("HISTORY_CATEGORY_EMPTY" not in html, "Category series populated"),
    ]
    for ok, label in checks:
        if ok:
            _pass(label)
        else:
            _fail(label)
            failures += 1

    # --- Negative: single period ---
    print("\n[Step 3: Single period — loud balance empty]")
    _truncate(db_url)
    import datetime
    import uuid

    import psycopg

    with psycopg.connect(db_url) as conn:
        account_id = uuid.uuid4()
        payload_id = uuid.uuid4()
        run_id = uuid.uuid4()
        statement_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO accounts (
                account_id, institution, account_mask, account_domain, account_type, currency
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (account_id, "Chase", "*0000", "depository", "checking", "USD"),
        )
        conn.execute(
            """
            INSERT INTO raw_payloads (
                raw_payload_id, content_sha256, byte_length, original_basename, ingested_at
            ) VALUES (%s, %s, %s, %s, NOW())
            """,
            (payload_id, uuid.uuid4().hex + uuid.uuid4().hex, 128, "one.pdf"),
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
                datetime.date(2026, 9, 1),
                datetime.date(2026, 9, 30),
                100_000,
                150_000,
                50_000,
            ),
        )
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
                datetime.date(2026, 9, 5),
                50_000,
                "SYNTHETIC Payroll",
                "deposit",
            ),
        )
        conn.commit()

    single = client.get("/api/v1/dashboard/overview", headers=HX).text
    if (
        'id="overview-history"' in single
        and "HISTORY_BALANCE_INSUFFICIENT" in single
        and 'role="alert"' in single
        and "history-line-liquid" not in single
    ):
        _pass("Single period → loud HISTORY_BALANCE_INSUFFICIENT (no blank chart)")
    else:
        _fail("Single period did not surface loud balance empty")
        failures += 1

    # --- Negative: periods without txns ---
    print("\n[Step 4: Two periods, no txns — loud category empty]")
    _truncate(db_url)
    with psycopg.connect(db_url) as conn:
        account_id = uuid.uuid4()
        conn.execute(
            """
            INSERT INTO accounts (
                account_id, institution, account_mask, account_domain, account_type, currency
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (account_id, "Chase", "*0000", "depository", "checking", "USD"),
        )
        for start, end, opening, closing in (
            (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30), 100_000, 110_000),
            (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31), 110_000, 120_000),
        ):
            payload_id = uuid.uuid4()
            run_id = uuid.uuid4()
            statement_id = uuid.uuid4()
            conn.execute(
                """
                INSERT INTO raw_payloads (
                    raw_payload_id, content_sha256, byte_length, original_basename, ingested_at
                ) VALUES (%s, %s, %s, %s, NOW())
                """,
                (payload_id, uuid.uuid4().hex + uuid.uuid4().hex, 128, "p.pdf"),
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
        conn.commit()

    no_txn = client.get("/api/v1/dashboard/overview", headers=HX).text
    if (
        "HISTORY_CATEGORY_EMPTY" in no_txn
        and "history-bar-income" not in no_txn
        and "history-line-liquid" in no_txn
    ):
        _pass("No txns → HISTORY_CATEGORY_EMPTY while balance chart still renders")
    else:
        _fail("Missing categorized txns did not surface loud category empty")
        failures += 1

    # Wireframe artifact present
    print("\n[Step 5: Wireframe artifact]")
    wire = ROOT / "docs" / "specs" / "visuals" / "history_charts_wireframe.html"
    if wire.is_file():
        _pass(f"Wireframe present: {wire.relative_to(ROOT)}")
    else:
        _fail("history_charts_wireframe.html missing")
        failures += 1

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} check(s) failed)")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
