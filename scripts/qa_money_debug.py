#!/usr/bin/env python3
"""QA: Money conversion debug toolkit.

Happy: parse/format/fixture traces; vault RTA excludes brokerage.
Negative: PII scrubbed from reports; malformed money fails loud.
Manual UI QA: not applicable (no user-visible UI).

Always uses sapphire_test — never truncates dogfood /sapphire.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def main() -> int:
    print("QA: Money conversion debug toolkit")
    print("Manual UI QA: not applicable (no user-visible UI).")
    failures = 0

    test_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://postgres:sapphire@localhost:54329/sapphire_test",
    )
    if not test_url.rstrip("/").endswith(("_test", "_pytest")):
        _fail("TEST_DATABASE_URL must end with _test or _pytest")
        return 1
    os.environ["TEST_DATABASE_URL"] = test_url
    os.environ["DATABASE_URL"] = test_url

    from app.db.connection import init_db
    from app.debug.money_trace import (
        scrub_pii,
        trace_fixture_or_pdf,
        trace_format,
        trace_parse,
        trace_vault_aggregates,
    )
    from app.pipeline.ingest_service import IngestSuccess, ingest_pdf_path

    # --- parse / format ---
    step = trace_parse("$1,295.00")
    if step.ok and step.cents == 129_500:
        _pass("parse $1,295.00 → 129500 cents")
    else:
        _fail(f"parse happy failed: {step.line()}")
        failures += 1

    bad = trace_parse("12.3")
    if not bad.ok:
        _pass("malformed token fails loud")
    else:
        _fail("malformed token was accepted")
        failures += 1

    fmt = trace_format(129_500)
    if fmt.ok and fmt.usd == "$1,295.00":
        _pass("format round-trip 129500 → $1,295.00")
    else:
        _fail(f"format failed: {fmt.line()}")
        failures += 1

    # --- PII scrub ---
    scrubbed = scrub_pii(
        "SSN 123-45-6789 acct 123456789012 at 99 Oak Street paid $50.00"
    )
    if (
        "123-45-6789" not in scrubbed
        and "123456789012" not in scrubbed
        and "Oak Street" not in scrubbed
        and "$50.00" in scrubbed
        and "[MASKED]" in scrubbed
    ):
        _pass("scrub_pii removes SSN/account/street; keeps USD")
    else:
        _fail(f"scrub_pii leak or over-scrub: {scrubbed!r}")
        failures += 1

    # --- fixture ---
    report = trace_fixture_or_pdf(FIXTURES / "chase_checking_happy.json")
    text = report.to_text()
    if (
        report.ok
        and report.statements
        and report.statements[0].closing_balance_cents == 129_500
        and "SYNTHETIC Payroll" not in text
        and "page_text" not in text.lower()
    ):
        _pass("fixture chase_checking_happy → closing 129500 without body dump")
    else:
        _fail("fixture chase_checking_happy trace failed or leaked body")
        failures += 1

    brok = trace_fixture_or_pdf(FIXTURES / "brokerage_cash_happy.json")
    if brok.ok and any(
        "_parse_cents" in w or "legacy" in w.lower() for w in brok.warnings
    ):
        _pass("brokerage fixture warns on legacy _parse_cents")
    else:
        _fail("brokerage fixture missing legacy parser warning")
        failures += 1

    # --- vault RTA excludes brokerage ---
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"database unavailable: {type(exc).__name__}")
        print(f"\nResult: [FAIL] ({failures + 1} check(s) failed)")
        return 1

    import json
    import re

    import psycopg
    from reportlab.pdfgen import canvas

    def render(name: str, target: Path) -> Path:
        data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        lines = data["raw_text"].split("\n")
        c = canvas.Canvas(str(target))
        y = 750
        for line in lines:
            line = re.sub(r"(\t|\s{2,})", " | ", line)
            c.drawString(50, y, line)
            y -= 15
            if y < 50:
                c.showPage()
                y = 750
        c.save()
        return target

    with psycopg.connect(test_url) as conn:
        conn.execute(
            """
            TRUNCATE TABLE envelopes, raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        chk = render("chase_checking_happy", tmp_path / "chk.pdf")
        brk = render("brokerage_cash_happy", tmp_path / "brk.pdf")
        r1 = ingest_pdf_path(chk, chk.name)
        r2 = ingest_pdf_path(brk, brk.name)
        if not isinstance(r1, IngestSuccess) or not isinstance(r2, IngestSuccess):
            d1 = getattr(r1, "detail", type(r1).__name__)
            d2 = getattr(r2, "detail", type(r2).__name__)
            c1 = getattr(r1, "error_code", "")
            c2 = getattr(r2, "error_code", "")
            _fail(f"ingest for vault QA failed: {c1}:{d1!s} | {c2}:{d2!s}"[:300])
            failures += 1
        else:
            agg = trace_vault_aggregates(test_url)
            if (
                agg.ok
                and agg.liquid_closing_cents == 129_500
                and agg.brokerage_closing_cents == 1_390_425
                and agg.ready_to_assign_cents == 129_500
            ):
                _pass("vault RTA equals liquid only (brokerage excluded)")
            else:
                _fail(
                    "vault aggregate mismatch: "
                    f"liquid={agg.liquid_closing_cents} "
                    f"brok={agg.brokerage_closing_cents} "
                    f"rta={agg.ready_to_assign_cents} err={agg.error}"
                )
                failures += 1

    # CLI smoke (subprocess — scripts/ is not an import package)
    import subprocess

    def _cli(*cli_args: str) -> int:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "debug_money.py"), *cli_args],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.returncode

    if _cli("parse", "$10.00") == 0 and _cli("format", "1000") == 0:
        _pass("debug_money CLI parse/format exit 0")
    else:
        _fail("debug_money CLI smoke failed")
        failures += 1

    if _cli("diff-stage", "--expected", "1", "--actual", "2", "--label", "x") == 1:
        _pass("debug_money CLI diff-stage fails loud on mismatch")
    else:
        _fail("diff-stage should exit 1 on mismatch")
        failures += 1

    if failures:
        print(f"\nResult: [FAIL] ({failures} check(s) failed)")
        return 1
    print("\nResult: [PASS] money debug toolkit checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
