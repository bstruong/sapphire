"""Shared PDF ingest orchestration for API and dashboard HTML paths.

Returns structured success/failure without FastAPI response types so both
JSON and HTMX callers can map to their surface.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from app.adapters.base import CombinedStatementAdapter
from app.adapters.registry import AdapterRegistryError, get_default_registry
from app.db.connection import get_db_connection
from app.db.repository import (
    get_canonical_ingest_by_sha256,
    get_run_statements,
    persist_canonical_statement,
    persist_raw_extraction,
    persist_raw_payload,
    update_run_status,
)
from app.extraction.core import extract_pdf_to_raw
from app.models.enums import RunStatus
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
    PersistenceError,
    SchemaDriftError,
    TokenError,
)
from app.models.raw import RawExtraction

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestSuccess:
    """Canonical persist completed for one run."""

    run_id: UUID
    raw_payload_id: UUID
    adapter_id: str
    adapter_version: str
    statements_count: int
    transactions_count: int
    accounts: list[tuple[UUID, str, str, str, str, str]]


@dataclass(frozen=True)
class IngestFailure:
    """Loud ingest failure with taxonomy code."""

    error_code: str
    detail: str
    http_status: int
    run_id: UUID | None = None
    raw_payload_id: UUID | None = None
    adapter_id: str | None = None


MAX_BATCH_FILES = 20
MAX_FILE_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class BatchItemResult:
    """Per-file outcome for a multi-PDF bulk ingest."""

    filename: str
    ok: bool
    run_id: UUID | None = None
    statement_id: UUID | None = None
    error_code: str | None = None
    detail: str | None = None
    adapter_id: str | None = None


def _first_statement_id(run_id: UUID) -> UUID | None:
    """Return the first statement_id for a persisted run, if any."""
    with get_db_connection() as conn:
        rows = get_run_statements(conn, run_id)
    if not rows:
        return None
    return rows[0][1].statement_id


def _validate_pdf_item(
    tmp_path: Path, original_basename: str
) -> BatchItemResult | None:
    """Return a failure result when pre-ingest checks fail; None when valid."""
    safe_name = Path(original_basename).name
    if not safe_name.lower().endswith(".pdf"):
        return BatchItemResult(
            filename=safe_name,
            ok=False,
            error_code="INVALID_FILE",
            detail="Only PDF documents are supported for ingestion.",
        )
    try:
        content = tmp_path.read_bytes()
    except OSError as exc:
        return BatchItemResult(
            filename=safe_name,
            ok=False,
            error_code="EXTRACTION_FAILED",
            detail=str(exc),
        )
    if not content:
        return BatchItemResult(
            filename=safe_name,
            ok=False,
            error_code="EMPTY_FILE",
            detail="Uploaded file payload is empty.",
        )
    if len(content) > MAX_FILE_BYTES:
        return BatchItemResult(
            filename=safe_name,
            ok=False,
            error_code="FILE_TOO_LARGE",
            detail=f"File exceeds maximum size of {MAX_FILE_BYTES} bytes.",
        )
    if not content.startswith(b"%PDF"):
        return BatchItemResult(
            filename=safe_name,
            ok=False,
            error_code="INVALID_PDF",
            detail="Uploaded file is not a valid PDF binary.",
        )
    return None


def ingest_pdf_batch(
    items: list[tuple[Path, str]],
) -> list[BatchItemResult]:
    """Ingest each PDF independently; partial success is explicit per file.

    Sequential only — no background jobs or parallel workers in v1.
    Invalid files become failed items with taxonomy codes (never silently skipped).
    """
    results: list[BatchItemResult] = []
    for tmp_path, original_basename in items:
        safe_name = Path(original_basename).name
        prefail = _validate_pdf_item(tmp_path, safe_name)
        if prefail is not None:
            results.append(prefail)
            continue

        outcome = ingest_pdf_path(tmp_path, safe_name)
        if isinstance(outcome, IngestFailure):
            results.append(
                BatchItemResult(
                    filename=safe_name,
                    ok=False,
                    run_id=outcome.run_id,
                    error_code=outcome.error_code,
                    detail=outcome.detail,
                    adapter_id=outcome.adapter_id,
                )
            )
            continue

        assert isinstance(outcome, IngestSuccess)
        results.append(
            BatchItemResult(
                filename=safe_name,
                ok=True,
                run_id=outcome.run_id,
                statement_id=_first_statement_id(outcome.run_id),
                adapter_id=outcome.adapter_id,
            )
        )
    return results


def ingest_pdf_path(
    tmp_path: Path, original_basename: str
) -> IngestSuccess | IngestFailure:
    """Match, extract, reconcile, and persist a statement PDF already on disk."""
    try:
        file_bytes = tmp_path.read_bytes()
    except OSError as exc:
        return IngestFailure(
            error_code="EXTRACTION_FAILED",
            detail=str(exc),
            http_status=422,
        )
    content_sha256 = hashlib.sha256(file_bytes).hexdigest()
    with get_db_connection() as conn:
        prior = get_canonical_ingest_by_sha256(conn, content_sha256)
    if prior is not None:
        logger.info(
            "Re-upload of identical PDF bytes; returning prior canonical ingest",
            extra={
                "run_id": str(prior["run_id"]),
                "adapter_id": str(prior["adapter_id"]),
            },
        )
        return IngestSuccess(
            run_id=prior["run_id"],  # type: ignore[arg-type]
            raw_payload_id=prior["raw_payload_id"],  # type: ignore[arg-type]
            adapter_id=str(prior["adapter_id"]),
            adapter_version=str(prior["adapter_version"]),
            statements_count=int(prior["statements_count"]),  # type: ignore[arg-type]
            transactions_count=int(prior["transactions_count"]),  # type: ignore[arg-type]
            accounts=list(prior["accounts"]),  # type: ignore[arg-type]
        )

    registry = get_default_registry()
    try:
        adapter = registry.match(tmp_path)
    except AdapterRegistryError as exc:
        logger.warning(
            "Adapter registry matching failed",
            extra={"error_type": type(exc).__name__},
        )
        return IngestFailure(
            error_code="ADAPTER_MATCH_FAILED",
            detail=str(exc),
            http_status=422,
        )

    try:
        raw_extraction = extract_pdf_to_raw(
            file_path=tmp_path,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=original_basename,
        )
    except (ValueError, FileNotFoundError) as exc:
        logger.error(
            "Raw page and token extraction failed",
            extra={"error_type": type(exc).__name__},
        )
        return IngestFailure(
            error_code="EXTRACTION_FAILED",
            detail=str(exc),
            http_status=422,
        )

    with get_db_connection() as conn:
        actual_payload_id = persist_raw_payload(conn, raw_extraction.payload)
        if raw_extraction.payload.raw_payload_id != actual_payload_id:
            raw_extraction = RawExtraction(
                payload=raw_extraction.payload.model_copy(
                    update={"raw_payload_id": actual_payload_id}
                ),
                run=raw_extraction.run.model_copy(
                    update={"raw_payload_id": actual_payload_id}
                ),
                pages=raw_extraction.pages,
            )
        persist_raw_extraction(conn, raw_extraction)

    run_id = raw_extraction.run.run_id

    try:
        parsed = adapter.parse_canonical(raw_extraction)
        if isinstance(adapter, CombinedStatementAdapter):
            bundles: Sequence[tuple] = parsed  # type: ignore[assignment]
        else:
            bundles = [parsed]  # type: ignore[list-item]

        with get_db_connection() as conn:
            for bundle in bundles:
                acc, stmt, summary, txns = bundle[:4]
                holdings = bundle[4] if len(bundle) > 4 else ()
                persist_canonical_statement(
                    conn, acc, stmt, summary, txns, holdings=holdings
                )
            update_run_status(conn, run_id, RunStatus.CANONICAL_PERSISTED)

        logger.info(
            "Statement successfully ingested and persisted",
            extra={
                "run_id": str(run_id),
                "adapter_id": adapter.adapter_id,
                "statements_count": len(bundles),
            },
        )

        accounts = [
            (
                acc.account_id,
                acc.institution,
                acc.account_mask,
                acc.account_domain.value,
                acc.account_type.value,
                acc.currency.value,
            )
            for acc, *_rest in bundles
        ]
        total_txns = sum(len(bundle[3]) for bundle in bundles)
        return IngestSuccess(
            run_id=run_id,
            raw_payload_id=actual_payload_id,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            statements_count=len(bundles),
            transactions_count=total_txns,
            accounts=accounts,
        )
    except (
        InvariantError,
        MissingSectionError,
        AmbiguousAccountsError,
        SchemaDriftError,
        TokenError,
        PersistenceError,
        ValueError,
    ) as exc:
        logger.error(
            "Statement processing failed invariant/schema contract",
            extra={
                "run_id": str(run_id),
                "adapter_id": adapter.adapter_id,
                "error_type": type(exc).__name__,
            },
        )
        with get_db_connection() as conn:
            update_run_status(
                conn,
                run_id,
                RunStatus.FAILED,
                error_code=type(exc).__name__,
                error_message=str(exc),
            )
        return IngestFailure(
            error_code=type(exc).__name__,
            detail=str(exc),
            http_status=422,
            run_id=run_id,
            raw_payload_id=actual_payload_id,
            adapter_id=adapter.adapter_id,
        )
