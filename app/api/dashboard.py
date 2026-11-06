"""HTMX Dashboard router for the SAPPHIRE Local Ingest API.

Guarantees:
- Zero PII egress across all routes and responses.
- Strict contract enforcement: fails loudly on invalid payloads.
- Financial precision: no floating-point monetary values.
- HTMX-first: returns HTML fragments for hx-swap updates.

Mutation scope: this module is intentionally **out of mutmut scope**
(``do_not_mutate`` / diff allowlist). Cover behavior with unit tests and
Playwright E2E; do not gate PRs on dashboard mutation kill rates.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.budget.envelopes import (
    assign_to_envelope,
    create_envelope,
    list_envelopes,
    ready_to_assign_cents,
)
from app.db.connection import get_db_connection
from app.db.repository import (
    get_canonical_statement,
    get_run_statements,
    get_statement_holdings,
    list_recent_statements,
)
from app.mcp.cloud_tools import CloudToolError, get_financial_summary
from app.models.exceptions import AssignExceedsAvailableError, TokenError
from app.pipeline.ingest_service import IngestFailure, IngestSuccess, ingest_pdf_path
from app.presentation.label_format import format_snake_label
from app.presentation.liquidity import (
    LIQUIDITY_BROKERAGE,
    LIQUIDITY_LIQUID,
    liquidity_class_for_domain,
    liquidity_label,
)
from app.presentation.money_format import format_usd_cents
from app.presentation.usd_parse import parse_usd_form_to_cents

logger = logging.getLogger("app.api.dashboard")

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(enabled_extensions=["html"]),
)
env.filters["usd_cents"] = format_usd_cents
env.filters["human_label"] = format_snake_label


router = APIRouter()
dashboard_api = APIRouter(prefix="/api/v1/dashboard")


def _overview_accounts(*, limit: int = 50) -> list[dict[str, Any]]:
    """Latest statement per account for the overview table (no txn rows)."""
    rows: list[dict[str, Any]] = []
    seen_accounts: set[UUID] = set()
    with get_db_connection() as conn:
        recent = list_recent_statements(conn, limit=limit)
        for account, statement, _txn_count in recent:
            if account.account_id in seen_accounts:
                continue
            seen_accounts.add(account.account_id)
            liq = liquidity_class_for_domain(account.account_domain)
            rows.append(
                {
                    "statement_id": str(statement.statement_id),
                    "institution": account.institution,
                    "account_type": account.account_type.value,
                    "account_domain": account.account_domain.value,
                    "liquidity_class": liq,
                    "liquidity_label": liquidity_label(liq),
                    "account_mask": account.account_mask,
                    "start_date": statement.statement_start_date.isoformat(),
                    "end_date": statement.statement_end_date.isoformat(),
                    "opening_balance_cents": statement.opening_balance_cents,
                    "closing_balance_cents": statement.closing_balance_cents,
                    "net_change_cents": statement.net_change_cents,
                }
            )
    return rows


def _sum_closing_for_liquidity(
    accounts: list[dict[str, Any]], liquidity_class: str
) -> int:
    """Sum closing balances for one liquidity bucket (integer cents only)."""
    total = 0
    for row in accounts:
        if row["liquidity_class"] != liquidity_class:
            continue
        closing = row["closing_balance_cents"]
        if not isinstance(closing, int):
            raise TokenError(
                f"closing_balance_cents must be int, got {type(closing).__name__}"
            )
        total += closing
    return total


def _render_overview_html() -> str:
    """Templated account overview or empty state (never fake zero portfolio)."""
    try:
        accounts = _overview_accounts()
        with get_db_connection() as conn:
            available = ready_to_assign_cents(conn)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Dashboard overview query failed",
            extra={"error_type": type(exc).__name__},
        )
        return env.get_template("upload_error.html").render(
            error_code="OVERVIEW_QUERY_FAILED",
            detail="Could not load account overview from the Vault database.",
        )
    if not accounts:
        return env.get_template("account_overview_empty.html").render()
    liquid_closing = _sum_closing_for_liquidity(accounts, LIQUIDITY_LIQUID)
    brokerage_closing = _sum_closing_for_liquidity(accounts, LIQUIDITY_BROKERAGE)
    liquid_net = sum(
        int(row["net_change_cents"])
        for row in accounts
        if row["liquidity_class"] == LIQUIDITY_LIQUID
    )
    last_row = max(accounts, key=lambda row: row["end_date"])
    return env.get_template("account_overview.html").render(
        accounts=accounts,
        account_count=len(accounts),
        liquid_closing_cents=liquid_closing,
        liquid_net_change_cents=liquid_net,
        brokerage_closing_cents=brokerage_closing,
        ready_to_assign_cents=available,
        last_statement={
            "end_date": last_row["end_date"],
            "institution": last_row["institution"],
            "account_mask": last_row["account_mask"],
        },
    )


def _render_budget_html(
    *, error_code: str | None = None, error_detail: str | None = None
) -> str:
    """Templated Budget view: Ready to Assign, assign form, envelopes.

    Integer cents are rendered as USD. When ``error_code`` is given the view is
    rendered with an inline alert so the forms stay available for a retry.
    """
    try:
        with get_db_connection() as conn:
            available = ready_to_assign_cents(conn)
            envelopes = list_envelopes(conn)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Dashboard budget query failed",
            extra={"error_type": type(exc).__name__},
        )
        return env.get_template("upload_error.html").render(
            error_code="BUDGET_QUERY_FAILED",
            detail="Could not load budget envelopes from the Vault database.",
        )
    return env.get_template("budget_result.html").render(
        ready_to_assign_cents=available,
        envelopes=[
            {
                "envelope_id": str(item.envelope_id),
                "name": item.name,
                "balance_cents": item.balance_cents,
            }
            for item in envelopes
        ],
        error_code=error_code,
        error_detail=error_detail,
    )


def _statement_rows(*, limit: int = 10) -> list[dict[str, Any]]:
    """Newest statements (header fields and row counts only, no transactions)."""
    with get_db_connection() as conn:
        recent = list_recent_statements(conn, limit=limit)
    return [
        {
            "statement_id": str(statement.statement_id),
            "institution": account.institution,
            "account_type": account.account_type.value,
            "account_mask": account.account_mask,
            "start_date": statement.statement_start_date.isoformat(),
            "end_date": statement.statement_end_date.isoformat(),
            "transaction_count": txn_count,
        }
        for account, statement, txn_count in recent
    ]


def _render_statements_html() -> str:
    """Statements view: upload form plus recent statements."""
    try:
        rows = _statement_rows()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Dashboard statements query failed",
            extra={"error_type": type(exc).__name__},
        )
        return env.get_template("upload_error.html").render(
            error_code="STATEMENTS_QUERY_FAILED",
            detail="Could not load recent statements from the Vault database.",
        )
    return env.get_template("statements.html").render(statements=rows)


def _render_dashboard() -> str:
    """Render the dashboard shell with the main pane prepopulated from Vault."""
    return env.get_template("dashboard.html").render(
        vault_pane_html=_render_overview_html(),
    )


def _ledger_blocks_for_run(run_id: UUID) -> list[dict[str, Any]]:
    """Build templated ledger dicts for every statement on a run."""
    blocks: list[dict[str, Any]] = []
    with get_db_connection() as conn:
        for account, statement, _txn_count in get_run_statements(conn, run_id):
            loaded = get_canonical_statement(conn, statement.statement_id)
            if loaded is None:
                continue
            _stmt, _summary, transactions = loaded
            holdings = get_statement_holdings(conn, statement.statement_id)
            blocks.append(_ledger_block(account, statement, transactions, holdings))
    return blocks


def _ledger_block(
    account: Any,
    statement: Any,
    transactions: Any,
    holdings: Any,
) -> dict[str, Any]:
    """Build one ledger card dict, including liquid vs non-liquid label."""
    liq = liquidity_class_for_domain(account.account_domain)
    return {
        "institution": account.institution,
        "account_type": account.account_type.value,
        "account_domain": account.account_domain.value,
        "liquidity_class": liq,
        "liquidity_label": liquidity_label(liq),
        "account_mask": account.account_mask,
        "start_date": statement.statement_start_date.isoformat(),
        "end_date": statement.statement_end_date.isoformat(),
        "opening_balance_cents": statement.opening_balance_cents,
        "closing_balance_cents": statement.closing_balance_cents,
        "net_change_cents": statement.net_change_cents,
        "transactions": [
            {
                "post_date": txn.post_date.isoformat(),
                "category": txn.transaction_category.value,
                "description": txn.description,
                "amount_cents": txn.amount_cents,
            }
            for txn in transactions
        ],
        "holdings": [
            {
                "symbol": holding.symbol,
                "description": holding.description,
                "quantity_nanos": holding.quantity_nanos,
                "market_value_cents": holding.market_value_cents,
            }
            for holding in holdings
        ],
    }


def _ledger_blocks_recent(*, limit: int = 5) -> list[dict[str, Any]]:
    """Build templated ledger dicts for the newest statements."""
    blocks: list[dict[str, Any]] = []
    with get_db_connection() as conn:
        recent = list_recent_statements(conn, limit=limit)
        for account, statement, _txn_count in recent:
            loaded = get_canonical_statement(conn, statement.statement_id)
            if loaded is None:
                continue
            _stmt, _summary, transactions = loaded
            holdings = get_statement_holdings(conn, statement.statement_id)
            blocks.append(_ledger_block(account, statement, transactions, holdings))
    return blocks


def _render_ledger_html(
    *,
    ledgers: list[dict[str, Any]],
    run_id: UUID | None = None,
    adapter_id: str | None = None,
) -> str:
    if not ledgers:
        return env.get_template("ledger_empty.html").render()
    return env.get_template("ledger_result.html").render(
        ledgers=ledgers,
        run_id=str(run_id) if run_id else None,
        adapter_id=adapter_id or "",
    )


@router.get("/dashboard", response_class=HTMLResponse)
def serve_dashboard() -> HTMLResponse:
    """Serve the main HTMX dashboard with account overview prepopulated."""
    logger.info("Dashboard page requested", extra={"surface": "account_overview"})
    return HTMLResponse(content=_render_dashboard())


@dashboard_api.get(
    "/overview",
    response_class=HTMLResponse,
    responses={400: {"description": "Requires HTMX or overview query failed"}},
)
async def get_account_overview(request: Request) -> HTMLResponse:
    """Return the account overview HTML fragment for Pane 2."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )
    html = _render_overview_html()
    status_code = (
        status.HTTP_400_BAD_REQUEST
        if "OVERVIEW_QUERY_FAILED" in html
        else status.HTTP_200_OK
    )
    return HTMLResponse(content=html, status_code=status_code)


@dashboard_api.get(
    "/budget",
    response_class=HTMLResponse,
    responses={400: {"description": "Requires HTMX or budget query failed"}},
)
async def get_budget(request: Request) -> HTMLResponse:
    """Return the Budget / envelopes HTML fragment for Pane 2."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )
    logger.info("Dashboard budget pane requested", extra={"surface": "budget"})
    html = _render_budget_html()
    status_code = (
        status.HTTP_400_BAD_REQUEST
        if "BUDGET_QUERY_FAILED" in html
        else status.HTTP_200_OK
    )
    return HTMLResponse(content=html, status_code=status_code)


@dashboard_api.post(
    "/budget/assign",
    response_class=HTMLResponse,
    responses={400: {"description": "Over-assign, invalid amount, or missing HTMX"}},
)
async def post_budget_assign(
    request: Request,
    envelope_id: Annotated[str, Form()],
    amount_usd: Annotated[str, Form()],
) -> HTMLResponse:
    """Assign USD form amount into an envelope; return Budget fragment."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    try:
        parsed_id = UUID(envelope_id.strip())
        amount_cents = parse_usd_form_to_cents(amount_usd)
    except (TokenError, ValueError) as exc:
        logger.warning(
            "Budget assign rejected invalid input",
            extra={"error_type": type(exc).__name__},
        )
        return HTMLResponse(
            content=_render_budget_html(
                error_code="INVALID_AMOUNT",
                error_detail="Amount must be positive USD (e.g. 50 or 50.00).",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        with get_db_connection() as conn:
            assign_to_envelope(
                conn,
                envelope_id=parsed_id,
                amount_cents=amount_cents,
            )
    except AssignExceedsAvailableError:
        logger.warning(
            "Budget assign exceeded Ready to Assign",
            extra={"envelope_id": str(parsed_id)},
        )
        return HTMLResponse(
            content=_render_budget_html(
                error_code="ASSIGN_EXCEEDS_AVAILABLE",
                error_detail="Assign amount exceeds Ready to Assign. No silent clamp.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    except TokenError as exc:
        code = (
            "ENVELOPE_NOT_FOUND"
            if "ENVELOPE_NOT_FOUND" in str(exc)
            else "INVALID_AMOUNT"
        )
        return HTMLResponse(
            content=_render_budget_html(error_code=code, error_detail=str(exc)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return HTMLResponse(content=_render_budget_html())


@dashboard_api.post(
    "/budget/envelopes",
    response_class=HTMLResponse,
    responses={400: {"description": "Blank name or missing HTMX"}},
)
async def post_budget_envelope(
    request: Request,
    name: Annotated[str, Form()],
) -> HTMLResponse:
    """Create a named envelope (thin prerequisite for assign)."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )
    try:
        with get_db_connection() as conn:
            create_envelope(conn, name=name)
    except TokenError as exc:
        return HTMLResponse(
            content=_render_budget_html(
                error_code="INVALID_ENVELOPE_NAME",
                error_detail=str(exc),
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return HTMLResponse(content=_render_budget_html())


@dashboard_api.get(
    "/statements",
    response_class=HTMLResponse,
    responses={400: {"description": "Requires HTMX or statements query failed"}},
)
async def get_statements(request: Request) -> HTMLResponse:
    """Return the Statements view (upload form + recent statements)."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )
    logger.info("Dashboard statements view requested", extra={"surface": "statements"})
    html = _render_statements_html()
    status_code = (
        status.HTTP_400_BAD_REQUEST
        if "STATEMENTS_QUERY_FAILED" in html
        else status.HTTP_200_OK
    )
    return HTMLResponse(content=html, status_code=status_code)


@dashboard_api.get(
    "/audit",
    response_class=HTMLResponse,
    responses={400: {"description": "Requires HTMX request"}},
)
async def get_audit(request: Request) -> HTMLResponse:
    """Return the Audit view (ledger refresh + frontier verification forms)."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )
    logger.info("Dashboard audit view requested", extra={"surface": "audit"})
    return HTMLResponse(content=env.get_template("audit.html").render())


@dashboard_api.get(
    "/statements/{statement_id}",
    response_class=HTMLResponse,
    responses={400: {"description": "Unknown statement or missing HTMX"}},
)
async def get_statement_ledger(
    request: Request,
    statement_id: UUID,
) -> HTMLResponse:
    """Drill-in: templated transaction ledger for one statement."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    logger.info(
        "Dashboard statement drill-in",
        extra={"statement_id": str(statement_id)},
    )

    try:
        with get_db_connection() as conn:
            loaded = get_canonical_statement(conn, statement_id)
            if loaded is None:
                return HTMLResponse(
                    content=env.get_template("upload_error.html").render(
                        error_code="STATEMENT_NOT_FOUND",
                        detail="No statement exists for the requested id.",
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            statement, _summary, transactions = loaded
            account = None
            for acc, stmt, _count in get_run_statements(conn, statement.run_id):
                if stmt.statement_id == statement_id:
                    account = acc
                    break
            if account is None:
                return HTMLResponse(
                    content=env.get_template("upload_error.html").render(
                        error_code="STATEMENT_NOT_FOUND",
                        detail="Statement account header could not be resolved.",
                    ),
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            holdings = get_statement_holdings(conn, statement_id)
            block = _ledger_block(account, statement, transactions, holdings)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Dashboard statement drill-in failed",
            extra={"error_type": type(exc).__name__},
        )
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="LEDGER_QUERY_FAILED",
                detail="Could not load statement ledger from the Vault database.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return HTMLResponse(content=_render_ledger_html(ledgers=[block]))


@dashboard_api.post(
    "/analyze",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Invalid or missing question"},
    },
)
async def analyze_question(
    request: Request,
    question: str = Form(""),
) -> HTMLResponse:
    """Return templated latest Vault ledger (question kept for HTMX form compat).

    Validates HTMX and non-empty question, then renders DB-backed ledger rows —
    never hardcoded fake metrics.
    """
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    stripped = question.strip() if question else ""
    if not stripped:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Question must not be empty or whitespace.",
        )

    logger.info(
        "Dashboard ledger request",
        extra={"question_length": len(stripped)},
    )

    try:
        ledgers = _ledger_blocks_recent(limit=5)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Dashboard ledger query failed",
            extra={"error_type": type(exc).__name__},
        )
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="LEDGER_QUERY_FAILED",
                detail="Could not load statements from the Vault database.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    return HTMLResponse(content=_render_ledger_html(ledgers=ledgers))


@dashboard_api.post(
    "/upload",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Invalid upload or missing HTMX header"},
    },
)
async def upload_statement_html(
    request: Request,
    file: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    """Ingest a PDF via the dashboard and swap a templated ledger into Pane 2."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    if file is None or not file.filename:
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="MISSING_FILE",
                detail="Choose a PDF statement to upload.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if not file.filename.lower().endswith(".pdf"):
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="INVALID_FILE",
                detail="Only PDF documents are supported for ingestion.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    content = await file.read()
    if not content:
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="EMPTY_FILE",
                detail="Uploaded file payload is empty.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if not content.startswith(b"%PDF"):
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="INVALID_PDF",
                detail="Uploaded file is not a valid PDF binary.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    safe_basename = Path(file.filename).name
    logger.info(
        "Dashboard statement upload",
        extra={"original_basename": safe_basename, "byte_length": len(content)},
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(content)

    try:
        result = ingest_pdf_path(tmp_path, safe_basename)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    if isinstance(result, IngestFailure):
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code=result.error_code,
                detail=result.detail,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    assert isinstance(result, IngestSuccess)
    ledgers = _ledger_blocks_for_run(result.run_id)
    return HTMLResponse(
        content=_render_ledger_html(
            ledgers=ledgers,
            run_id=result.run_id,
            adapter_id=result.adapter_id,
        )
    )


@dashboard_api.get(
    "/status",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Requires HTMX request"},
    },
)
async def get_status(request: Request) -> HTMLResponse:
    """Return a status HTML fragment for the Local Vault pane."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    return HTMLResponse(content=env.get_template("status.html").render())


def _frontier_audit_error(
    detail: str, *, error_code: str = "INVALID_GROUP_BY"
) -> HTMLResponse:
    """Return the rejected frontier fragment with HTTP 400."""
    return HTMLResponse(
        content=env.get_template("frontier_audit_error.html").render(
            detail=detail,
            error_code=error_code,
        ),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@dashboard_api.post(
    "/frontier-audit",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Missing HTMX header or invalid group_by"},
    },
)
async def frontier_audit(
    request: Request,
    group_by: str = Form(""),
) -> HTMLResponse:
    """Return a Tier-2 aggregate fragment for the frontier audit button.

    The fragment contains integer-cent totals only. Row-level fields are not
    rendered. Invalid grouping fails loudly.
    """
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    stripped = group_by.strip() if group_by else ""
    if not stripped:
        return _frontier_audit_error("group_by must not be empty or whitespace.")

    logger.info(
        "Dashboard frontier audit requested",
        extra={"group_by": stripped},
    )

    try:
        summary = get_financial_summary(group_by=stripped)
    except CloudToolError as exc:
        logger.warning(
            "Dashboard frontier audit rejected",
            extra={"group_by": stripped, "error_type": type(exc).__name__},
        )
        return _frontier_audit_error(str(exc), error_code="CLOUD_TOOL_ERROR")

    totals = summary["totals"]
    return HTMLResponse(
        content=env.get_template("frontier_audit_result.html").render(
            group_by=summary["group_by_column"],
            totals=totals,
        )
    )
