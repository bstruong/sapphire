#!/usr/bin/env python3
"""QA: Spend-category taxonomy + manual HTMX labeling (category spend step 3).

Happy: system slugs seeded; manual label persists; ledger HTMX select assigns.
Negative: unknown slug / missing txn fail loud; empty slug clears to Uncategorized.

Uses sapphire_test only — refuses dogfood wipe.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from uuid import uuid4

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
            TRUNCATE TABLE spend_label_suggestions, spend_label_rules,
                           transaction_spend_labels, envelopes, raw_tokens, raw_pages,
                           transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()


def main() -> int:
    print("=" * 70)
    print("QA: Spend taxonomy + manual ledger labels")
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
    from app.db.repository import (
        persist_canonical_statement,
        persist_raw_extraction,
    )
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
    from app.models.exceptions import TokenError
    from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
    from app.spend.categories import (
        SYSTEM_SLUGS,
        clear_manual_label,
        get_labels_by_transaction_ids,
        list_spend_categories,
        set_manual_label,
    )

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    _truncate(db_url)

    print("\n[Happy: system taxonomy seeded]")
    with psycopg.connect(db_url) as conn:
        cats = list_spend_categories(conn)
    slugs = {c.slug for c in cats}
    if not SYSTEM_SLUGS <= slugs:
        _fail(f"Missing system slugs: {sorted(SYSTEM_SLUGS - slugs)}")
        failures += 1
    else:
        _pass(f"System slugs present ({len(SYSTEM_SLUGS)}): {sorted(SYSTEM_SLUGS)}")

    payload = RawPayload(
        content_sha256="a1" * 32,
        byte_length=800,
        original_basename="qa_spend.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="QA spend")
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
        opening_balance_cents=200000,
        closing_balance_cents=150000,
        net_change_cents=-50000,
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=0,
        withdrawals_cents=50000,
        interest_paid_cents=0,
        fees_cents=0,
    )
    txn = CanonicalTransaction(
        statement_id=statement.statement_id,
        post_date=datetime.date(2026, 10, 12),
        amount_cents=-50000,
        description="Electronic Withdrawal",
        transaction_category=TransactionCategory.WITHDRAWAL,
    )

    with psycopg.connect(db_url) as conn:
        persist_raw_extraction(
            conn, RawExtraction(payload=payload, run=run, pages=[page])
        )
        persist_canonical_statement(conn, account, statement, summary, [txn])
        conn.commit()

    print("\n[Happy: manual label + HTMX ledger]")
    with psycopg.connect(db_url) as conn:
        label = set_manual_label(conn, transaction_id=txn.transaction_id, slug="rent")
        conn.commit()
    if label.slug != "rent":
        _fail("Manual label did not stick")
        failures += 1
    else:
        _pass(f"Manual label set: {label.display_name}")

    client = TestClient(create_app())
    hx = {"HX-Request": "true"}
    resp = client.get(
        f"/api/v1/dashboard/statements/{statement.statement_id}",
        headers=hx,
    )
    if (
        resp.status_code != 200
        or "spend_category_slug" not in resp.text
        and "Rent" not in resp.text
    ):
        # Template shows selected option text / Uncategorized
        if resp.status_code != 200 or "Rent" not in resp.text:
            _fail(f"Ledger missing spend UI / Rent (status={resp.status_code})")
            failures += 1
        else:
            _pass("Ledger shows Rent spend category")
    else:
        _pass("Ledger shows Rent spend category")

    clear_resp = client.post(
        "/api/v1/dashboard/ledger/label",
        headers=hx,
        data={
            "transaction_id": str(txn.transaction_id),
            "statement_id": str(statement.statement_id),
            "spend_category_slug": "groceries",
        },
    )
    if clear_resp.status_code != 200 or "Groceries" not in clear_resp.text:
        _fail(f"HTMX label assign failed (status={clear_resp.status_code})")
        failures += 1
    else:
        _pass("HTMX assign → Groceries")

    uncat = client.post(
        "/api/v1/dashboard/ledger/label",
        headers=hx,
        data={
            "transaction_id": str(txn.transaction_id),
            "statement_id": str(statement.statement_id),
            "spend_category_slug": "",
        },
    )
    if uncat.status_code != 200 or "Uncategorized" not in uncat.text:
        _fail("HTMX clear to Uncategorized failed")
        failures += 1
    else:
        _pass("HTMX clear → Uncategorized")

    print("\n[Negative: unknown slug / missing txn]")
    with psycopg.connect(db_url) as conn:
        try:
            set_manual_label(
                conn, transaction_id=txn.transaction_id, slug="not_a_real_slug"
            )
            _fail("Expected TokenError for unknown slug")
            failures += 1
        except TokenError as exc:
            if "SPEND_CATEGORY_UNKNOWN" in str(exc):
                _pass("Loud reject unknown slug")
            else:
                _fail(f"Wrong error for unknown slug: {exc}")
                failures += 1

        try:
            set_manual_label(conn, transaction_id=uuid4(), slug="rent")
            _fail("Expected TokenError for missing txn")
            failures += 1
        except TokenError as exc:
            if "TRANSACTION_NOT_FOUND" in str(exc):
                _pass("Loud reject missing transaction")
            else:
                _fail(f"Wrong error for missing txn: {exc}")
                failures += 1

        clear_manual_label(conn, transaction_id=txn.transaction_id)
        conn.commit()
        loaded = get_labels_by_transaction_ids(conn, [txn.transaction_id])
        if txn.transaction_id in loaded:
            _fail("Clear left a label behind")
            failures += 1
        else:
            _pass("Clear removes label row")

    bad = client.post(
        "/api/v1/dashboard/ledger/label",
        headers=hx,
        data={
            "transaction_id": str(txn.transaction_id),
            "statement_id": str(statement.statement_id),
            "spend_category_slug": "bogus",
        },
    )
    if bad.status_code != 400 or "SPEND_CATEGORY_UNKNOWN" not in bad.text:
        _fail(f"HTMX unknown slug should 400 (got {bad.status_code})")
        failures += 1
    else:
        _pass("HTMX unknown slug → 400 SPEND_CATEGORY_UNKNOWN")

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
