#!/usr/bin/env python3
"""QA: Spend-label rules matcher (bulk suggest B1).

Happy: normalize + insert + exact match → utilities slug.
Negative: empty payee / duplicate pattern / unknown category fail loud.

Uses sapphire_test only — refuses dogfood wipe.
"""

from __future__ import annotations

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
            TRUNCATE TABLE spend_label_rules, transaction_spend_labels,
                           envelopes, raw_tokens, raw_pages,
                           transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()


def main() -> int:
    print("=" * 70)
    print("QA: Spend-label rules (normalize + exact match)")
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

    from app.db.connection import init_db
    from app.models.exceptions import TokenError
    from app.spend.categories import list_spend_categories
    from app.spend.rules import (
        insert_spend_label_rule,
        match_payee_rule,
        normalize_payee,
    )

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"init_db failed: {exc}")
        return 1

    _truncate(db_url)

    if normalize_payee("  SYNTHETIC  PG&E ") != "synthetic pg&e":
        _fail("normalize_payee")
        failures += 1
    else:
        _pass("normalize_payee lower + collapse whitespace")

    try:
        normalize_payee("   ")
        _fail("empty payee did not fail loud")
        failures += 1
    except TokenError:
        _pass("empty payee fails loud")

    with psycopg.connect(db_url) as conn:
        cats = {c.slug: c for c in list_spend_categories(conn)}
        rule = insert_spend_label_rule(
            conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        conn.commit()
        hit = match_payee_rule(conn, "  Synthetic  PG&E ")
        if hit is None or hit.slug != "utilities" or hit.rule_id != rule.rule_id:
            _fail(f"exact match failed: {hit}")
            failures += 1
        else:
            _pass("exact normalized match → utilities")

        if match_payee_rule(conn, "SYNTHETIC UNKNOWN") is not None:
            _fail("unknown payee should miss")
            failures += 1
        else:
            _pass("unknown payee returns no match")

        try:
            insert_spend_label_rule(
                conn,
                payee_pattern="  synthetic   pg&e ",
                spend_category_id=cats["dining"].spend_category_id,
            )
            _fail("duplicate pattern did not fail loud")
            failures += 1
        except TokenError as exc:
            if "SPEND_LABEL_RULE_AMBIGUOUS" in str(exc):
                _pass("duplicate normalized pattern fails loud")
            else:
                _fail(f"unexpected duplicate error: {exc}")
                failures += 1

        try:
            insert_spend_label_rule(
                conn,
                payee_pattern="SYNTHETIC X",
                spend_category_id=uuid4(),
            )
            _fail("unknown category did not fail loud")
            failures += 1
        except TokenError as exc:
            if "SPEND_CATEGORY" in str(exc):
                _pass("unknown category fails loud")
            else:
                _fail(f"unexpected category error: {exc}")
                failures += 1

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
