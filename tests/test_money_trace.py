"""Money conversion debug tracers: cents OK, PII scrubbed."""

from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg
import pytest
from reportlab.pdfgen import canvas

from app.debug.money_trace import (
    scrub_pii,
    trace_diff_stage,
    trace_fixture_or_pdf,
    trace_format,
    trace_parse,
    trace_vault_aggregates,
)
from app.models.exceptions import TokenError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _render_pdf_from_fixture(fixture_name: str, target_path: Path) -> Path:
    data = json.loads(
        (FIXTURES_DIR / f"{fixture_name}.json").read_text(encoding="utf-8")
    )
    lines = data["raw_text"].split("\n")
    c = canvas.Canvas(str(target_path))
    y = 750
    for line in lines:
        line = re.sub(r"(\t|\s{2,})", " | ", line)
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    return target_path


class TestScrubPii:
    def test_preserves_usd_amounts_and_token_masks(self) -> None:
        text = "Opening $1,295.00 for [ACCOUNT_1] and *0000"
        out = scrub_pii(text)
        assert "$1,295.00" in out
        assert "[ACCOUNT_1]" in out
        assert "*0000" in out

    def test_scrubs_ssn_and_long_account_digits(self) -> None:
        text = "SSN 123-45-6789 acct 123456789012 transfer $50.00"
        out = scrub_pii(text)
        assert "123-45-6789" not in out
        assert "123456789012" not in out
        assert "$50.00" in out
        assert "[MASKED]" in out

    def test_scrubs_street_address_lines(self) -> None:
        text = "123 Main Street Apt 4\nPayment $10.00"
        out = scrub_pii(text)
        assert "Main Street" not in out
        assert "$10.00" in out

    def test_scrub_rejects_non_str(self) -> None:
        with pytest.raises(TokenError):
            scrub_pii(12345)  # type: ignore[arg-type]


class TestTraceParse:
    def test_happy_parse_returns_cents(self) -> None:
        step = trace_parse("$1,295.00")
        assert step.ok is True
        assert step.cents == 129_500
        assert step.sign == "+"
        assert "$1,295.00" in step.detail

    def test_negative_parse_sign(self) -> None:
        step = trace_parse("($12.34)")
        assert step.ok is True
        assert step.cents == -1234
        assert step.sign == "-"

    def test_malformed_fails_loud(self) -> None:
        step = trace_parse("12.3")
        assert step.ok is False
        assert step.cents is None
        assert "TokenError" in step.detail or "decimal" in step.detail.lower()


class TestTraceFormat:
    def test_round_trip_positive(self) -> None:
        step = trace_format(129_500)
        assert step.ok is True
        assert step.usd == "$1,295.00"
        assert step.cents == 129_500

    def test_round_trip_zero_and_negative(self) -> None:
        zero = trace_format(0)
        assert zero.ok is True
        assert zero.usd == "$0.00"
        neg = trace_format(-1234)
        assert neg.ok is True
        assert neg.usd == "-$12.34"

    def test_rejects_non_int(self) -> None:
        step = trace_format(12.5)  # type: ignore[arg-type]
        assert step.ok is False


class TestTraceDiffStage:
    def test_match_passes(self) -> None:
        step = trace_diff_stage(expected=100, actual=100, label="net")
        assert step.ok is True

    def test_mismatch_fails_with_cents(self) -> None:
        step = trace_diff_stage(expected=100, actual=200, label="net")
        assert step.ok is False
        assert "100" in step.detail
        assert "200" in step.detail

    def test_non_int_fails_loud(self) -> None:
        step = trace_diff_stage(expected=1.5, actual=1, label="x")  # type: ignore[arg-type]
        assert step.ok is False


class TestTraceFixture:
    def test_checking_fixture_extracts_cents_without_page_dump(
        self, tmp_path: Path
    ) -> None:
        pdf = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "c.pdf")
        report = trace_fixture_or_pdf(pdf)
        assert report.ok is True
        assert report.adapter_id
        text = report.to_text()
        assert "129500" in text or "$1,295.00" in text
        assert "page_text" not in text.lower()
        # Must not dump raw fixture narrative bodies
        assert "SYNTHETIC Payroll" not in text

    def test_json_fixture_path_works(self) -> None:
        path = FIXTURES_DIR / "chase_checking_happy.json"
        report = trace_fixture_or_pdf(path)
        assert report.ok is True
        assert report.statements
        stmt = report.statements[0]
        assert stmt.closing_balance_cents == 129_500
        assert stmt.txn_sum_cents == stmt.net_change_cents

    def test_brokerage_warns_legacy_parser(self, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture("brokerage_cash_happy", tmp_path / "b.pdf")
        report = trace_fixture_or_pdf(pdf)
        assert report.ok is True
        assert report.adapter_id == "schwab_brokerage"
        assert any(
            "legacy" in w.lower() or "_parse_cents" in w for w in report.warnings
        )

    def test_missing_file_and_bad_suffix(self, tmp_path: Path) -> None:
        missing = trace_fixture_or_pdf(tmp_path / "nope.pdf")
        assert missing.ok is False
        (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
        bad = trace_fixture_or_pdf(tmp_path / "x.txt")
        assert bad.ok is False

    def test_card_fixture_credit_domain(self) -> None:
        report = trace_fixture_or_pdf(FIXTURES_DIR / "card_happy.json")
        assert report.ok is True
        assert report.statements
        assert report.statements[0].account_domain == "revolving_credit"
        assert report.statements[0].liquidity_label == "Liability"


class TestTraceVaultAggregates:
    def test_rta_excludes_brokerage(
        self, postgres_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", postgres_url)
        from app.db.connection import init_db
        from app.pipeline.ingest_service import ingest_pdf_path

        init_db()
        with psycopg.connect(postgres_url) as conn:
            conn.execute(
                """
                TRUNCATE TABLE envelopes, raw_tokens, raw_pages, transactions, holdings,
                               statement_depository_summaries, statement_credit_summaries,
                               statement_brokerage_summaries, statements, accounts,
                               extraction_runs, raw_payloads CASCADE;
                """
            )
            conn.commit()

        checking = _render_pdf_from_fixture(
            "chase_checking_happy", tmp_path / "chk.pdf"
        )
        brokerage = _render_pdf_from_fixture(
            "brokerage_cash_happy", tmp_path / "brk.pdf"
        )
        from app.pipeline.ingest_service import IngestSuccess

        result_c = ingest_pdf_path(checking, checking.name)
        result_b = ingest_pdf_path(brokerage, brokerage.name)
        assert isinstance(result_c, IngestSuccess)
        assert isinstance(result_b, IngestSuccess)

        agg = trace_vault_aggregates(postgres_url)
        assert agg.ok is True
        assert agg.liquid_closing_cents == 129_500
        assert agg.brokerage_closing_cents == 1_390_425
        assert agg.ready_to_assign_cents == 129_500
        text = agg.to_text()
        assert "123-45-6789" not in text
        assert "Liability" in text or "liquid" in text.lower() or "Liquid" in text

    def test_empty_and_bad_database_url(self) -> None:
        empty = trace_vault_aggregates("   ")
        assert empty.ok is False
        assert empty.to_text().startswith("[FAIL]")
        bad = trace_vault_aggregates(
            "postgresql://postgres:sapphire@127.0.0.1:1/sapphire_test"
        )
        assert bad.ok is False
