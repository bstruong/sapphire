#!/usr/bin/env python3
"""QA: Chase credit card adapter (exclusive match + integer-cent reconcile).

Verifies:
1. Synthetic Chase card fixture matches chase_credit_card only.
2. Chase checking still matches chase_depository only.
3. Standard card still matches standard_credit_card only.
4. Canonical parse reconciles summary buckets and net change (integer cents).
5. Unknown bank fails loud with AdapterRegistryError.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


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
    print("QA: Chase credit card adapter")
    print("=" * 70)
    failures = 0

    from app.adapters.chase import ChaseDepositoryAdapter
    from app.adapters.chase_credit_card import ChaseCreditCardAdapter
    from app.adapters.credit_card import StandardCreditCardAdapter
    from app.adapters.registry import get_default_registry
    from app.extraction.core import extract_pdf_to_raw
    from app.models.enums import AccountType
    from app.models.exceptions import AdapterRegistryError

    registry = get_default_registry()

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        card_pdf = _render_pdf("chase_credit_card_happy", tmp_path / "chase_card.pdf")
        checking_pdf = _render_pdf(
            "chase_checking_happy", tmp_path / "chase_checking.pdf"
        )
        std_card_pdf = _render_pdf("card_happy", tmp_path / "std_card.pdf")

        print("\n[Step 1: Exclusive matching]")
        if (
            ChaseCreditCardAdapter().matches(card_pdf)
            and not ChaseDepositoryAdapter().matches(card_pdf)
            and not StandardCreditCardAdapter().matches(card_pdf)
            and registry.match(card_pdf).adapter_id == "chase_credit_card"
        ):
            _pass("Chase card fixture → chase_credit_card exclusively")
        else:
            _fail("Chase card fixture matching failed exclusivity")
            failures += 1

        if registry.match(checking_pdf).adapter_id == "chase_depository":
            _pass("Chase checking fixture → chase_depository")
        else:
            _fail("Chase checking fixture did not match chase_depository")
            failures += 1

        if registry.match(std_card_pdf).adapter_id == "standard_credit_card":
            _pass("Standard card fixture → standard_credit_card")
        else:
            _fail("Standard card fixture matching failed")
            failures += 1

        print("\n[Step 2: Canonical parse + integer cents]")
        adapter = ChaseCreditCardAdapter()
        raw = extract_pdf_to_raw(
            file_path=card_pdf,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename="chase_card.pdf",
        )
        account, statement, summary, txns = adapter.parse_canonical(raw)
        fixture = json.loads(
            (FIXTURES / "chase_credit_card_happy.json").read_text(encoding="utf-8")
        )
        cents = fixture["summary_cents"]
        if (
            account.institution == "Chase"
            and account.account_type == AccountType.CREDIT_CARD
            and account.account_mask == "*0001"
            and statement.opening_balance_cents == cents["previous_balance_cents"]
            and statement.closing_balance_cents == cents["new_balance_cents"]
            and summary.purchases_cents == cents["purchases_cents"]
            and sum(t.amount_cents for t in txns) == statement.net_change_cents
        ):
            _pass("Canonical Chase card ledger reconciles in integer cents")
        else:
            _fail("Canonical parse / reconciliation mismatch")
            failures += 1

        print("\n[Step 3: Loud negative — unknown bank]")
        from reportlab.pdfgen import canvas

        unknown = tmp_path / "unknown.pdf"
        c = canvas.Canvas(str(unknown))
        c.drawString(50, 750, "SOME OTHER BANK CREDIT CARD Card Number: *9999")
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
