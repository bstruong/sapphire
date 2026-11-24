"""Bulk multi-PDF statement upload: batch helper, dashboard HTMX, JSON API."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.pipeline.ingest_service import (
    MAX_BATCH_FILES,
    MAX_FILE_BYTES,
    BatchItemResult,
    ingest_pdf_batch,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


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


class TestIngestPdfBatch:
    def test_empty_batch_returns_empty(self) -> None:
        assert ingest_pdf_batch([]) == []

    def test_mixed_success_and_fail(self, postgres_url: str, tmp_path: Path) -> None:
        import os

        os.environ["DATABASE_URL"] = postgres_url
        from app.db.connection import init_db

        init_db()
        good = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        bad = _unknown_pdf(tmp_path / "unknown.pdf")
        results = ingest_pdf_batch(
            [
                (good, "chase.pdf"),
                (bad, "unknown.pdf"),
            ]
        )
        assert len(results) == 2
        assert results[0].ok is True
        assert results[0].filename == "chase.pdf"
        assert results[0].run_id is not None
        assert results[0].statement_id is not None
        assert results[1].ok is False
        assert results[1].error_code == "ADAPTER_MATCH_FAILED"

    def test_non_pdf_extension_fails_loud(self, tmp_path: Path) -> None:
        path = tmp_path / "notes.txt"
        path.write_bytes(b"not a pdf")
        results = ingest_pdf_batch([(path, "notes.txt")])
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error_code == "INVALID_FILE"

    def test_empty_file_fails_loud(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.pdf"
        path.write_bytes(b"")
        results = ingest_pdf_batch([(path, "empty.pdf")])
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error_code == "EMPTY_FILE"

    def test_invalid_pdf_magic_fails_loud(self, tmp_path: Path) -> None:
        path = tmp_path / "fake.pdf"
        path.write_bytes(b"NOTPDF content here")
        results = ingest_pdf_batch([(path, "fake.pdf")])
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error_code == "INVALID_PDF"

    def test_oversize_file_fails_loud(self, tmp_path: Path) -> None:
        path = tmp_path / "huge.pdf"
        # Valid magic but over max bytes — helper rejects without ingest.
        path.write_bytes(b"%PDF" + b"x" * MAX_FILE_BYTES)
        results = ingest_pdf_batch([(path, "huge.pdf")])
        assert len(results) == 1
        assert results[0].ok is False
        assert results[0].error_code == "FILE_TOO_LARGE"


class TestDashboardBulkUpload:
    def test_statements_form_has_multiple(self, client: TestClient) -> None:
        response = client.get(
            "/api/v1/dashboard/statements",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert 'name="files"' in response.text
        assert "multiple" in response.text
        assert "max 20" in response.text.lower() or "Max 20" in response.text

    def test_two_success_returns_batch_summary(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        chase = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        schwab = _render_pdf_from_fixture(
            "brokerage_cash_happy", tmp_path / "schwab.pdf"
        )
        with chase.open("rb") as h1, schwab.open("rb") as h2:
            response = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", ("chase.pdf", h1, "application/pdf")),
                    ("files", ("schwab.pdf", h2, "application/pdf")),
                ],
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "Uploaded 2 of 2" in response.text
        assert "chase.pdf" in response.text
        assert "schwab.pdf" in response.text
        assert response.text.count(">OK<") >= 2 or "status-ok" in response.text

    def test_mixed_batch_partial_success(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        good = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        bad = _unknown_pdf(tmp_path / "unknown.pdf")
        with good.open("rb") as h1, bad.open("rb") as h2:
            response = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", ("chase.pdf", h1, "application/pdf")),
                    ("files", ("unknown.pdf", h2, "application/pdf")),
                ],
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "Uploaded 1 of 2" in response.text
        assert "ADAPTER_MATCH_FAILED" in response.text
        assert "FAIL" in response.text

    def test_single_file_still_returns_ledger(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        """One-file upload keeps existing ledger swap for demo continuity."""
        pdf = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        with pdf.open("rb") as handle:
            response = client.post(
                "/api/v1/dashboard/upload",
                files=[("files", ("chase.pdf", handle, "application/pdf"))],
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "Vault Ledger" in response.text

    def test_legacy_file_field_still_works(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        pdf = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        with pdf.open("rb") as handle:
            response = client.post(
                "/api/v1/dashboard/upload",
                files={"file": ("chase.pdf", handle, "application/pdf")},
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "Vault Ledger" in response.text

    def test_zero_files_missing(self, client: TestClient) -> None:
        response = client.post(
            "/api/v1/dashboard/upload",
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        assert "MISSING_FILE" in response.text

    def test_over_max_count_rejected(self, client: TestClient, tmp_path: Path) -> None:
        paths = []
        for i in range(MAX_BATCH_FILES + 1):
            p = tmp_path / f"f{i}.pdf"
            p.write_bytes(b"%PDF-1.4 minimal")
            paths.append(p)
        handles = [p.open("rb") for p in paths]
        try:
            response = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", (p.name, h, "application/pdf"))
                    for p, h in zip(paths, handles, strict=True)
                ],
                headers={"HX-Request": "true"},
            )
        finally:
            for h in handles:
                h.close()
        assert response.status_code == 400
        assert "TOO_MANY_FILES" in response.text
        assert str(MAX_BATCH_FILES) in response.text

    def test_non_pdf_in_batch_is_per_file_fail(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        good = _render_pdf_from_fixture("chase_checking_happy", tmp_path / "chase.pdf")
        txt = tmp_path / "notes.txt"
        txt.write_bytes(b"hello")
        with good.open("rb") as h1, txt.open("rb") as h2:
            response = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", ("chase.pdf", h1, "application/pdf")),
                    ("files", ("notes.txt", h2, "text/plain")),
                ],
                headers={"HX-Request": "true"},
            )
        assert response.status_code == 200
        assert "Uploaded 1 of 2" in response.text
        assert "INVALID_FILE" in response.text


def test_batch_item_result_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    item = BatchItemResult(filename="a.pdf", ok=False, error_code="X", detail="y")
    with pytest.raises(FrozenInstanceError):
        item.ok = True  # type: ignore[misc]
