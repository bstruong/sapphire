#!/usr/bin/env python3
"""QA: Category spend pie (Overview History third block).

Happy: labeled + Uncategorized slices; calendar month query; SVG donut.
Negative: empty month → HISTORY_SPEND_CATEGORY_EMPTY; invalid month fails loud.

Uses sapphire_test only — refuses dogfood wipe.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _truncate(url: str) -> None:
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute(
            """
            TRUNCATE TABLE transaction_spend_labels, envelopes, raw_tokens, raw_pages,
                           transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()


def main() -> int:
    print("=" * 70)
    print("QA: Category spend pie (calendar month + Uncategorized)")
    print("=" * 70)
    failures = 0

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

    import psycopg
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db
    from app.db.history import (
        list_available_spend_months,
        list_spend_category_month_totals,
    )
    from app.db.repository import persist_canonical_statement, persist_raw_extraction
    from app.models.canonical import (
        Account,
        CanonicalStatement,
        CanonicalTransaction,
        DepositorySummary,
    )
    from app.models.enums import (
        AccountDomain,
        AccountType,
        RunStatus,
        TransactionCategory,
    )
    from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
    from app.presentation.history_charts import render_spend_category_pie_svg
    from app.spend.categories import set_manual_label

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    _truncate(db_url)

    payload = RawPayload(
        content_sha256="c1" * 32,
        byte_length=900,
        original_basename="qa_pie.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="QA pie")
    account = Account(
        institution="SYNTHETIC Bank",
        account_mask="*0000",
        account_domain=AccountDomain.DEPOSITORY,
        account_type=AccountType.CHECKING,
    )
    statement = CanonicalStatement(
        account_id=account.account_id,
        run_id=run.run_id,
        raw_payload_id=payload.raw_payload_id,
        statement_start_date=datetime.date(2026, 10, 1),
        statement_end_date=datetime.date(2026, 10, 31),
        opening_balance_cents=300000,
        closing_balance_cents=46369,
        net_change_cents=-253631,
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=0,
        withdrawals_cents=253631,
        interest_paid_cents=0,
        fees_cents=0,
    )
    rent = CanonicalTransaction(
        statement_id=statement.statement_id,
        post_date=datetime.date(2026, 10, 5),
        amount_cents=-185000,
        description="SYNTHETIC Rent",
        transaction_category=TransactionCategory.WITHDRAWAL,
    )
    food = CanonicalTransaction(
        statement_id=statement.statement_id,
        post_date=datetime.date(2026, 10, 12),
        amount_cents=-45231,
        description="SYNTHETIC Groceries",
        transaction_category=TransactionCategory.WITHDRAWAL,
    )
    opaque = CanonicalTransaction(
        statement_id=statement.statement_id,
        post_date=datetime.date(2026, 10, 20),
        amount_cents=-23400,
        description="Electronic Withdrawal",
        transaction_category=TransactionCategory.WITHDRAWAL,
    )

    with psycopg.connect(db_url) as conn:
        persist_raw_extraction(
            conn, RawExtraction(payload=payload, run=run, pages=[page])
        )
        persist_canonical_statement(
            conn, account, statement, summary, [rent, food, opaque]
        )
        set_manual_label(conn, transaction_id=rent.transaction_id, slug="rent")
        set_manual_label(conn, transaction_id=food.transaction_id, slug="groceries")
        conn.commit()

    print("\n[Happy: pie slices + SVG]")
    with psycopg.connect(db_url) as conn:
        months = list_available_spend_months(conn)
        slices = list_spend_category_month_totals(conn, year=2026, month=10)
    if "2026-10" not in months:
        _fail("Available months missing 2026-10")
        failures += 1
    else:
        _pass(f"Available spend months include 2026-10 ({months!r})")

    by_slug = {s.slug: s.spend_cents for s in slices}
    if by_slug.get("rent") != 185000 or by_slug.get("uncategorized") != 23400:
        _fail(f"Unexpected slices: {by_slug}")
        failures += 1
    else:
        _pass(f"Slices rent/groceries/uncategorized: {by_slug}")

    try:
        svg = render_spend_category_pie_svg(slices, year=2026, month=10)
    except Exception as exc:  # noqa: BLE001
        _fail(f"SVG render failed: {exc}")
        failures += 1
        svg = ""
    if "pie-svg" not in svg or "$2,536.31" not in svg:
        _fail("SVG missing donut / total")
        failures += 1
    else:
        _pass("SVG donut includes integer-cent total")

    print("\n[Happy: Overview HTMX with calendar month]")
    client = TestClient(create_app())
    hx = {"HX-Request": "true"}
    resp = client.get(
        "/api/v1/dashboard/overview?pie_year=2026&pie_month=10",
        headers=hx,
    )
    if resp.status_code != 200 or "spend-by-category" not in resp.text:
        _fail(f"Overview pie block missing (status={resp.status_code})")
        failures += 1
    elif "Uncategorized" not in resp.text or "Rent" not in resp.text:
        _fail("Overview pie legend missing Rent / Uncategorized")
        failures += 1
    else:
        _pass("Overview History third block shows pie legend")

    if 'id="pie-cal-trigger"' not in resp.text or "month-step" not in resp.text:
        _fail("Calendar primary / step secondary controls missing")
        failures += 1
    else:
        _pass("Calendar primary + step controls present")

    print("\n[Negative: empty month + invalid month]")
    empty = client.get(
        "/api/v1/dashboard/overview?pie_year=2020&pie_month=1",
        headers=hx,
    )
    if empty.status_code != 200 or "HISTORY_SPEND_CATEGORY_EMPTY" not in empty.text:
        _fail("Empty month should show HISTORY_SPEND_CATEGORY_EMPTY")
        failures += 1
    else:
        _pass("Empty month → HISTORY_SPEND_CATEGORY_EMPTY")

    bad = client.get(
        "/api/v1/dashboard/overview?pie_year=2026&pie_month=99",
        headers=hx,
    )
    if bad.status_code != 400 or "PIE_MONTH_INVALID" not in bad.text:
        _fail(f"Invalid month should 400 (got {bad.status_code})")
        failures += 1
    else:
        _pass("Invalid month → 400 PIE_MONTH_INVALID")

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
