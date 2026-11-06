"""Dashboard refresh: sidebar shell, light-default theme toggle, new views.

Guarantees under test (pitch: docs/specs/pitch_dashboard_refresh.md):
- Single sidebar navigation replaces duplicated tab rows.
- Light theme is the default; a toggle switches light/dark and persists.
- All styling is served locally (no inline styles, no external assets).
- Statements and Audit are first-class HTMX views.
- Existing contract IDs / error codes / integer-cents display are preserved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.budget.envelopes import create_envelope

FIXTURES_DIR = Path(__file__).parent / "fixtures"
HX = {"HX-Request": "true"}
STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

NAV_TARGETS = {
    "nav-overview": "/api/v1/dashboard/overview",
    "nav-budget": "/api/v1/dashboard/budget",
    "nav-statements": "/api/v1/dashboard/statements",
    "nav-audit": "/api/v1/dashboard/audit",
}


@pytest.fixture()
def client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    from app.api.app import create_app
    from app.db.connection import init_db

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
    return TestClient(create_app())


def _upload(client: TestClient, tmp_path: Path, fixture: str) -> None:
    data = json.loads((FIXTURES_DIR / f"{fixture}.json").read_text(encoding="utf-8"))
    pdf = tmp_path / f"{fixture}.pdf"
    c = canvas.Canvas(str(pdf))
    y = 750
    for line in data["raw_text"].split("\n"):
        c.drawString(50, y, re.sub(r"(\t|\s{2,})", " | ", line))
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    with pdf.open("rb") as handle:
        response = client.post(
            "/api/v1/dashboard/upload",
            files={"file": (pdf.name, handle, "application/pdf")},
            headers=HX,
        )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Shell: sidebar, topbar, theme
# ---------------------------------------------------------------------------


class TestShell:
    def test_default_theme_is_light(self, client: TestClient) -> None:
        html = client.get("/dashboard").text
        assert '<html lang="en" data-theme="light">' in html

    def test_theme_toggle_present_and_persisted(self, client: TestClient) -> None:
        html = client.get("/dashboard").text
        assert 'id="theme-toggle"' in html
        assert 'aria-label="Switch to dark theme"' in html
        assert "localStorage" in html
        assert "sapphire-theme" in html
        # When nothing is stored, prefer the OS color scheme.
        assert "prefers-color-scheme" in html
        assert "matchMedia" in html

    def test_sidebar_has_single_navigation_with_htmx_targets(
        self, client: TestClient
    ) -> None:
        html = client.get("/dashboard").text
        assert html.count('<nav class="nav"') == 1
        for button_id, url in NAV_TARGETS.items():
            assert f'id="{button_id}"' in html
            assert f'hx-get="{url}"' in html
        assert 'id="pane-vault-body"' in html
        # Old duplicated tab rows are gone.
        assert "switchActionTab" not in html
        assert "header-btn-overview" not in html
        assert "tab-btn-budget" not in html

    def test_topbar_title_and_primary_action(self, client: TestClient) -> None:
        html = client.get("/dashboard").text
        assert 'id="topbar-title"' in html
        assert "Overview" in html
        assert 'id="topbar-upload"' in html
        assert "Upload statement" in html
        # Title updates when HTMX swaps a view with data-nav.
        assert "topbar-title" in html
        assert "data-nav" in html or "PAGE_TITLES" in html or "pageTitles" in html

    def test_vault_trust_card(self, client: TestClient) -> None:
        html = client.get("/dashboard").text
        assert "Local Vault" in html
        assert "Integer cents" in html

    def test_no_inline_styles_or_external_assets(self, client: TestClient) -> None:
        html = client.get("/dashboard").text
        assert "style=" not in html
        assert "<style" not in html
        assert "/static/sapphire.css" in html
        assert "/static/htmx.min.js" in html
        assert re.findall(r'(?:src|href)="https?://', html) == []
        assert "@import" not in html

    def test_overview_is_prepopulated_in_main_pane(self, client: TestClient) -> None:
        html = client.get("/dashboard").text
        assert "Account overview" in html


class TestStylesheet:
    def test_stylesheet_is_served_with_both_theme_token_sets(
        self, client: TestClient
    ) -> None:
        response = client.get("/static/sapphire.css")
        assert response.status_code == 200
        css = response.text
        assert '[data-theme="light"]' in css
        assert '[data-theme="dark"]' in css
        assert "--accent" in css
        assert "tabular-nums" in css

    def test_stylesheet_has_no_remote_resources(self) -> None:
        css = (STATIC_DIR / "sapphire.css").read_text(encoding="utf-8")
        assert "http://" not in css
        assert "https://" not in css
        assert "@import" not in css
        assert "url(" not in css


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestSeedSupportsDrillIn:
    def test_seed_script_sidecars_enable_statement_drill_in(
        self, client: TestClient, postgres_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", postgres_url)
        from scripts.seed_dashboard import seed

        seed()
        seed()  # idempotent second pass
        overview = client.get("/api/v1/dashboard/overview", headers=HX).text
        statement_ids = re.findall(
            r'hx-get="/api/v1/dashboard/statements/([0-9a-f-]{36})"', overview
        )
        assert len(statement_ids) == 3
        checking = None
        for statement_id in statement_ids:
            response = client.get(
                f"/api/v1/dashboard/statements/{statement_id}", headers=HX
            )
            assert response.status_code == 200, response.text
            assert "Vault Ledger" in response.text
            assert "LEDGER_QUERY_FAILED" not in response.text
            if "SYNTHETIC Payroll" in response.text:
                checking = response.text
        assert checking is not None
        assert 'class="amount num pos"' in checking
        assert 'class="amount num neg"' in checking


class TestOverview:
    def test_empty_state_offers_next_action(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert "No persisted statements yet" in html
        assert 'data-nav="overview"' in html
        assert 'hx-get="/api/v1/dashboard/statements"' in html
        assert "Total closing balance" not in html  # never invent zero balances

    def test_kpis_and_clickable_account_rows(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert "Total closing balance" in html
        assert "Ready to Assign" in html
        assert "Last statement" in html
        assert 'class="period"' in html
        assert re.search(r'hx-get="/api/v1/dashboard/statements/[0-9a-f-]{36}"', html)
        assert re.search(r"\$\d[\d,]*\.\d{2}\b", html)
        assert re.findall(r"\$\d[\d,]*\.\d{3,}", html) == []
        assert "style=" not in html

    def test_ready_to_assign_kpi_matches_budget_value(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        overview = client.get("/api/v1/dashboard/overview", headers=HX).text
        budget = client.get("/api/v1/dashboard/budget", headers=HX).text
        rta = re.search(r'id="ready-to-assign">\s*(-?\$[\d,]+\.\d{2})', budget)
        assert rta is not None
        assert rta.group(1) in overview


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


class TestBudgetView:
    def test_forms_live_inside_budget_view_with_stable_ids(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            create_envelope(conn, name="SYNTHETIC Groceries")
            conn.commit()
        html = client.get("/api/v1/dashboard/budget", headers=HX).text
        assert 'data-nav="budget"' in html
        assert 'id="budget-assign-form"' in html
        assert 'id="envelope-create-form"' in html
        assert 'id="assign-btn"' in html
        assert 'hx-post="/api/v1/dashboard/budget/assign"' in html
        assert 'hx-post="/api/v1/dashboard/budget/envelopes"' in html
        assert "SYNTHETIC Groceries</option>" in html
        assert "style=" not in html

    def test_no_envelopes_disables_assign_with_hint(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/budget", headers=HX).text
        assert "Create an envelope first" in html
        assert "No envelopes yet" in html

    def test_assign_error_renders_inline_and_keeps_forms(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Groceries")
            conn.commit()
        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={"envelope_id": str(env.envelope_id), "amount_usd": "999999999"},
            headers=HX,
        )
        assert response.status_code == 400
        assert 'role="alert"' in response.text
        assert "ASSIGN_EXCEEDS_AVAILABLE" in response.text
        assert "nothing was saved" in response.text.lower()
        assert 'id="budget-assign-form"' in response.text

    def test_invalid_amount_error_renders_inline(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Groceries")
            conn.commit()
        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={"envelope_id": str(env.envelope_id), "amount_usd": "abc"},
            headers=HX,
        )
        assert response.status_code == 400
        assert "INVALID_AMOUNT" in response.text
        assert 'id="envelope-create-form"' in response.text

    def test_blank_envelope_name_error_renders_inline(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/budget/envelopes",
            data={"name": "   "},
            headers=HX,
        )
        assert response.status_code == 400
        assert "INVALID_ENVELOPE_NAME" in response.text
        assert 'id="envelope-create-form"' in response.text

    def test_unknown_envelope_error_renders_inline(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={
                "envelope_id": "00000000-0000-0000-0000-000000000000",
                "amount_usd": "1",
            },
            headers=HX,
        )
        assert response.status_code == 400
        assert 'id="budget-assign-form"' in response.text


# ---------------------------------------------------------------------------
# Statements + Audit views
# ---------------------------------------------------------------------------


class TestStatementsView:
    def test_requires_htmx(self, client: TestClient) -> None:
        assert client.get("/api/v1/dashboard/statements").status_code == 400

    def test_empty_shows_dropzone_and_hint(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/statements", headers=HX).text
        assert 'data-nav="statements"' in html
        assert 'id="upload-form"' in html
        assert 'hx-post="/api/v1/dashboard/upload"' in html
        assert 'hx-encoding="multipart/form-data"' in html
        assert 'id="upload-dropzone"' in html
        assert 'for="statement-file"' in html
        assert 'id="statement-file"' in html
        assert 'id="upload-prompt"' in html
        assert 'id="upload-filename"' in html
        assert 'accept="application/pdf,.pdf"' in html
        assert 'id="upload-btn"' in html
        assert "Upload to Vault" in html
        assert "never leave the Vault" in html
        assert "No statements yet" in html
        assert "style=" not in html
        # Shell must update the dropzone label when a file is chosen (hidden input).
        shell = client.get("/dashboard").text
        assert 'id="statement-file"' in shell or "statement-file" in shell
        assert "upload-prompt" in shell
        assert "files[0]" in shell or "files &&" in shell or ".files" in shell

    def test_lists_recent_statements(self, client: TestClient, tmp_path: Path) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        html = client.get("/api/v1/dashboard/statements", headers=HX).text
        assert 'id="statements-recent"' in html
        assert re.search(r'hx-get="/api/v1/dashboard/statements/[0-9a-f-]{36}"', html)
        assert "Chase" in html
        assert "No statements yet" not in html


class TestAuditView:
    def test_requires_htmx(self, client: TestClient) -> None:
        assert client.get("/api/v1/dashboard/audit").status_code == 400

    def test_contains_ledger_and_frontier_controls(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/audit", headers=HX).text
        assert 'data-nav="audit"' in html
        assert 'id="question"' in html
        assert 'id="submit-btn"' in html
        assert 'hx-post="/api/v1/dashboard/analyze"' in html
        assert 'id="frontier-audit-form"' in html
        assert 'id="frontier-verify-btn"' in html
        assert 'hx-post="/api/v1/dashboard/frontier-audit"' in html
        assert 'hx-target="#frontier-result"' in html
        assert 'id="frontier-result"' in html
        assert 'value="transaction_category"' in html
        assert "Verify &amp; Analyze with Frontier Model" in html
        assert "Only grouped totals" in html
        assert "style=" not in html


# ---------------------------------------------------------------------------
# Shared partials: errors, ledger, frontier, status
# ---------------------------------------------------------------------------


class TestSharedPartials:
    def test_upload_error_is_accessible_alert_with_recovery(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/v1/dashboard/upload",
            files={"file": ("x.txt", b"nope", "text/plain")},
            headers=HX,
        )
        assert response.status_code == 400
        html = response.text
        assert 'role="alert"' in html
        assert "<code>" in html
        assert "INVALID_FILE" in html or "INVALID_PDF" in html
        assert "nothing was saved" in html.lower()
        assert 'hx-get="/api/v1/dashboard/statements"' in html
        assert "style=" not in html

    def test_ledger_result_has_back_to_overview_and_positive_amounts(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        html = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "Show latest Vault ledger"},
            headers=HX,
        ).text
        assert "Vault Ledger" in html
        assert 'hx-get="/api/v1/dashboard/overview"' in html
        assert 'data-nav="statements"' in html
        assert 'class="amount num pos"' in html
        assert "style=" not in html

    def test_ledger_empty_state(self, client: TestClient) -> None:
        html = client.post(
            "/api/v1/dashboard/analyze",
            data={"question": "Show latest Vault ledger"},
            headers=HX,
        ).text
        assert "No persisted statements yet" in html
        assert "style=" not in html

    def test_frontier_error_is_alert(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/frontier-audit",
            data={"group_by": ""},
            headers=HX,
        )
        assert response.status_code == 400
        assert 'role="alert"' in response.text
        assert "Rejected" in response.text
        assert "INVALID_GROUP_BY" in response.text
        assert "nothing was saved" in response.text.lower()
        assert "style=" not in response.text

    def test_status_fragment_has_no_inline_style(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/status", headers=HX).text
        assert "Engine Online" in html
        assert "style=" not in html
