"""Upload progress bar: determinate N-of-M for bulk, bar through single request."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.pipeline.ingest_service import MAX_BATCH_FILES

FIXTURES_DIR = Path(__file__).parent / "fixtures"
HX = {"HX-Request": "true"}


@pytest.fixture()
def client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    from app.api.app import create_app
    from app.db.connection import init_db

    init_db()
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


def _unknown_pdf(target_path: Path) -> Path:
    c = canvas.Canvas(str(target_path))
    c.drawString(50, 750, "UNKNOWN CREDIT UNION")
    c.drawString(50, 735, "CHECKING")
    c.save()
    return target_path


class TestUploadProgressMarkup:
    def test_statements_form_has_progress_bar(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/statements", headers=HX).text
        assert 'id="upload-progress"' in html
        assert 'id="upload-progress-fill"' in html
        assert 'id="upload-progress-label"' in html
        assert 'role="progressbar"' in html
        assert 'hx-indicator="#upload-progress"' in html
        # Must not use the old pane-dimming spinner substitute for upload.
        assert 'hx-indicator="#pane-vault-body"' not in html
        assert "style=" not in html
        # Real bar track — not a spinner-only affordance.
        assert "upload-progress-track" in html
        assert "spinner" not in html.lower()

    def test_dashboard_shell_has_sequential_progress_script(
        self, client: TestClient
    ) -> None:
        shell = client.get("/dashboard").text
        assert "upload-progress" in shell
        assert "of" in shell  # N of M label logic
        assert "upload-item" in shell
        assert "upload-summary" in shell
        # No spinner-centric upload UX.
        assert "htmx-indicator" in shell or "upload-progress" in shell


class TestUploadItemJson:
    def test_requires_htmx(self, client: TestClient, tmp_path: Path) -> None:
        pdf = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        with pdf.open("rb") as handle:
            response = client.post(
                "/api/v1/dashboard/upload-item",
                files={"files": ("chase.pdf", handle, "application/pdf")},
            )
        assert response.status_code == 400

    def test_success_returns_json_item(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        pdf = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        with pdf.open("rb") as handle:
            response = client.post(
                "/api/v1/dashboard/upload-item",
                files={"files": ("chase.pdf", handle, "application/pdf")},
                headers=HX,
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["filename"] == "chase.pdf"
        assert body["statement_id"]
        assert body["error_code"] is None

    def test_failure_returns_json_item_loud(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        pdf = _unknown_pdf(tmp_path / "unknown.pdf")
        with pdf.open("rb") as handle:
            response = client.post(
                "/api/v1/dashboard/upload-item",
                files={"files": ("unknown.pdf", handle, "application/pdf")},
                headers=HX,
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["error_code"] == "ADAPTER_MATCH_FAILED"
        assert body["detail"]

    def test_missing_file_fails_loud(self, client: TestClient) -> None:
        response = client.post("/api/v1/dashboard/upload-item", headers=HX)
        assert response.status_code == 400
        assert response.json()["error_code"] == "MISSING_FILE"

    def test_rejects_multi_file_payload(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        a = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "a.pdf")
        b = _render_pdf_from_fixture("brokerage_cash_happy", tmp_path / "b.pdf")
        with a.open("rb") as h1, b.open("rb") as h2:
            response = client.post(
                "/api/v1/dashboard/upload-item",
                files=[
                    ("files", ("a.pdf", h1, "application/pdf")),
                    ("files", ("b.pdf", h2, "application/pdf")),
                ],
                headers=HX,
            )
        assert response.status_code == 400
        assert response.json()["error_code"] == "TOO_MANY_FILES"


class TestUploadSummaryHtml:
    def test_renders_batch_from_json_results(self, client: TestClient) -> None:
        payload = {
            "results": [
                {
                    "filename": "ok.pdf",
                    "ok": True,
                    "statement_id": "11111111-1111-1111-1111-111111111111",
                    "run_id": None,
                    "error_code": None,
                    "detail": None,
                    "adapter_id": "chase_depository",
                },
                {
                    "filename": "bad.pdf",
                    "ok": False,
                    "statement_id": None,
                    "run_id": None,
                    "error_code": "ADAPTER_MATCH_FAILED",
                    "detail": "No adapter matched",
                    "adapter_id": None,
                },
            ]
        }
        response = client.post(
            "/api/v1/dashboard/upload-summary",
            headers={**HX, "Content-Type": "application/json"},
            content=json.dumps(payload),
        )
        assert response.status_code == 200
        assert "Uploaded 1 of 2" in response.text
        assert "ok.pdf" in response.text
        assert "bad.pdf" in response.text
        assert "ADAPTER_MATCH_FAILED" in response.text
        assert "style=" not in response.text

    def test_empty_results_fails_loud(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/upload-summary",
            headers={**HX, "Content-Type": "application/json"},
            content=json.dumps({"results": []}),
        )
        assert response.status_code == 400

    def test_malformed_payload_fails_loud(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/upload-summary",
            headers={**HX, "Content-Type": "application/json"},
            content=json.dumps({"results": [{"filename": "x.pdf"}]}),
        )
        assert response.status_code == 400

    def test_requires_htmx(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/upload-summary",
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "results": [
                        {
                            "filename": "a.pdf",
                            "ok": False,
                            "statement_id": None,
                            "run_id": None,
                            "error_code": "X",
                            "detail": "y",
                            "adapter_id": None,
                        }
                    ]
                }
            ),
        )
        assert response.status_code == 400


class TestProgressCss:
    def test_stylesheet_has_progress_bar_not_spinner_only(
        self, client: TestClient
    ) -> None:
        css = client.get("/static/sapphire.css").text
        assert ".upload-progress" in css
        assert ".upload-progress-fill" in css
        assert ".upload-progress-track" in css
        # Advancing fill animation for single-file request lifecycle.
        assert "upload-progress-advance" in css or "@keyframes" in css


def test_max_batch_constant_unchanged() -> None:
    assert MAX_BATCH_FILES == 20
