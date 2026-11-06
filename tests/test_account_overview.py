"""Account overview home: prepopulated dashboard + transaction drill-in."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    import psycopg

    from app.api.app import create_app
    from app.db.connection import init_db

    init_db()
    # Isolate each test from leftover Vault rows in the session DB.
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
    return TestClient(create_app())


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


def _upload(client: TestClient, tmp_path: Path, fixture: str) -> None:
    pdf = _render_pdf_from_fixture(fixture, tmp_path / f"{fixture}.pdf")
    with pdf.open("rb") as handle:
        response = client.post(
            "/api/v1/dashboard/upload",
            files={"file": (pdf.name, handle, "application/pdf")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text


class TestAccountOverviewOnLoad:
    def test_empty_db_shows_overview_empty_state(self, client: TestClient) -> None:
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "Account overview" in response.text
        assert "No persisted statements yet" in response.text
        # Must not invent fake zero balances
        assert 'total_closing_cents">0<' not in response.text.replace(" ", "")

    def test_seeded_db_prepopulates_high_level_metrics(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        response = client.get("/dashboard")
        assert response.status_code == 200
        html = response.text
        assert "Account overview" in html
        assert "Opening" in html
        assert "Closing" in html
        assert "Checking" in html or "Brokerage Cash" in html
        assert "brokerage_cash" not in html
        assert 'class="period"' in html
        assert "white-space: nowrap" in html or "period" in html
        assert ">Txns<" not in html and "Total transactions" not in html
        # High-level only on load: no full transaction table dump as the home
        assert "SYNTHETIC Payroll" not in html
        # Stored as cents; shown as USD (two decimal places, no float slop)
        assert re.search(r"\$\d[\d,]*\.\d{2}\b", html) is not None
        assert re.findall(r"\$\d[\d,]*\.\d{3,}", html) == []

    def test_overview_includes_multiple_accounts(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        _upload(client, tmp_path, "brokerage_cash_happy")
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "Chase" in response.text or "chase" in response.text.lower()
        assert "Schwab" in response.text or "schwab" in response.text.lower()
        assert "Accounts" in response.text

    def test_overview_splits_liquid_vs_brokerage_funds(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """Depository = liquid cash; custodial brokerage = non-liquid funds.

        Redacted Schwab checking PDFs are bank-only (no brokerage markers);
        brokerage arrives as a separate statement/account. Overview must not
        collapse both into one undifferentiated total.
        """
        _upload(client, tmp_path, "chase_checking_happy")
        _upload(client, tmp_path, "brokerage_cash_happy")
        response = client.get("/dashboard")
        assert response.status_code == 200
        html = response.text
        assert "Liquid cash" in html
        assert "Brokerage funds" in html
        assert "Liquid" in html
        assert "Non-liquid" in html
        # chase_checking_happy ending $1,295.00; brokerage_cash_happy ending $13,904.25
        assert "$1,295.00" in html
        assert "$13,904.25" in html
        # Hero must not present a single blended "Total closing balance" only
        assert "Total closing balance" not in html


class TestTransactionDrillIn:
    def test_details_returns_ledger_for_statement(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        dash = client.get("/dashboard")
        match = re.search(
            r"/api/v1/dashboard/statements/([0-9a-f-]{36})",
            dash.text,
            re.IGNORECASE,
        )
        assert match is not None, "overview must link Details to a statement id"
        statement_id = match.group(1)
        response = client.get(
            f"/api/v1/dashboard/statements/{statement_id}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "Vault Ledger" in response.text
        assert "Chase" in response.text
        assert "Liquid" in response.text
        assert "SYNTHETIC Payroll" in response.text
        assert "transfer_in" not in response.text
        assert "Deposit" in response.text or "Withdrawal" in response.text
        assert re.search(r"\$\d[\d,]*\.\d{2}\b", response.text) is not None
        assert re.findall(r"\$\d[\d,]*\.\d{3,}", response.text) == []

    def test_unknown_statement_fails_loud(self, client: TestClient) -> None:
        response = client.get(
            f"/api/v1/dashboard/statements/{uuid4()}",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        assert "STATEMENT_NOT_FOUND" in response.text

    def test_drill_in_requires_htmx(self, client: TestClient) -> None:
        response = client.get(f"/api/v1/dashboard/statements/{uuid4()}")
        assert response.status_code == 400
