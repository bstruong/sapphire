#!/usr/bin/env python3
"""QA: Schwab Bank checking adapter against redacted PDF.

Verifies:
1. Redacted Schwab Bank PDF matches schwab_checking only (not brokerage).
2. Brokerage fixture still matches schwab_brokerage.
3. Canonical parse reconciles integer-cent buckets and net change.
4. Cross-month line-broken statement periods parse (Jan 31 → Feb 27).
5. Unknown bank fails loud with AdapterRegistryError.

Manual UI QA:
  uv run uvicorn app.api.app:create_app --factory --host 127.0.0.1 --port 8000
  Open http://127.0.0.1:8000/dashboard
  Upload tests/fixtures/redacted/schwab_checking_sample.pdf
  Expect Vault Ledger (not ADAPTER_MATCH_FAILED / brokerage schema errors).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
REDACTED = FIXTURES / "redacted" / "schwab_checking_sample.pdf"


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _render_pdf(fixture_name: str, target: Path) -> Path:
    from reportlab.pdfgen import canvas

    data = json.loads((FIXTURES / f"{fixture_name}.json").read_text(encoding="utf-8"))
    c = canvas.Canvas(str(target))
    y = 750
    for line in data["raw_text"].split("\n"):
        line = re.sub(r"(\t|\s{2,})", " | ", line)
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    return target


def main() -> int:
    print("=" * 70)
    print("QA: Schwab checking adapter (redacted PDF)")
    print("=" * 70)
    failures = 0

    from app.adapters.registry import get_default_registry
    from app.adapters.schwab import SchwabAdapter
    from app.adapters.schwab_checking import SchwabCheckingAdapter
    from app.extraction.core import extract_pdf_to_raw
    from app.models.enums import AccountType
    from app.models.exceptions import AdapterRegistryError

    registry = get_default_registry()

    if not REDACTED.is_file():
        _fail(f"Missing redacted fixture: {REDACTED}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        brok_pdf = _render_pdf("brokerage_cash_happy", tmp_path / "brok.pdf")

        print("\n[Step 1: Exclusive matching]")
        if (
            SchwabCheckingAdapter().matches(REDACTED)
            and not SchwabAdapter().matches(REDACTED)
            and registry.match(REDACTED).adapter_id == "schwab_checking"
        ):
            _pass("Redacted Schwab Bank PDF → schwab_checking exclusively")
        else:
            _fail("Schwab checking matching failed exclusivity")
            failures += 1

        if registry.match(brok_pdf).adapter_id == "schwab_brokerage":
            _pass("Brokerage fixture → schwab_brokerage")
        else:
            _fail("Brokerage fixture did not match schwab_brokerage")
            failures += 1

        print("\n[Step 2: Canonical parse + integer cents]")
        adapter = SchwabCheckingAdapter()
        raw = extract_pdf_to_raw(
            file_path=REDACTED,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=REDACTED.name,
        )
        account, statement, summary, txns = adapter.parse_canonical(raw)
        if (
            account.institution == "Charles Schwab Bank"
            and account.account_type == AccountType.CHECKING
            and statement.opening_balance_cents == 624078
            and statement.closing_balance_cents == 603485
            and summary.deposits_cents == 505000
            and summary.withdrawals_cents == 525595
            and summary.interest_paid_cents == 2
            and len(txns) == 15
            and sum(t.amount_cents for t in txns) == statement.net_change_cents
        ):
            _pass("Canonical Schwab checking ledger reconciles in integer cents")
        else:
            _fail("Canonical parse / reconciliation mismatch")
            failures += 1

        print("\n[Step 3: Cross-month period chrome (line-broken)]")
        from reportlab.pdfgen import canvas

        cross = tmp_path / "cross_month.pdf"
        c = canvas.Canvas(str(cross))
        y = 750
        for line in (
            "Charles Schwab Bank Investor Checking Member FDIC",
            "Statement Period",
            "Account Number January 31, 2026 to",
            "SYNTHETIC HOLDER [ACCOUNT_1] February 27, 2026",
            "Beginning Balance $100.00",
            "Deposits and Credits $0.00",
            "Interest Paid $0.00",
            "Withdrawals and Other Debits $0.00",
            "Other Fees $0.00",
            "Ending Balance $100.00",
        ):
            c.drawString(50, y, line)
            y -= 15
        c.save()
        cross_adapter = SchwabCheckingAdapter()
        cross_raw = extract_pdf_to_raw(
            file_path=cross,
            adapter_id=cross_adapter.adapter_id,
            adapter_version=cross_adapter.adapter_version,
            original_basename=cross.name,
        )
        _mask, _atype, start, end = cross_adapter.parse_header(cross_raw)
        if start.isoformat() == "2026-01-31" and end.isoformat() == "2026-02-27":
            _pass("Cross-month line-broken period parses (Jan 31 → Feb 27)")
        else:
            _fail(f"Cross-month period mismatch: {start} → {end}")
            failures += 1

        print("\n[Step 4: Loud negative — unknown bank]")
        unknown = tmp_path / "unknown.pdf"
        c = canvas.Canvas(str(unknown))
        c.drawString(50, 750, "SOME OTHER BANK CHECKING Account Number: *9999")
        c.save()
        try:
            registry.match(unknown)
            _fail("Unknown bank was not rejected")
            failures += 1
        except AdapterRegistryError:
            _pass("Unknown bank rejected with AdapterRegistryError")

    print("\n" + "=" * 70)
    if failures == 0:
        print("ALL VERIFICATIONS PASSED [PASS]")
        print("=" * 70)
        return 0
    print(f"{failures} VERIFICATION(S) FAILED [FAIL]")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    sys.exit(main())
