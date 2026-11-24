#!/usr/bin/env python3
"""QA: Bulk suggest labels (B1/B2/P3) — rules + statement scope + opaque honesty.

Happy: synthetic payees + rule → Accept group writes N labels source=rule.
Happy (P3): statement_id Run stages only that statement; Accept does not bleed.
Negative: opaque EW never suggestable; unknown slug fails loud; JARVIS down
skips Vault with loud UI copy; neither scope fails loud; dual scope prefers
statement.

Uses sapphire_test only — refuses dogfood wipe / re-seed.
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
            TRUNCATE TABLE spend_label_suggestions, spend_label_rules,
                           transaction_spend_labels, envelopes, raw_tokens,
                           raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()


def main() -> int:
    print("=" * 70)
    print("QA: Bulk suggest labels (rules + Accept/Reject + Vault downtime)")
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
    from app.models.exceptions import TokenError
    from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
    from app.spend.categories import (
        get_labels_by_transaction_ids,
        list_spend_categories,
    )
    from app.spend.rules import insert_spend_label_rule, normalize_payee
    from app.spend.suggest import (
        accept_suggestion_group,
        require_suggestable_transaction,
        run_suggest,
    )

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"init_db failed: {exc}")
        return 1

    _truncate(db_url)

    class _DownVault:
        def is_available(self) -> bool:
            return False

        def extract_structured(self, text: str, schema_description: str) -> dict:
            raise RuntimeError("should not be called")

    with psycopg.connect(db_url) as conn:
        cats = {c.slug: c for c in list_spend_categories(conn)}
        insert_spend_label_rule(
            conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        conn.commit()

        payload = RawPayload(
            content_sha256="c" * 64,
            byte_length=500,
            original_basename="qa_bulk_suggest.pdf",
        )
        run = ExtractionRun(
            raw_payload_id=payload.raw_payload_id,
            adapter_id="standard_depository",
            adapter_version="1.0.0",
            status=RunStatus.VALIDATED,
        )
        page = RawPage(run_id=run.run_id, page_number=1, page_text="QA bulk")
        persist_raw_extraction(
            conn, RawExtraction(payload=payload, run=run, pages=[page])
        )
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
            closing_balance_cents=50000,
            net_change_cents=-150000,
        )
        summary = DepositorySummary(
            statement_id=statement.statement_id,
            deposits_cents=0,
            withdrawals_cents=150000,
            interest_paid_cents=0,
            fees_cents=0,
        )
        txns = [
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2026, 10, 5),
                amount_cents=-38412,
                description="SYNTHETIC PG&E BILL",
                transaction_category=TransactionCategory.WITHDRAWAL,
                payee="SYNTHETIC PG&E",
            ),
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2026, 10, 6),
                amount_cents=-38412,
                description="SYNTHETIC PG&E BILL",
                transaction_category=TransactionCategory.WITHDRAWAL,
                payee="SYNTHETIC PG&E",
            ),
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2026, 10, 8),
                amount_cents=-120000,
                description="Electronic Withdrawal",
                transaction_category=TransactionCategory.WITHDRAWAL,
                payee=None,
            ),
        ]
        persist_canonical_statement(conn, account, statement, summary, txns)

        # Second statement in the same month (cross-bleed probe for P3).
        payload_b = RawPayload(
            content_sha256="d" * 64,
            byte_length=500,
            original_basename="qa_bulk_suggest_b.pdf",
        )
        run_b = ExtractionRun(
            raw_payload_id=payload_b.raw_payload_id,
            adapter_id="standard_depository",
            adapter_version="1.0.0",
            status=RunStatus.VALIDATED,
        )
        page_b = RawPage(run_id=run_b.run_id, page_number=1, page_text="QA bulk B")
        persist_raw_extraction(
            conn, RawExtraction(payload=payload_b, run=run_b, pages=[page_b])
        )
        account_b = Account(
            institution="SYNTHETIC Bank B",
            account_mask="*1111",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
        )
        statement_b = CanonicalStatement(
            account_id=account_b.account_id,
            run_id=run_b.run_id,
            raw_payload_id=payload_b.raw_payload_id,
            statement_start_date=datetime.date(2026, 10, 1),
            statement_end_date=datetime.date(2026, 10, 31),
            opening_balance_cents=100000,
            closing_balance_cents=50000,
            net_change_cents=-50000,
        )
        summary_b = DepositorySummary(
            statement_id=statement_b.statement_id,
            deposits_cents=0,
            withdrawals_cents=50000,
            interest_paid_cents=0,
            fees_cents=0,
        )
        txns_b = [
            CanonicalTransaction(
                statement_id=statement_b.statement_id,
                post_date=datetime.date(2026, 10, 9),
                amount_cents=-50000,
                description="SYNTHETIC PG&E OTHER STMT",
                transaction_category=TransactionCategory.WITHDRAWAL,
                payee="SYNTHETIC PG&E",
            ),
        ]
        persist_canonical_statement(conn, account_b, statement_b, summary_b, txns_b)
        conn.commit()

        if normalize_payee("  SYNTHETIC  PG&E ") != "synthetic pg&e":
            _fail("normalize_payee collapsed whitespace")
            failures += 1
        else:
            _pass("normalize_payee lower + collapse whitespace")

        try:
            run_suggest(conn, vault_client=_DownVault())
            _fail("neither scope did not fail loud")
            failures += 1
        except TokenError as exc:
            if "SUGGEST_SCOPE_REQUIRED" in str(exc):
                _pass("neither scope fails loud")
            else:
                _fail(f"unexpected neither-scope error: {exc}")
                failures += 1

        stmt_batch = run_suggest(
            conn,
            year=2026,
            month=10,
            statement_id=statement.statement_id,
            vault_client=_DownVault(),
        )
        conn.commit()
        if stmt_batch.statement_id != statement.statement_id:
            _fail("dual scope did not prefer statement")
            failures += 1
        elif stmt_batch.suggestable_count != 2 or stmt_batch.opaque_count != 1:
            _fail(
                f"statement scope counts opaque={stmt_batch.opaque_count} "
                f"suggestable={stmt_batch.suggestable_count}"
            )
            failures += 1
        elif not stmt_batch.rule_groups or stmt_batch.rule_groups[0].txn_count != 2:
            _fail("statement scope staged wrong txn count")
            failures += 1
        else:
            _pass("dual scope prefers statement; counts scoped")

        written_stmt = accept_suggestion_group(
            conn,
            batch_id=stmt_batch.batch_id,
            group_key=stmt_batch.rule_groups[0].group_key,
            slug="utilities",
        )
        conn.commit()
        labels_a = get_labels_by_transaction_ids(
            conn, [txns[0].transaction_id, txns[1].transaction_id]
        )
        labels_b = get_labels_by_transaction_ids(conn, [txns_b[0].transaction_id])
        if written_stmt != 2 or labels_b:
            _fail(
                f"statement Accept bleed written={written_stmt} "
                f"other_stmt_labels={len(labels_b)}"
            )
            failures += 1
        elif any(lab.source != "rule" for lab in labels_a.values()):
            _fail("statement Accept wrong label source")
            failures += 1
        else:
            _pass("statement Accept writes only that statement's txns")

        # Re-seed month path on remaining unlabeled (stmt B + opaque on A).
        batch = run_suggest(conn, year=2026, month=10, vault_client=_DownVault())
        conn.commit()
        if batch.statement_id is not None:
            _fail("month mode unexpectedly set statement_id")
            failures += 1
        elif batch.opaque_count != 1 or batch.suggestable_count != 1:
            _fail(
                f"month scope after statement Accept "
                f"opaque={batch.opaque_count} suggestable={batch.suggestable_count}"
            )
            failures += 1
        else:
            _pass("month mode unchanged after statement Accept")

        if batch.vault_available or not batch.rule_groups:
            _fail("expected month rules staged and vault down")
            failures += 1
        else:
            _pass("month rules staged; Vault lane skipped (JARVIS down)")

        try:
            require_suggestable_transaction(conn, txns[2].transaction_id)
            _fail("opaque Accept path did not fail loud")
            failures += 1
        except TokenError as exc:
            if "OPAQUE_PAYEE_INELIGIBLE" in str(exc):
                _pass("opaque EW Accept fails loud")
            else:
                _fail(f"unexpected opaque error: {exc}")
                failures += 1

        group = batch.rule_groups[0]
        try:
            accept_suggestion_group(
                conn,
                batch_id=batch.batch_id,
                group_key=group.group_key,
                slug="not_a_real_slug",
            )
            _fail("unknown slug did not fail loud")
            failures += 1
        except TokenError as exc:
            if "SPEND_CATEGORY_UNKNOWN" in str(exc):
                _pass("unknown slug fails loud")
            else:
                _fail(f"unexpected slug error: {exc}")
                failures += 1

        written = accept_suggestion_group(
            conn,
            batch_id=batch.batch_id,
            group_key=group.group_key,
            slug="utilities",
        )
        conn.commit()
        labels = get_labels_by_transaction_ids(conn, [txns_b[0].transaction_id])
        if written != 1 or any(lab.source != "rule" for lab in labels.values()):
            _fail(f"month Accept wrote {written} labels sources={labels}")
            failures += 1
        else:
            _pass("month Accept group wrote 1 label source=rule")

    # HTMX panel smoke (still on sapphire_test)
    os.environ["DATABASE_URL"] = db_url
    client = TestClient(create_app())
    from app.spend import suggest as suggest_mod

    suggest_mod.VaultInferenceClient = lambda: _DownVault()  # type: ignore[assignment]

    # Re-seed unlabeled for HTMX after Accept emptied suggestable rows.
    with psycopg.connect(db_url) as conn:
        from app.models.canonical import CanonicalTransaction as CT

        # Use a fresh payload so HTMX month + statement CTAs still exercise.
        payload_h = RawPayload(
            content_sha256="e" * 64,
            byte_length=400,
            original_basename="qa_htmx_suggest.pdf",
        )
        run_h = ExtractionRun(
            raw_payload_id=payload_h.raw_payload_id,
            adapter_id="standard_depository",
            adapter_version="1.0.0",
            status=RunStatus.VALIDATED,
        )
        persist_raw_extraction(
            conn,
            RawExtraction(
                payload=payload_h,
                run=run_h,
                pages=[RawPage(run_id=run_h.run_id, page_number=1, page_text="HTMX")],
            ),
        )
        account_h = Account(
            institution="SYNTHETIC HTMX",
            account_mask="*2222",
            account_domain=AccountDomain.DEPOSITORY,
            account_type=AccountType.CHECKING,
        )
        statement_h = CanonicalStatement(
            account_id=account_h.account_id,
            run_id=run_h.run_id,
            raw_payload_id=payload_h.raw_payload_id,
            statement_start_date=datetime.date(2026, 10, 1),
            statement_end_date=datetime.date(2026, 10, 31),
            opening_balance_cents=0,
            closing_balance_cents=-1000,
            net_change_cents=-1000,
        )
        persist_canonical_statement(
            conn,
            account_h,
            statement_h,
            DepositorySummary(
                statement_id=statement_h.statement_id,
                deposits_cents=0,
                withdrawals_cents=1000,
                interest_paid_cents=0,
                fees_cents=0,
            ),
            [
                CT(
                    statement_id=statement_h.statement_id,
                    post_date=datetime.date(2026, 10, 12),
                    amount_cents=-1000,
                    description="SYNTHETIC PG&E HTMX",
                    transaction_category=TransactionCategory.WITHDRAWAL,
                    payee="SYNTHETIC PG&E",
                )
            ],
        )
        conn.commit()
        htmx_statement_id = statement_h.statement_id

    resp = client.post(
        "/api/v1/dashboard/ledger/suggest/run",
        data={"pie_year": "2026", "pie_month": "10"},
        headers={"HX-Request": "true"},
    )
    if resp.status_code != 200 or "JARVIS unavailable" not in resp.text:
        _fail("HTMX Run suggest missing downtime chrome")
        failures += 1
    else:
        _pass("HTMX Run suggest shows JARVIS downtime + panel")

    if "Opaque" not in resp.text and "Suggestable" not in resp.text:
        _fail("HTMX panel missing scope chips")
        failures += 1
    else:
        _pass("HTMX month panel renders")

    stmt_page = client.get(
        f"/api/v1/dashboard/statements/{htmx_statement_id}",
        headers={"HX-Request": "true"},
    )
    if (
        stmt_page.status_code != 200
        or "Suggest for this statement" not in stmt_page.text
    ):
        _fail("statement ledger missing Suggest for this statement CTA")
        failures += 1
    else:
        _pass("statement ledger shows Suggest for this statement")

    stmt_run = client.post(
        "/api/v1/dashboard/ledger/suggest/run",
        data={"statement_id": str(htmx_statement_id)},
        headers={"HX-Request": "true"},
    )
    if stmt_run.status_code != 200 or "This statement" not in stmt_run.text:
        _fail("HTMX statement Run missing statement chrome")
        failures += 1
    else:
        _pass("HTMX statement Run prefer-statement panel")

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
