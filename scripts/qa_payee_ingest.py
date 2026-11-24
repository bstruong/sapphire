#!/usr/bin/env python3
"""QA: Payee ingest (category spend step 2).

Happy: Chase card redacted PDF persists non-null payee from printed description;
       Schwab checking redacted PDF persists NULL payee for opaque bank types.
Negative: empty / whitespace payee rejected by CanonicalTransaction (fail loud).

Uses TEST_DATABASE_URL / sapphire_test only — refuses dogfood DB wipe.
Does not truncate Metrotower personal data.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
REDACTED = ROOT / "tests" / "fixtures" / "redacted"


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
    print("QA: Payee ingest (nullable transactions.payee)")
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
    from pydantic import ValidationError

    from app.adapters.chase_credit_card import ChaseCreditCardAdapter
    from app.adapters.schwab_checking import SchwabCheckingAdapter
    from app.db.connection import init_db
    from app.db.repository import (
        get_canonical_statement,
        persist_canonical_statement,
        persist_raw_extraction,
    )
    from app.extraction.core import extract_pdf_to_raw
    from app.models.canonical import CanonicalTransaction
    from app.models.enums import TransactionCategory

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    _truncate(db_url)

    chase_pdf = REDACTED / "chase_credit_card_sample.pdf"
    schwab_pdf = REDACTED / "schwab_checking_sample.pdf"
    if not chase_pdf.is_file() or not schwab_pdf.is_file():
        _fail("Missing redacted PDF fixtures")
        return 1

    # --- Happy: Chase persists payee ---
    print("\n[Happy: Chase card payee persisted]")
    chase = ChaseCreditCardAdapter()
    chase_raw = extract_pdf_to_raw(
        file_path=chase_pdf,
        adapter_id=chase.adapter_id,
        adapter_version=chase.adapter_version,
        original_basename=chase_pdf.name,
    )
    account, statement, summary, txns = chase.parse_canonical(chase_raw)
    if not txns or any(t.payee is None for t in txns):
        _fail("Chase parse should yield non-null payees for printed merchants")
        failures += 1
    else:
        _pass(f"Chase parse: {len(txns)} txns with payees (e.g. {txns[0].payee!r})")

    with psycopg.connect(db_url) as conn:
        persist_raw_extraction(conn, chase_raw)
        persist_canonical_statement(conn, account, statement, summary, txns)
        conn.commit()
        loaded_bundle = get_canonical_statement(conn, statement.statement_id)

    if loaded_bundle is None:
        _fail("Chase statement missing after persist")
        failures += 1
        loaded = []
    else:
        _stmt, _sum, loaded = loaded_bundle

    if not loaded or any(t.payee is None for t in loaded):
        _fail("Chase round-trip lost payee column")
        failures += 1
    elif any(t.payee != t.description for t in loaded):
        _fail("Chase payee should equal printed description")
        failures += 1
    else:
        _pass(f"Chase DB round-trip: {len(loaded)} payees intact")

    # --- Happy: Schwab opaque → NULL payee ---
    print("\n[Happy: Schwab opaque rows persist payee=NULL]")
    schwab = SchwabCheckingAdapter()
    schwab_raw = extract_pdf_to_raw(
        file_path=schwab_pdf,
        adapter_id=schwab.adapter_id,
        adapter_version=schwab.adapter_version,
        original_basename=schwab_pdf.name,
    )
    s_account, s_statement, s_summary, s_txns = schwab.parse_canonical(schwab_raw)
    if not s_txns or any(t.payee is not None for t in s_txns):
        _fail("Schwab opaque fixture should yield payee=None for all rows")
        failures += 1
    else:
        _pass(f"Schwab parse: {len(s_txns)} opaque rows, payee=None")

    with psycopg.connect(db_url) as conn:
        persist_raw_extraction(conn, schwab_raw)
        persist_canonical_statement(conn, s_account, s_statement, s_summary, s_txns)
        conn.commit()
        s_bundle = get_canonical_statement(conn, s_statement.statement_id)

    if s_bundle is None:
        _fail("Schwab statement missing after persist")
        failures += 1
        s_loaded = []
    else:
        _s2, _sum2, s_loaded = s_bundle

    if not s_loaded or any(t.payee is not None for t in s_loaded):
        _fail("Schwab round-trip should keep payee NULL (no invent)")
        failures += 1
    else:
        _pass(f"Schwab DB round-trip: {len(s_loaded)} rows payee IS NULL")

    # --- Negative: empty payee fails loud ---
    print("\n[Negative: empty payee rejected]")
    for bad in ("", "  "):
        try:
            CanonicalTransaction(
                statement_id=uuid4(),
                post_date=datetime.date(2026, 10, 1),
                amount_cents=-100,
                description="Purchase",
                transaction_category=TransactionCategory.PURCHASE,
                payee=bad,
            )
            _fail(f"Expected ValidationError for payee={bad!r}")
            failures += 1
        except ValidationError:
            _pass(f"Loud reject for payee={bad!r}")

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
