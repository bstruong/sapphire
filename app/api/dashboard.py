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
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.budget.envelopes import (
    assign_to_envelope,
    create_envelope,
    list_envelopes,
    ready_to_assign_cents,
)
from app.db.connection import get_db_connection
from app.db.history import (
    list_account_balance_series,
    list_available_spend_months,
    list_category_month_totals,
    list_liquid_closing_by_period,
    list_spend_category_month_totals,
)
from app.db.repository import (
    get_canonical_statement,
    get_run_statements,
    get_statement_holdings,
    list_accounts_with_statements,
    list_recent_statements,
    list_statement_history,
    list_statement_years,
)
from app.mcp.cloud_tools import CloudToolError, get_financial_summary
from app.models.exceptions import AssignExceedsAvailableError, TokenError
from app.pipeline.ingest_service import (
    MAX_BATCH_FILES,
    BatchItemResult,
    IngestFailure,
    IngestSuccess,
    ingest_pdf_batch,
    ingest_pdf_path,
)
from app.presentation.history_charts import (
    render_balance_history_svg,
    render_category_history_svg,
    render_spend_category_pie_svg,
)
from app.presentation.label_format import format_snake_label
from app.presentation.liquidity import (
    LIQUIDITY_BROKERAGE,
    LIQUIDITY_LIQUID,
    liquidity_class_for_domain,
    liquidity_label,
)
from app.presentation.money_format import format_usd_cents
from app.presentation.usd_parse import parse_usd_form_to_cents
from app.spend.categories import (
    clear_manual_label,
    get_labels_by_transaction_ids,
    list_spend_categories,
    set_manual_label,
)
from app.spend.suggest import (
    SuggestBatch,
    accept_all_rule_suggestions,
    accept_suggestion_group,
    load_batch,
    month_scope_counts,
    reject_all_vault_suggestions,
    reject_suggestion_group,
    run_suggest,
)

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


def _parse_pie_year_month(
    *,
    year: int | None,
    month: int | None,
    available: list[str],
) -> tuple[int, int] | None:
    """Resolve pie calendar month; default to latest available spend month."""
    if year is not None or month is not None:
        if year is None or month is None:
            raise TokenError("PIE_MONTH_INCOMPLETE: year and month are both required")
        if type(year) is not int or type(month) is not int:
            raise TokenError("PIE_MONTH_INVALID: year and month must be int")
        if month < 1 or month > 12:
            raise TokenError("PIE_MONTH_INVALID: month must be 1..12")
        return year, month
    if not available:
        return None
    latest = available[-1]
    y_str, m_str = latest.split("-", 1)
    return int(y_str), int(m_str)


def _overview_history_context(
    *,
    pie_year: int | None = None,
    pie_month: int | None = None,
) -> dict[str, Any]:
    """Build History section context (SVGs or loud empty codes)."""
    with get_db_connection() as conn:
        liquid_series = list_liquid_closing_by_period(conn)
        account_series = list_account_balance_series(conn)
        category_months = list_category_month_totals(conn)
        available_months = list_available_spend_months(conn)
        selected = _parse_pie_year_month(
            year=pie_year, month=pie_month, available=available_months
        )
        pie_slices = (
            list_spend_category_month_totals(conn, year=selected[0], month=selected[1])
            if selected is not None
            else []
        )

    balance_svg: str | None = None
    balance_empty_code: str | None = None
    if len(liquid_series) >= 2:
        balance_svg = render_balance_history_svg(
            liquid_series, account_series=account_series
        )
    else:
        balance_empty_code = "HISTORY_BALANCE_INSUFFICIENT"

    category_svg: str | None = None
    category_empty_code: str | None = None
    if category_months:
        category_svg = render_category_history_svg(category_months)
    else:
        category_empty_code = "HISTORY_CATEGORY_EMPTY"

    pie_svg: str | None = None
    pie_empty_code: str | None = None
    pie_legend: list[dict[str, Any]] = []
    pie_total_cents = 0
    pie_label = ""
    pie_year_out: int | None = None
    pie_month_out: int | None = None
    if selected is None:
        pie_empty_code = "HISTORY_SPEND_CATEGORY_EMPTY"
    elif not pie_slices:
        pie_year_out, pie_month_out = selected
        pie_empty_code = "HISTORY_SPEND_CATEGORY_EMPTY"
        pie_label = f"{pie_year_out:04d}-{pie_month_out:02d}"
    else:
        pie_year_out, pie_month_out = selected
        pie_svg = render_spend_category_pie_svg(
            pie_slices, year=pie_year_out, month=pie_month_out
        )
        pie_total_cents = sum(s.spend_cents for s in pie_slices)
        month_names = (
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        )
        pie_label = f"{month_names[pie_month_out - 1]} {pie_year_out}"
        for slice_ in pie_slices:
            pct = slice_.spend_cents * 100 // pie_total_cents if pie_total_cents else 0
            pie_legend.append(
                {
                    "slug": slice_.slug,
                    "name": slice_.display_name,
                    "cents": slice_.spend_cents,
                    "usd": format_usd_cents(slice_.spend_cents),
                    "pct": pct,
                    "color": slice_.color,
                    "uncategorized": slice_.slug == "uncategorized",
                }
            )

    # Prev/next month keys among available spend months.
    pie_prev: str | None = None
    pie_next: str | None = None
    if selected is not None and available_months:
        key = f"{selected[0]:04d}-{selected[1]:02d}"
        if key in available_months:
            idx = available_months.index(key)
            if idx > 0:
                pie_prev = available_months[idx - 1]
            if idx < len(available_months) - 1:
                pie_next = available_months[idx + 1]

    available_set = set(available_months)
    year_min = int(available_months[0][:4]) if available_months else None
    year_max = int(available_months[-1][:4]) if available_months else None

    logger.info(
        "Dashboard overview history prepared",
        extra={
            "liquid_periods": len(liquid_series),
            "category_months": len(category_months),
            "balance_ready": balance_svg is not None,
            "category_ready": category_svg is not None,
            "pie_ready": pie_svg is not None,
            "pie_months": len(available_months),
        },
    )
    return {
        "balance_svg": balance_svg,
        "balance_empty_code": balance_empty_code,
        "category_svg": category_svg,
        "category_empty_code": category_empty_code,
        "account_series_labels": [
            f"{s.institution} {s.account_mask}" for s in account_series
        ],
        "pie_svg": pie_svg,
        "pie_empty_code": pie_empty_code,
        "pie_legend": pie_legend,
        "pie_total_cents": pie_total_cents,
        "pie_total_usd": format_usd_cents(pie_total_cents) if pie_total_cents else "",
        "pie_label": pie_label,
        "pie_year": pie_year_out,
        "pie_month": pie_month_out,
        "pie_prev": pie_prev,
        "pie_next": pie_next,
        "pie_available_months": available_months,
        "pie_available_set": available_set,
        "pie_year_min": year_min,
        "pie_year_max": year_max,
    }


def _render_overview_html(
    *,
    pie_year: int | None = None,
    pie_month: int | None = None,
) -> str:
    """Templated account overview or empty state (never fake zero portfolio)."""
    try:
        accounts = _overview_accounts()
        with get_db_connection() as conn:
            available = ready_to_assign_cents(conn)
        history = (
            _overview_history_context(pie_year=pie_year, pie_month=pie_month)
            if accounts
            else None
        )
    except TokenError as exc:
        logger.warning(
            "Dashboard overview rejected pie month",
            extra={"error_type": type(exc).__name__},
        )
        return env.get_template("upload_error.html").render(
            error_code="PIE_MONTH_INVALID",
            detail=str(exc),
        )
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
        history=history,
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


def _statement_row_dict(account: Any, statement: Any, txn_count: int) -> dict[str, Any]:
    """Serialize one statement list row (no transaction bodies)."""
    return {
        "statement_id": str(statement.statement_id),
        "account_id": str(account.account_id),
        "institution": account.institution,
        "account_type": account.account_type.value,
        "account_mask": account.account_mask,
        "start_date": statement.statement_start_date.isoformat(),
        "end_date": statement.statement_end_date.isoformat(),
        "year": statement.statement_end_date.year,
        "transaction_count": txn_count,
    }


def _statement_rows(*, limit: int = 10) -> list[dict[str, Any]]:
    """Newest statements (header fields and row counts only, no transactions)."""
    with get_db_connection() as conn:
        recent = list_recent_statements(conn, limit=limit)
    return [
        _statement_row_dict(account, statement, txn_count)
        for account, statement, txn_count in recent
    ]


def _group_statements_by_year(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group statement rows into newest-first year buckets for the history UI."""
    by_year: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        by_year.setdefault(int(row["year"]), []).append(row)
    return [
        {"year": year, "count": len(by_year[year]), "statements": by_year[year]}
        for year in sorted(by_year.keys(), reverse=True)
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


def _render_statement_history_html(
    *,
    account_id: UUID | None = None,
    year: int | None = None,
) -> str:
    """Full statement archive with account/year filters and year groups."""
    try:
        with get_db_connection() as conn:
            history = list_statement_history(conn, account_id=account_id, year=year)
            accounts = list_accounts_with_statements(conn)
            years = list_statement_years(conn)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Dashboard statement history query failed",
            extra={"error_type": type(exc).__name__},
        )
        return env.get_template("upload_error.html").render(
            error_code="STATEMENT_HISTORY_QUERY_FAILED",
            detail="Could not load statement history from the Vault database.",
        )

    rows = [
        _statement_row_dict(account, statement, txn_count)
        for account, statement, txn_count in history
    ]
    account_options = [
        {
            "account_id": str(acc.account_id),
            "label": (
                f"{acc.institution} · "
                f"{format_snake_label(acc.account_type.value)} · "
                f"{acc.account_mask}"
            ),
        }
        for acc in accounts
    ]
    return env.get_template("statement_history.html").render(
        statements=rows,
        year_groups=_group_statements_by_year(rows),
        total_count=len(rows),
        account_options=account_options,
        year_options=years,
        selected_account=str(account_id) if account_id else "all",
        selected_year=str(year) if year is not None else "all",
        has_any_statements=bool(accounts),
    )


def _render_dashboard() -> str:
    """Render the dashboard shell with the main pane prepopulated from Vault."""
    return env.get_template("dashboard.html").render(
        vault_pane_html=_render_overview_html(),
    )


def _ledger_blocks_for_run(run_id: UUID) -> list[dict[str, Any]]:
    """Build templated ledger dicts for every statement on a run."""
    blocks: list[dict[str, Any]] = []
    with get_db_connection() as conn:
        categories = list_spend_categories(conn)
        for account, statement, _txn_count in get_run_statements(conn, run_id):
            loaded = get_canonical_statement(conn, statement.statement_id)
            if loaded is None:
                continue
            _stmt, _summary, transactions = loaded
            holdings = get_statement_holdings(conn, statement.statement_id)
            labels = get_labels_by_transaction_ids(
                conn, [txn.transaction_id for txn in transactions]
            )
            blocks.append(
                _ledger_block(
                    account,
                    statement,
                    transactions,
                    holdings,
                    spend_labels=labels,
                    spend_categories=categories,
                )
            )
    return blocks


def _ledger_block(
    account: Any,
    statement: Any,
    transactions: Any,
    holdings: Any,
    *,
    spend_labels: dict[Any, Any] | None = None,
    spend_categories: list[Any] | None = None,
) -> dict[str, Any]:
    """Build one ledger card dict, including liquid vs non-liquid label."""
    liq = liquidity_class_for_domain(account.account_domain)
    labels = spend_labels or {}
    return {
        "institution": account.institution,
        "account_type": account.account_type.value,
        "account_domain": account.account_domain.value,
        "liquidity_class": liq,
        "liquidity_label": liquidity_label(liq),
        "account_mask": account.account_mask,
        "statement_id": str(statement.statement_id),
        "start_date": statement.statement_start_date.isoformat(),
        "end_date": statement.statement_end_date.isoformat(),
        "opening_balance_cents": statement.opening_balance_cents,
        "closing_balance_cents": statement.closing_balance_cents,
        "net_change_cents": statement.net_change_cents,
        "spend_categories": [
            {"slug": cat.slug, "display_name": cat.display_name}
            for cat in (spend_categories or [])
        ],
        "transactions": [
            {
                "transaction_id": str(txn.transaction_id),
                "post_date": txn.post_date.isoformat(),
                "category": txn.transaction_category.value,
                "description": txn.description,
                "payee": txn.payee,
                "amount_cents": txn.amount_cents,
                "spend_category_slug": (
                    labels[txn.transaction_id].slug
                    if txn.transaction_id in labels
                    else ""
                ),
                "spend_category_display": (
                    labels[txn.transaction_id].display_name
                    if txn.transaction_id in labels
                    else "Uncategorized"
                ),
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
        categories = list_spend_categories(conn)
        recent = list_recent_statements(conn, limit=limit)
        for account, statement, _txn_count in recent:
            loaded = get_canonical_statement(conn, statement.statement_id)
            if loaded is None:
                continue
            _stmt, _summary, transactions = loaded
            holdings = get_statement_holdings(conn, statement.statement_id)
            labels = get_labels_by_transaction_ids(
                conn, [txn.transaction_id for txn in transactions]
            )
            blocks.append(
                _ledger_block(
                    account,
                    statement,
                    transactions,
                    holdings,
                    spend_labels=labels,
                    spend_categories=categories,
                )
            )
    return blocks


def _default_suggest_year_month(
    ledgers: list[dict[str, Any]],
) -> tuple[int, int]:
    """Prefer the newest ledger end month; fall back to a fixed dogfood month."""
    end_dates: list[str] = []
    for block in ledgers:
        end = block.get("end_date")
        if isinstance(end, str) and len(end) >= 7:
            end_dates.append(end)
    if end_dates:
        latest = max(end_dates)
        return int(latest[0:4]), int(latest[5:7])
    return 2026, 10


def _suggest_context_idle(
    *,
    year: int,
    month: int,
    categories: list[Any] | None = None,
) -> dict[str, Any]:
    with get_db_connection() as conn:
        cats = categories if categories is not None else list_spend_categories(conn)
        unlabeled, opaque, suggestable = month_scope_counts(
            conn, year=year, month=month
        )
    return {
        "has_run": False,
        "batch_id": "",
        "pie_year": year,
        "pie_month": month,
        "unlabeled_count": unlabeled,
        "opaque_count": opaque,
        "suggestable_count": suggestable,
        "vault_available": False,
        "vault_skipped_reason": None,
        "rule_groups": [],
        "vault_groups": [],
        "categories": [{"slug": c.slug, "display_name": c.display_name} for c in cats],
    }


def _suggest_context_from_batch(
    batch: SuggestBatch, *, categories: list[Any]
) -> dict[str, Any]:
    return {
        "has_run": True,
        "batch_id": str(batch.batch_id),
        "pie_year": batch.pie_year,
        "pie_month": batch.pie_month,
        "unlabeled_count": batch.unlabeled_count,
        "opaque_count": batch.opaque_count,
        "suggestable_count": batch.suggestable_count,
        "vault_available": batch.vault_available,
        "vault_skipped_reason": batch.vault_skipped_reason,
        "rule_groups": [
            {
                "group_key": g.group_key,
                "payee_display": g.payee_display,
                "proposed_slug": g.proposed_slug,
                "display_name": g.display_name,
                "origin": g.origin,
                "evidence": g.evidence,
                "txn_count": g.txn_count,
                "abs_cents_total": g.abs_cents_total,
            }
            for g in batch.rule_groups
        ],
        "vault_groups": [
            {
                "group_key": g.group_key,
                "payee_display": g.payee_display,
                "proposed_slug": g.proposed_slug,
                "display_name": g.display_name,
                "origin": g.origin,
                "evidence": g.evidence,
                "txn_count": g.txn_count,
                "abs_cents_total": g.abs_cents_total,
            }
            for g in batch.vault_groups
        ],
        "categories": [
            {"slug": c.slug, "display_name": c.display_name} for c in categories
        ],
    }


def _render_suggest_panel(suggest: dict[str, Any]) -> str:
    return env.get_template("ledger_suggest_panel.html").render(suggest=suggest)


def _render_ledger_html(
    *,
    ledgers: list[dict[str, Any]],
    run_id: UUID | None = None,
    adapter_id: str | None = None,
    back_href: str = "/api/v1/dashboard/overview",
    back_label: str = "Back to overview",
    suggest: dict[str, Any] | None = None,
) -> str:
    if not ledgers:
        return env.get_template("ledger_empty.html").render()
    year, month = _default_suggest_year_month(ledgers)
    suggest_ctx = suggest or _suggest_context_idle(year=year, month=month)
    return env.get_template("ledger_result.html").render(
        ledgers=ledgers,
        run_id=str(run_id) if run_id else None,
        adapter_id=adapter_id or "",
        back_href=back_href,
        back_label=back_label,
        suggest=suggest_ctx,
    )


def _parse_pie_form_year_month(pie_year: str, pie_month: str) -> tuple[int, int]:
    try:
        year = int(str(pie_year).strip())
        month = int(str(pie_month).strip())
    except (TypeError, ValueError) as exc:
        raise TokenError("PIE_MONTH_INVALID: pie_year/pie_month must be ints") from exc
    if year < 1970 or year > 2100 or month < 1 or month > 12:
        raise TokenError("PIE_MONTH_INVALID: out of range")
    return year, month


def _require_htmx(request: Request) -> None:
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
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
async def get_account_overview(
    request: Request,
    pie_year: int | None = None,
    pie_month: int | None = None,
) -> HTMLResponse:
    """Return the account overview HTML fragment for Pane 2."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )
    html = _render_overview_html(pie_year=pie_year, pie_month=pie_month)
    status_code = (
        status.HTTP_400_BAD_REQUEST
        if ("OVERVIEW_QUERY_FAILED" in html or "PIE_MONTH_INVALID" in html)
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
    "/statements/history",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Requires HTMX, invalid filter, or history query failed"}
    },
)
async def get_statement_history(
    request: Request,
    account: str = "all",
    year: str = "all",
) -> HTMLResponse:
    """Full Vault statement archive with account and year filters."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    account_id: UUID | None = None
    account_raw = (account or "all").strip().lower()
    if account_raw not in {"", "all"}:
        try:
            account_id = UUID(account_raw)
        except ValueError:
            return HTMLResponse(
                content=env.get_template("upload_error.html").render(
                    error_code="INVALID_ACCOUNT_FILTER",
                    detail="Account filter must be a UUID or 'all'.",
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    year_value: int | None = None
    year_raw = (year or "all").strip().lower()
    if year_raw not in {"", "all"}:
        if not year_raw.isdigit():
            return HTMLResponse(
                content=env.get_template("upload_error.html").render(
                    error_code="INVALID_YEAR_FILTER",
                    detail="Year filter must be a four-digit year or 'all'.",
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        year_value = int(year_raw)
        if year_value < 1900 or year_value > 2100:
            return HTMLResponse(
                content=env.get_template("upload_error.html").render(
                    error_code="INVALID_YEAR_FILTER",
                    detail="Year filter is out of range.",
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    logger.info(
        "Dashboard statement history requested",
        extra={
            "surface": "statement_history",
            "has_account_filter": account_id is not None,
            "has_year_filter": year_value is not None,
        },
    )
    html = _render_statement_history_html(account_id=account_id, year=year_value)
    status_code = (
        status.HTTP_400_BAD_REQUEST
        if "STATEMENT_HISTORY_QUERY_FAILED" in html
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

    # Query param `from` is reserved in Python; read it from the request.
    origin = (request.query_params.get("from") or "").strip().lower()

    logger.info(
        "Dashboard statement drill-in",
        extra={"statement_id": str(statement_id), "from_view": origin or "default"},
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
            categories = list_spend_categories(conn)
            labels = get_labels_by_transaction_ids(
                conn, [txn.transaction_id for txn in transactions]
            )
            block = _ledger_block(
                account,
                statement,
                transactions,
                holdings,
                spend_labels=labels,
                spend_categories=categories,
            )
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

    if origin == "history":
        back_href = "/api/v1/dashboard/statements/history"
        back_label = "Back to statement history"
    elif origin == "statements":
        back_href = "/api/v1/dashboard/statements"
        back_label = "Back to statements"
    else:
        back_href = "/api/v1/dashboard/overview"
        back_label = "Back to overview"

    return HTMLResponse(
        content=_render_ledger_html(
            ledgers=[block],
            back_href=back_href,
            back_label=back_label,
        )
    )


@dashboard_api.post(
    "/ledger/label",
    response_class=HTMLResponse,
    responses={400: {"description": "Unknown slug/txn, or missing HTMX"}},
)
async def post_ledger_spend_label(
    request: Request,
    transaction_id: Annotated[str, Form()],
    statement_id: Annotated[str, Form()],
    spend_category_slug: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Assign or clear a manual spend category on one ledger transaction."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    origin = (request.query_params.get("from") or "").strip().lower()
    try:
        txn_uuid = UUID(transaction_id.strip())
        stmt_uuid = UUID(statement_id.strip())
    except ValueError:
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code="INVALID_LABEL_IDS",
                detail="transaction_id and statement_id must be UUIDs.",
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    slug = spend_category_slug.strip()
    try:
        with get_db_connection() as conn:
            if slug:
                set_manual_label(conn, transaction_id=txn_uuid, slug=slug)
            else:
                clear_manual_label(conn, transaction_id=txn_uuid)
            loaded = get_canonical_statement(conn, stmt_uuid)
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
                if stmt.statement_id == stmt_uuid:
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
            holdings = get_statement_holdings(conn, stmt_uuid)
            categories = list_spend_categories(conn)
            labels = get_labels_by_transaction_ids(
                conn, [txn.transaction_id for txn in transactions]
            )
            block = _ledger_block(
                account,
                statement,
                transactions,
                holdings,
                spend_labels=labels,
                spend_categories=categories,
            )
    except TokenError as exc:
        code = "SPEND_LABEL_REJECTED"
        text = str(exc)
        if "SPEND_CATEGORY_UNKNOWN" in text:
            code = "SPEND_CATEGORY_UNKNOWN"
        elif "TRANSACTION_NOT_FOUND" in text:
            code = "TRANSACTION_NOT_FOUND"
        elif "SPEND_CATEGORY_SLUG_REQUIRED" in text:
            code = "SPEND_CATEGORY_SLUG_REQUIRED"
        logger.warning(
            "Spend label rejected",
            extra={"error_code": code, "error_type": type(exc).__name__},
        )
        return HTMLResponse(
            content=env.get_template("upload_error.html").render(
                error_code=code,
                detail=text,
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    if origin == "history":
        back_href = "/api/v1/dashboard/statements/history"
        back_label = "Back to statement history"
    elif origin == "statements":
        back_href = "/api/v1/dashboard/statements"
        back_label = "Back to statements"
    else:
        back_href = "/api/v1/dashboard/overview"
        back_label = "Back to overview"

    return HTMLResponse(
        content=_render_ledger_html(
            ledgers=[block],
            back_href=back_href,
            back_label=back_label,
        )
    )


def _suggest_error_response(exc: TokenError) -> HTMLResponse:
    text = str(exc)
    code = "SUGGEST_REJECTED"
    if "PIE_MONTH_INVALID" in text:
        code = "PIE_MONTH_INVALID"
    elif "SPEND_CATEGORY_UNKNOWN" in text:
        code = "SPEND_CATEGORY_UNKNOWN"
    elif "OPAQUE_PAYEE_INELIGIBLE" in text:
        code = "OPAQUE_PAYEE_INELIGIBLE"
    elif "SUGGEST_BATCH_NOT_FOUND" in text:
        code = "SUGGEST_BATCH_NOT_FOUND"
    elif "SUGGEST_GROUP_NOT_FOUND" in text:
        code = "SUGGEST_GROUP_NOT_FOUND"
    logger.warning(
        "Spend suggest action rejected",
        extra={"error_code": code, "error_type": type(exc).__name__},
    )
    return HTMLResponse(
        content=env.get_template("upload_error.html").render(
            error_code=code,
            detail=text,
        ),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


@dashboard_api.post(
    "/ledger/suggest/run",
    response_class=HTMLResponse,
    responses={400: {"description": "Invalid month or missing HTMX"}},
)
async def post_ledger_suggest_run(
    request: Request,
    pie_year: Annotated[str, Form()] = "",
    pie_month: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """Stage rule (+ optional Vault) suggestions for a calendar month."""
    _require_htmx(request)
    try:
        year, month = _parse_pie_form_year_month(pie_year, pie_month)
        with get_db_connection() as conn:
            batch = run_suggest(conn, year=year, month=month)
            categories = list_spend_categories(conn)
            ctx = _suggest_context_from_batch(batch, categories=categories)
    except TokenError as exc:
        return _suggest_error_response(exc)
    return HTMLResponse(content=_render_suggest_panel(ctx))


@dashboard_api.post(
    "/ledger/suggest/accept",
    response_class=HTMLResponse,
    responses={400: {"description": "Invalid accept payload or missing HTMX"}},
)
async def post_ledger_suggest_accept(
    request: Request,
    batch_id: Annotated[str, Form()] = "",
    group_key: Annotated[str, Form()] = "",
    slug: Annotated[str, Form()] = "",
    pie_year: Annotated[str, Form()] = "",
    pie_month: Annotated[str, Form()] = "",
    vault_available: Annotated[str, Form()] = "0",
) -> HTMLResponse:
    """Accept one suggestion group; rewrite panel from remaining staging."""
    _require_htmx(request)
    try:
        year, month = _parse_pie_form_year_month(pie_year, pie_month)
        bid = UUID(batch_id.strip())
        with get_db_connection() as conn:
            accept_suggestion_group(
                conn, batch_id=bid, group_key=group_key.strip(), slug=slug
            )
            categories = list_spend_categories(conn)
            batch = load_batch(
                conn,
                batch_id=bid,
                year=year,
                month=month,
                vault_available=vault_available.strip() == "1",
                vault_skipped_reason=(
                    None if vault_available.strip() == "1" else "jarvis_unavailable"
                ),
            )
            ctx = _suggest_context_from_batch(batch, categories=categories)
    except (TokenError, ValueError) as exc:
        if isinstance(exc, ValueError):
            return _suggest_error_response(TokenError(f"Invalid batch_id: {exc}"))
        return _suggest_error_response(exc)
    return HTMLResponse(content=_render_suggest_panel(ctx))


@dashboard_api.post(
    "/ledger/suggest/reject",
    response_class=HTMLResponse,
    responses={400: {"description": "Invalid reject payload or missing HTMX"}},
)
async def post_ledger_suggest_reject(
    request: Request,
    batch_id: Annotated[str, Form()] = "",
    group_key: Annotated[str, Form()] = "",
    pie_year: Annotated[str, Form()] = "",
    pie_month: Annotated[str, Form()] = "",
    vault_available: Annotated[str, Form()] = "0",
) -> HTMLResponse:
    """Reject one suggestion group (staging only)."""
    _require_htmx(request)
    try:
        year, month = _parse_pie_form_year_month(pie_year, pie_month)
        bid = UUID(batch_id.strip())
        with get_db_connection() as conn:
            reject_suggestion_group(conn, batch_id=bid, group_key=group_key.strip())
            categories = list_spend_categories(conn)
            batch = load_batch(
                conn,
                batch_id=bid,
                year=year,
                month=month,
                vault_available=vault_available.strip() == "1",
                vault_skipped_reason=(
                    None if vault_available.strip() == "1" else "jarvis_unavailable"
                ),
            )
            ctx = _suggest_context_from_batch(batch, categories=categories)
    except (TokenError, ValueError) as exc:
        if isinstance(exc, ValueError):
            return _suggest_error_response(TokenError(f"Invalid batch_id: {exc}"))
        return _suggest_error_response(exc)
    return HTMLResponse(content=_render_suggest_panel(ctx))


@dashboard_api.post(
    "/ledger/suggest/accept-all-rules",
    response_class=HTMLResponse,
    responses={400: {"description": "Invalid batch or missing HTMX"}},
)
async def post_ledger_suggest_accept_all_rules(
    request: Request,
    batch_id: Annotated[str, Form()] = "",
    pie_year: Annotated[str, Form()] = "",
    pie_month: Annotated[str, Form()] = "",
    vault_available: Annotated[str, Form()] = "0",
) -> HTMLResponse:
    """Accept every staged rule group. Never bulk-accepts Vault."""
    _require_htmx(request)
    try:
        year, month = _parse_pie_form_year_month(pie_year, pie_month)
        bid = UUID(batch_id.strip())
        with get_db_connection() as conn:
            accept_all_rule_suggestions(conn, batch_id=bid)
            categories = list_spend_categories(conn)
            batch = load_batch(
                conn,
                batch_id=bid,
                year=year,
                month=month,
                vault_available=vault_available.strip() == "1",
                vault_skipped_reason=(
                    None if vault_available.strip() == "1" else "jarvis_unavailable"
                ),
            )
            ctx = _suggest_context_from_batch(batch, categories=categories)
    except (TokenError, ValueError) as exc:
        if isinstance(exc, ValueError):
            return _suggest_error_response(TokenError(f"Invalid batch_id: {exc}"))
        return _suggest_error_response(exc)
    return HTMLResponse(content=_render_suggest_panel(ctx))


@dashboard_api.post(
    "/ledger/suggest/reject-all-vault",
    response_class=HTMLResponse,
    responses={400: {"description": "Invalid batch or missing HTMX"}},
)
async def post_ledger_suggest_reject_all_vault(
    request: Request,
    batch_id: Annotated[str, Form()] = "",
    pie_year: Annotated[str, Form()] = "",
    pie_month: Annotated[str, Form()] = "",
    vault_available: Annotated[str, Form()] = "0",
) -> HTMLResponse:
    """Drop all Vault-staged suggestions for the batch."""
    _require_htmx(request)
    try:
        year, month = _parse_pie_form_year_month(pie_year, pie_month)
        bid = UUID(batch_id.strip())
        with get_db_connection() as conn:
            reject_all_vault_suggestions(conn, batch_id=bid)
            categories = list_spend_categories(conn)
            batch = load_batch(
                conn,
                batch_id=bid,
                year=year,
                month=month,
                vault_available=vault_available.strip() == "1",
                vault_skipped_reason=(
                    None if vault_available.strip() == "1" else "jarvis_unavailable"
                ),
            )
            ctx = _suggest_context_from_batch(batch, categories=categories)
    except (TokenError, ValueError) as exc:
        if isinstance(exc, ValueError):
            return _suggest_error_response(TokenError(f"Invalid batch_id: {exc}"))
        return _suggest_error_response(exc)
    return HTMLResponse(content=_render_suggest_panel(ctx))


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


def _upload_error_html(error_code: str, detail: str) -> HTMLResponse:
    return HTMLResponse(
        content=env.get_template("upload_error.html").render(
            error_code=error_code,
            detail=detail,
        ),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _render_batch_result_html(results: list[BatchItemResult]) -> str:
    succeeded = sum(1 for item in results if item.ok)
    failed = len(results) - succeeded
    rows = [
        {
            "filename": item.filename,
            "ok": item.ok,
            "statement_id": str(item.statement_id) if item.statement_id else None,
            "run_id": str(item.run_id) if item.run_id else None,
            "adapter_id": item.adapter_id or "",
            "error_code": item.error_code or "",
            "detail": item.detail or "",
        }
        for item in results
    ]
    # After a successful ingest, re-query History so the bulk result pane shows
    # updated balance/category charts without a full page reload (HTMX swap).
    history: dict[str, Any] | None = None
    history_error_code: str | None = None
    history_error_detail: str | None = None
    if succeeded > 0:
        try:
            if _overview_accounts():
                history = _overview_history_context()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Dashboard bulk upload history refresh failed",
                extra={"error_type": type(exc).__name__},
            )
            history_error_code = "HISTORY_REFRESH_FAILED"
            history_error_detail = (
                "Could not load Overview History after upload. Open Overview to retry."
            )
    return env.get_template("upload_batch_result.html").render(
        succeeded=succeeded,
        failed=failed,
        total=len(results),
        results=rows,
        history=history,
        history_error_code=history_error_code,
        history_error_detail=history_error_detail,
    )


async def _collect_upload_files(
    files: list[UploadFile] | None,
    file: UploadFile | None,
) -> list[UploadFile]:
    """Merge multi `files` parts with legacy single `file` into one list."""
    collected: list[UploadFile] = []
    if files:
        for upload in files:
            if upload is not None and upload.filename:
                collected.append(upload)
    if file is not None and file.filename and not any(u is file for u in collected):
        collected.append(file)
    return collected


@dashboard_api.post(
    "/upload",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Invalid upload or missing HTMX header"},
    },
)
async def upload_statement_html(
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    """Ingest one or more PDFs via the dashboard.

    One file keeps the existing ledger swap. Two or more return a batch
    summary with per-file OK/FAIL (partial success). Sequential ingest only.
    """
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    uploads = await _collect_upload_files(files, file)
    if not uploads:
        return _upload_error_html(
            "MISSING_FILE",
            "Choose a PDF statement to upload.",
        )

    if len(uploads) > MAX_BATCH_FILES:
        return _upload_error_html(
            "TOO_MANY_FILES",
            f"At most {MAX_BATCH_FILES} files may be uploaded per request "
            f"(received {len(uploads)}).",
        )

    # Single-file path: preserve ledger swap for demo continuity.
    if len(uploads) == 1:
        return await _upload_single_statement_html(uploads[0])

    batch_id = str(uuid4())
    logger.info(
        "Dashboard bulk statement upload",
        extra={"batch_id": batch_id, "file_count": len(uploads)},
    )

    temp_paths: list[tuple[Path, str]] = []
    try:
        for upload in uploads:
            safe_basename = Path(upload.filename or "upload.pdf").name
            content = await upload.read()
            logger.info(
                "Dashboard bulk file received",
                extra={
                    "batch_id": batch_id,
                    "original_basename": safe_basename,
                    "byte_length": len(content),
                },
            )
            suffix = ".pdf" if safe_basename.lower().endswith(".pdf") else ".bin"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(content)
            temp_paths.append((tmp_path, safe_basename))

        results = ingest_pdf_batch(temp_paths)
    finally:
        for tmp_path, _name in temp_paths:
            if tmp_path.exists():
                tmp_path.unlink()

    return HTMLResponse(content=_render_batch_result_html(results))


def _batch_item_to_dict(item: BatchItemResult) -> dict[str, Any]:
    """Serialize a batch item for progress JSON (no monetary fields)."""
    return {
        "filename": item.filename,
        "ok": item.ok,
        "statement_id": str(item.statement_id) if item.statement_id else None,
        "run_id": str(item.run_id) if item.run_id else None,
        "error_code": item.error_code,
        "detail": item.detail,
        "adapter_id": item.adapter_id,
    }


def _parse_batch_item_dict(raw: Any) -> BatchItemResult:
    """Fail loudly when a progress-summary row is missing required keys."""
    if not isinstance(raw, dict):
        raise TypeError("Each result must be an object")
    required = (
        "filename",
        "ok",
        "statement_id",
        "run_id",
        "error_code",
        "detail",
        "adapter_id",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"Missing keys: {', '.join(missing)}")
    if not isinstance(raw["filename"], str) or not raw["filename"]:
        raise ValueError("filename must be a non-empty string")
    if not isinstance(raw["ok"], bool):
        raise TypeError("ok must be a boolean")

    statement_id: UUID | None = None
    run_id: UUID | None = None
    if raw["statement_id"] is not None:
        statement_id = UUID(str(raw["statement_id"]))
    if raw["run_id"] is not None:
        run_id = UUID(str(raw["run_id"]))

    error_code = raw["error_code"]
    detail = raw["detail"]
    adapter_id = raw["adapter_id"]
    if error_code is not None and not isinstance(error_code, str):
        raise ValueError("error_code must be a string or null")
    if detail is not None and not isinstance(detail, str):
        raise ValueError("detail must be a string or null")
    if adapter_id is not None and not isinstance(adapter_id, str):
        raise ValueError("adapter_id must be a string or null")

    return BatchItemResult(
        filename=Path(raw["filename"]).name,
        ok=raw["ok"],
        statement_id=statement_id,
        run_id=run_id,
        error_code=error_code,
        detail=detail,
        adapter_id=adapter_id,
    )


async def _ingest_upload_to_batch_item(upload: UploadFile) -> BatchItemResult:
    """Ingest one uploaded PDF into a BatchItemResult (progress / bulk path)."""
    safe_basename = Path(upload.filename or "upload.pdf").name
    content = await upload.read()
    suffix = ".pdf" if safe_basename.lower().endswith(".pdf") else ".bin"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(content)
    try:
        return ingest_pdf_batch([(tmp_path, safe_basename)])[0]
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


async def _upload_single_statement_html(upload: UploadFile) -> HTMLResponse:
    """Legacy one-file dashboard ingest → ledger or error fragment."""
    if not upload.filename or not upload.filename.lower().endswith(".pdf"):
        return _upload_error_html(
            "INVALID_FILE",
            "Only PDF documents are supported for ingestion.",
        )

    content = await upload.read()
    if not content:
        return _upload_error_html(
            "EMPTY_FILE",
            "Uploaded file payload is empty.",
        )
    if not content.startswith(b"%PDF"):
        return _upload_error_html(
            "INVALID_PDF",
            "Uploaded file is not a valid PDF binary.",
        )

    safe_basename = Path(upload.filename).name
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
        return _upload_error_html(result.error_code, result.detail)

    assert isinstance(result, IngestSuccess)
    ledgers = _ledger_blocks_for_run(result.run_id)
    return HTMLResponse(
        content=_render_ledger_html(
            ledgers=ledgers,
            run_id=result.run_id,
            adapter_id=result.adapter_id,
        )
    )


@dashboard_api.post(
    "/upload-item",
    responses={
        400: {"description": "Invalid upload, multi-file payload, or missing HTMX"},
    },
)
async def upload_statement_item_json(
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> JSONResponse:
    """Ingest exactly one PDF and return a JSON batch item for progress UX.

    Used by the dashboard sequential bulk loop (N of M). Failures are explicit
    in the JSON body with taxonomy codes — never silently skipped.
    """
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    uploads = await _collect_upload_files(files, file)
    if not uploads:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "ok": False,
                "error_code": "MISSING_FILE",
                "detail": "Choose a PDF statement to upload.",
                "filename": "",
                "statement_id": None,
                "run_id": None,
                "adapter_id": None,
            },
        )
    if len(uploads) > 1:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "ok": False,
                "error_code": "TOO_MANY_FILES",
                "detail": "upload-item accepts exactly one file per request.",
                "filename": Path(uploads[0].filename or "upload.pdf").name,
                "statement_id": None,
                "run_id": None,
                "adapter_id": None,
            },
        )

    item = await _ingest_upload_to_batch_item(uploads[0])
    logger.info(
        "Dashboard upload-item completed",
        extra={
            "original_basename": item.filename,
            "ok": item.ok,
            "error_code": item.error_code,
        },
    )
    return JSONResponse(content=_batch_item_to_dict(item))


@dashboard_api.post(
    "/upload-summary",
    response_class=HTMLResponse,
    responses={
        400: {"description": "Malformed summary payload or missing HTMX"},
    },
)
async def upload_summary_html(request: Request) -> HTMLResponse:
    """Render the bulk upload result table from client-collected item JSON."""
    hx_request = request.headers.get("HX-Request", "").lower()
    if hx_request != "true":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This endpoint requires an HTMX request (HX-Request: true).",
        )

    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload summary payload must be valid JSON.",
        ) from exc

    if not isinstance(payload, dict) or "results" not in payload:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload summary requires a results array.",
        )
    raw_results = payload["results"]
    if not isinstance(raw_results, list) or not raw_results:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload summary results must be a non-empty array.",
        )
    if len(raw_results) > MAX_BATCH_FILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"At most {MAX_BATCH_FILES} results may be summarized.",
        )

    try:
        results = [_parse_batch_item_dict(row) for row in raw_results]
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Malformed upload summary row: {exc}",
        ) from exc

    return HTMLResponse(content=_render_batch_result_html(results))


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
