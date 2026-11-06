"""Tests for the Chase credit-card adapter."""

from __future__ import annotations

import json
import re
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


def _render_pdf_from_fixture(fixture_name: str, target_path: Path) -> Path:
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
    def test_matches_chase_card_fixture(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture(
            "chase_credit_card_happy", tmp_path / "chase_card.pdf"
        )
        assert ChaseCreditCardAdapter().matches(pdf) is True
        assert StandardCreditCardAdapter().matches(pdf) is False
        assert ChaseDepositoryAdapter().matches(pdf) is False

    def test_registry_exclusive_chase_card(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture(
            "chase_credit_card_happy", tmp_path / "chase_card.pdf"
        )
        matched = get_default_registry().match(pdf)
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


class TestChaseCreditCardParse:
    def test_parse_canonical_happy_path(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture(
            "chase_credit_card_happy", tmp_path / "chase_card.pdf"
        )
        adapter = ChaseCreditCardAdapter()
        raw = extract_pdf_to_raw(
            file_path=pdf,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename="chase_card.pdf",
        )
        account, statement, summary, txns = adapter.parse_canonical(raw)
        assert account.institution == "Chase"
        assert account.account_type == AccountType.CREDIT_CARD
        assert account.account_mask == "*0001"
        fixture = json.loads(
            (FIXTURES_DIR / "chase_credit_card_happy.json").read_text(encoding="utf-8")
        )
        cents = fixture["summary_cents"]
        assert statement.opening_balance_cents == cents["previous_balance_cents"]
        assert statement.closing_balance_cents == cents["new_balance_cents"]
        assert summary.purchases_cents == cents["purchases_cents"]
        assert summary.payments_credits_cents == cents["payments_credits_cents"]
        assert len(txns) >= 2
        assert sum(t.amount_cents for t in txns) == statement.net_change_cents

    def test_unmatched_bank_fails_registry(self, tmp_path: Path) -> None:
        pdf = _render_lines(
            tmp_path / "unknown.pdf",
            ["SOME OTHER BANK", "CREDIT CARD", "Card Number: *9999"],
        )
        with pytest.raises(AdapterRegistryError):
            get_default_registry().match(pdf)
