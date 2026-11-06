"""Tests for the Chase credit-card adapter (redacted PDF + mutual exclusion)."""

from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.adapters.chase import ChaseDepositoryAdapter
from app.adapters.chase_credit_card import ChaseCreditCardAdapter
from app.adapters.credit_card import StandardCreditCardAdapter
from app.adapters.registry import get_default_registry
from app.extraction.core import extract_pdf_to_raw
from app.models.enums import AccountType
from app.models.exceptions import AdapterRegistryError

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REDACTED_CARD_PDF = FIXTURES_DIR / "redacted" / "chase_credit_card_sample.pdf"


def _render_pdf_from_fixture(fixture_name: str, target_path: Path) -> Path:
    import json
    import re

    data = json.loads(
        (FIXTURES_DIR / f"{fixture_name}.json").read_text(encoding="utf-8")
    )
    lines = data["raw_text"].split("\n")
    c = canvas.Canvas(str(target_path))
    y = 750
    for line in lines:
        if "\t" in line or "    " in line:
            line = re.sub(r"(\t|\s{2,})", " | ", line)
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    return target_path


def _render_lines(path: Path, lines: list[str]) -> Path:
    c = canvas.Canvas(str(path))
    y = 750
    for line in lines:
        c.drawString(50, y, line)
        y -= 15
    c.save()
    return path


class TestChaseCreditCardMatching:
    def test_matches_redacted_chase_card_fixture(self) -> None:
        assert REDACTED_CARD_PDF.is_file()
        assert ChaseCreditCardAdapter().matches(REDACTED_CARD_PDF) is True
        assert StandardCreditCardAdapter().matches(REDACTED_CARD_PDF) is False
        assert ChaseDepositoryAdapter().matches(REDACTED_CARD_PDF) is False

    def test_registry_exclusive_chase_card(self) -> None:
        matched = get_default_registry().match(REDACTED_CARD_PDF)
        assert matched.adapter_id == "chase_credit_card"

    def test_does_not_match_standard_card(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture("card_happy", tmp_path / "std_card.pdf")
        assert ChaseCreditCardAdapter().matches(pdf) is False
        assert get_default_registry().match(pdf).adapter_id == "standard_credit_card"

    def test_does_not_match_chase_checking(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture(
            "chase_checking_happy", tmp_path / "chase_checking.pdf"
        )
        assert ChaseCreditCardAdapter().matches(pdf) is False
        assert get_default_registry().match(pdf).adapter_id == "chase_depository"

    def test_depository_rejects_card_markers(self, tmp_path: Path) -> None:
        pdf = _render_lines(
            tmp_path / "chase_card_like.pdf",
            ["CHASE", "CREDIT CARD STATEMENT", "CHECKING"],
        )
        assert ChaseDepositoryAdapter().matches(pdf) is False

    def test_negative_unit_pdf_without_chase_chrome(self, tmp_path: Path) -> None:
        pdf = _render_lines(
            tmp_path / "not_chase.pdf",
            [
                "SOME OTHER BANK",
                "CREDIT CARD STATEMENT",
                "ACCOUNT SUMMARY",
                "Previous Balance: $10.00",
            ],
        )
        assert ChaseCreditCardAdapter().matches(pdf) is False


class TestChaseCreditCardParse:
    def test_parse_redacted_pdf_reconciles(self) -> None:
        adapter = ChaseCreditCardAdapter()
        raw = extract_pdf_to_raw(
            file_path=REDACTED_CARD_PDF,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=REDACTED_CARD_PDF.name,
        )
        account, statement, summary, txns = adapter.parse_canonical(raw)
        assert account.institution == "Chase"
        assert account.account_type == AccountType.CREDIT_CARD
        assert account.account_mask.startswith("[")
        assert statement.opening_balance_cents == 3259
        assert statement.closing_balance_cents == 3282
        assert summary.previous_balance_cents == 3259
        assert summary.payments_credits_cents == 3259
        assert summary.purchases_cents == 3282
        assert summary.new_balance_cents == 3282
        assert len(txns) == 3
        assert sum(t.amount_cents for t in txns) == statement.net_change_cents

    def test_schema_markers_do_not_require_account_summary_heading(
        self, tmp_path: Path
    ) -> None:
        """Raw Chase extracts often lack the ACCOUNT SUMMARY heading string."""
        pdf = _render_lines(
            tmp_path / "chase_no_heading.pdf",
            [
                "Chase Ultimate Rewards",
                "Account Number: [CREDIT_CARD_1]",
                "Opening/Closing Date 09/26/26 - 10/25/26",
                "Previous Balance $32.59",
                "Payment, Credits -$32.59",
                "Purchases +$32.82",
                "Cash Advances $0.00",
                "Balance Transfers $0.00",
                "Fees Charged $0.00",
                "Interest Charged $0.00",
                "New Balance $32.82",
                "Payment Due Date: 11/19/26",
                "Minimum Payment Due: $32.82",
                "Merchant Name or Transaction Description $ Amount",
                "10/19 AUTOMATIC PAYMENT - THANK YOU -32.59",
                "10/04 SYNTHETIC MERCHANT A 22.83",
                "10/12 SYNTHETIC MERCHANT B 9.99",
                "TRANSACTIONS THIS CYCLE $0.23",
            ],
        )
        adapter = ChaseCreditCardAdapter()
        assert adapter.matches(pdf) is True
        raw = extract_pdf_to_raw(
            file_path=pdf,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=pdf.name,
        )
        _, statement, summary, txns = adapter.parse_canonical(raw)
        assert statement.opening_balance_cents == 3259
        assert statement.closing_balance_cents == 3282
        assert summary.purchases_cents == 3282
        assert len(txns) == 3

    def test_unmatched_bank_fails_registry(self, tmp_path: Path) -> None:
        pdf = _render_lines(
            tmp_path / "unknown.pdf",
            ["SOME OTHER BANK", "CREDIT CARD", "Card Number: *9999"],
        )
        with pytest.raises(AdapterRegistryError):
            get_default_registry().match(pdf)
