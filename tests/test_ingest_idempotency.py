"""Re-uploading identical PDF bytes must not create duplicate statements."""

from __future__ import annotations

from pathlib import Path

from app.pipeline.ingest_service import IngestSuccess, ingest_pdf_path

REDACTED = (
    Path(__file__).parent / "fixtures" / "redacted" / "schwab_checking_sample.pdf"
)


def test_reingest_same_bytes_is_idempotent(postgres_url: str, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    assert REDACTED.is_file()
    first = ingest_pdf_path(REDACTED, REDACTED.name)
    assert isinstance(first, IngestSuccess)
    second = ingest_pdf_path(REDACTED, REDACTED.name)
    assert isinstance(second, IngestSuccess)
    assert second.run_id == first.run_id
    assert second.raw_payload_id == first.raw_payload_id
    assert second.statements_count == first.statements_count
