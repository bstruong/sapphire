"""Tests for Charles Schwab Bank checking adapter (redacted PDF)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from app.adapters.chase import ChaseDepositoryAdapter
from app.adapters.registry import get_default_registry
from app.adapters.schwab import SchwabAdapter
from app.adapters.schwab_checking import SchwabCheckingAdapter
from app.extraction.core import extract_pdf_to_raw
from app.models.enums import AccountType
from app.models.exceptions import AdapterRegistryError

FIXTURES_DIR = Path(__file__).parent / "fixtures"
REDACTED_PDF = FIXTURES_DIR / "redacted" / "schwab_checking_sample.pdf"


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


class TestSchwabCheckingMatching:
    def test_matches_redacted_bank_pdf(self) -> None:
        assert REDACTED_PDF.is_file()
        assert SchwabCheckingAdapter().matches(REDACTED_PDF) is True
        assert SchwabAdapter().matches(REDACTED_PDF) is False
        assert ChaseDepositoryAdapter().matches(REDACTED_PDF) is False

    def test_registry_exclusive_schwab_checking(self) -> None:
        assert (
            get_default_registry().match(REDACTED_PDF).adapter_id == "schwab_checking"
        )

    def test_brokerage_fixture_still_schwab_brokerage(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture("brokerage_cash_happy", tmp_path / "brok.pdf")
        assert SchwabCheckingAdapter().matches(pdf) is False
        assert get_default_registry().match(pdf).adapter_id == "schwab_brokerage"

    def test_does_not_match_chase_checking(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture(
            "chase_checking_happy", tmp_path / "chase_checking.pdf"
        )
        assert SchwabCheckingAdapter().matches(pdf) is False


class TestSchwabCheckingPeriodDates:
    def test_compact_month_day_range_header(self, tmp_path: Path) -> None:
        """Real Schwab Bank chrome uses ``August 1-31, 2026`` (not slash ranges)."""
        pdf = _render_lines(
            tmp_path / "schwab_period.pdf",
            [
                "Charles Schwab Bank Investor Checking Member FDIC",
                "Account Number Statement Period",
                "[ACCOUNT_1] August 1-31, 2026",
                "Beginning Balance $100.00",
                "Deposits and Credits $0.00",
                "Interest Paid $0.00",
                "Withdrawals and Other Debits $0.00",
                "Other Fees $0.00",
                "Ending Balance $100.00",
            ],
        )
        adapter = SchwabCheckingAdapter()
        assert adapter.matches(pdf) is True
        raw = extract_pdf_to_raw(
            file_path=pdf,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=pdf.name,
        )
        mask, acc_type, start, end = adapter.parse_header(raw)
        assert mask == "[ACCOUNT_1]"
        assert acc_type == AccountType.CHECKING
        assert start.isoformat() == "2026-08-01"
        assert end.isoformat() == "2026-08-31"

    def test_compact_month_day_range_with_spaces(self, tmp_path: Path) -> None:
        pdf = _render_lines(
            tmp_path / "schwab_period_spaces.pdf",
            [
                "Charles Schwab Bank Checking",
                "Account Number: [ACCOUNT_9]",
                "Statement Period August 1 - 31, 2026",
            ],
        )
        adapter = SchwabCheckingAdapter()
        raw = extract_pdf_to_raw(
            file_path=pdf,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=pdf.name,
        )
        _mask, _type, start, end = adapter.parse_header(raw)
        assert start.isoformat() == "2026-08-01"
        assert end.isoformat() == "2026-08-31"


class TestSchwabCheckingParse:
    def test_parse_redacted_pdf_reconciles(self) -> None:
        adapter = SchwabCheckingAdapter()
        raw = extract_pdf_to_raw(
            file_path=REDACTED_PDF,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=REDACTED_PDF.name,
        )
        account, statement, summary, txns = adapter.parse_canonical(raw)
        assert account.institution == "Charles Schwab Bank"
        assert account.account_type == AccountType.CHECKING
        assert account.account_mask.startswith("[")
        assert statement.opening_balance_cents == 624078
        assert statement.closing_balance_cents == 603485
        assert summary.deposits_cents == 505000
        assert summary.withdrawals_cents == 525595
        assert summary.interest_paid_cents == 2
        assert summary.fees_cents == 0
        assert len(txns) == 15
        assert sum(t.amount_cents for t in txns) == statement.net_change_cents

    def test_unmatched_bank_fails_registry(self, tmp_path: Path) -> None:
        pdf = _render_lines(
            tmp_path / "unknown.pdf",
            ["SOME OTHER BANK", "CHECKING", "Account Number: *9999"],
        )
        with pytest.raises(AdapterRegistryError):
            get_default_registry().match(pdf)
