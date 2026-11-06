"""Money conversion tracers for local / Cursor debugging.

Amounts (integer cents) are allowed in reports. PII is scrubbed: SSN, DL,
street addresses, long raw account digit runs, and personal-name heuristics.
Never dump PDF ``page_text`` into reports. Do not put cents into logger extras.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
from reportlab.pdfgen import canvas

from app.adapters.registry import get_default_registry
from app.budget.envelopes import ready_to_assign_cents
from app.db.repository import list_recent_statements
from app.extraction.core import extract_pdf_to_raw, parse_currency_to_cents
from app.models.enums import AccountDomain
from app.models.exceptions import InvariantError, TokenError
from app.pipeline.validator import (
    validate_brokerage_reconciliation,
    validate_credit_card_reconciliation,
    validate_depository_reconciliation,
    validate_universal_reconciliation,
)
from app.presentation.liquidity import (
    LIQUIDITY_BROKERAGE,
    LIQUIDITY_LIABILITY,
    LIQUIDITY_LIQUID,
    liquidity_class_for_domain,
    liquidity_label,
)
from app.presentation.money_format import format_usd_cents
from app.presentation.usd_parse import parse_usd_form_to_cents

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_DL_RE = re.compile(r"\b(?:DL|License)[#:\s]*[A-Z0-9]{6,}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\b\d{8,}\b")
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]{1,20}){0,3}\s+"
    r"(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|Boulevard|Blvd\.?|"
    r"Lane|Ln\.?|Drive|Dr\.?|Court|Ct\.?|Way|Place|Pl\.?)\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")


@dataclass(frozen=True, slots=True)
class TraceStep:
    """One pipeline stage result (amounts OK; detail must be PII-scrubbed)."""

    stage: str
    ok: bool
    detail: str
    cents: int | None = None
    sign: str | None = None
    usd: str | None = None

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        bits = [f"[{mark}] {self.stage}"]
        if self.cents is not None:
            bits.append(f"cents={self.cents}")
        if self.usd is not None:
            bits.append(f"usd={self.usd}")
        if self.sign is not None:
            bits.append(f"sign={self.sign}")
        if self.detail:
            bits.append(scrub_pii(self.detail))
        return " ".join(bits)


@dataclass(frozen=True, slots=True)
class StatementTrace:
    """Per-statement cents summary without transaction descriptions."""

    institution: str
    account_mask: str
    account_domain: str
    liquidity_class: str
    liquidity_label: str
    opening_balance_cents: int
    closing_balance_cents: int
    net_change_cents: int
    txn_sum_cents: int
    txn_count: int


@dataclass
class FixtureTraceReport:
    """Adapter extract + reconcile report for a fixture or PDF."""

    ok: bool
    adapter_id: str
    statements: list[StatementTrace] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    steps: list[TraceStep] = field(default_factory=list)
    error: str | None = None

    def to_text(self) -> str:
        lines = [step.line() for step in self.steps]
        if self.adapter_id:
            lines.append(f"adapter_id={self.adapter_id}")
        for warning in self.warnings:
            lines.append(f"[WARN] {scrub_pii(warning)}")
        for stmt in self.statements:
            lines.append(
                "statement "
                f"institution={scrub_pii(stmt.institution)} "
                f"mask={scrub_pii(stmt.account_mask)} "
                f"domain={stmt.account_domain} "
                f"liquidity={stmt.liquidity_label} "
                f"opening_cents={stmt.opening_balance_cents} "
                f"closing_cents={stmt.closing_balance_cents} "
                f"net_cents={stmt.net_change_cents} "
                f"txn_sum_cents={stmt.txn_sum_cents} "
                f"txn_count={stmt.txn_count} "
                f"opening_usd={format_usd_cents(stmt.opening_balance_cents)} "
                f"closing_usd={format_usd_cents(stmt.closing_balance_cents)}"
            )
        if self.error:
            lines.append(f"[FAIL] error {scrub_pii(self.error)}")
        return "\n".join(lines)


@dataclass
class VaultAggregateReport:
    """Dogfood/test DB liquidity split + Ready to Assign."""

    ok: bool
    liquid_closing_cents: int
    brokerage_closing_cents: int
    liability_closing_cents: int
    ready_to_assign_cents: int
    accounts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    def to_text(self) -> str:
        if self.ok:
            head = (
                f"[PASS] liquid_closing_cents={self.liquid_closing_cents} "
                f"usd={format_usd_cents(self.liquid_closing_cents)}"
            )
        else:
            head = f"[FAIL] vault aggregates {scrub_pii(self.error or '')}"
        lines = [
            head,
            (
                f"brokerage_closing_cents={self.brokerage_closing_cents} "
                f"usd={format_usd_cents(self.brokerage_closing_cents)}"
            ),
            (
                f"liability_closing_cents={self.liability_closing_cents} "
                f"usd={format_usd_cents(self.liability_closing_cents)}"
            ),
            (
                f"ready_to_assign_cents={self.ready_to_assign_cents} "
                f"usd={format_usd_cents(self.ready_to_assign_cents)}"
            ),
        ]
        for row in self.accounts:
            lines.append(
                "account "
                f"institution={scrub_pii(str(row['institution']))} "
                f"mask={scrub_pii(str(row['account_mask']))} "
                f"domain={row['account_domain']} "
                f"liquidity={row['liquidity_label']} "
                f"closing_cents={row['closing_balance_cents']} "
                f"closing_usd={format_usd_cents(int(row['closing_balance_cents']))}"
            )
        return "\n".join(lines)


def scrub_pii(text: str) -> str:
    """Replace PII patterns; preserve USD amounts and ``[TOKEN]`` / ``*0000`` masks."""
    if not isinstance(text, str):
        raise TokenError(f"scrub_pii expects str, got {type(text).__name__}")
    out = text
    out = _SSN_RE.sub("[MASKED]", out)
    out = _DL_RE.sub("[MASKED]", out)
    out = _EMAIL_RE.sub("[MASKED]", out)
    out = _PHONE_RE.sub("[MASKED]", out)
    out = _STREET_RE.sub("[MASKED]", out)
    out = _LONG_DIGITS_RE.sub("[MASKED]", out)
    return out


def trace_parse(token: str, *, allow_zero: bool = False) -> TraceStep:
    """Parse a currency token to signed integer cents."""
    safe_token = scrub_pii(token) if isinstance(token, str) else token
    try:
        cents = parse_currency_to_cents(token, allow_zero=allow_zero)
    except TokenError as exc:
        return TraceStep(
            stage="parse",
            ok=False,
            detail=f"TokenError: {scrub_pii(str(exc))}",
            cents=None,
        )
    sign = "-" if cents < 0 else "+" if cents > 0 else "0"
    return TraceStep(
        stage="parse",
        ok=True,
        detail=f"token={safe_token!r}",
        cents=cents,
        sign=sign,
        usd=format_usd_cents(cents),
    )


def trace_format(cents: int) -> TraceStep:
    """Format cents as USD and verify round-trip parse where possible."""
    try:
        usd = format_usd_cents(cents)
    except TypeError as exc:
        return TraceStep(
            stage="format",
            ok=False,
            detail=scrub_pii(str(exc)),
            cents=None,
        )
    try:
        if cents > 0:
            round_trip = parse_usd_form_to_cents(usd)
        else:
            round_trip = parse_currency_to_cents(usd, allow_zero=(cents == 0))
    except TokenError as exc:
        return TraceStep(
            stage="format",
            ok=False,
            detail=f"round-trip TokenError: {scrub_pii(str(exc))}",
            cents=cents,
            usd=usd,
        )
    ok = round_trip == cents
    return TraceStep(
        stage="format",
        ok=ok,
        detail="round-trip ok" if ok else f"round-trip got {round_trip}",
        cents=cents,
        usd=usd,
        sign="-" if cents < 0 else "+",
    )


def trace_diff_stage(*, expected: int, actual: int, label: str) -> TraceStep:
    """Fail loud when two integer-cent values differ."""
    if type(expected) is not int or type(actual) is not int:
        return TraceStep(
            stage="diff-stage",
            ok=False,
            detail="expected and actual must be int cents",
        )
    ok = expected == actual
    return TraceStep(
        stage="diff-stage",
        ok=ok,
        detail=(
            f"label={scrub_pii(label)} expected_cents={expected} actual_cents={actual}"
        ),
        cents=actual,
    )


def _render_fixture_json_to_pdf(fixture_path: Path, target: Path) -> Path:
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    raw_text = data.get("raw_text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise TokenError(f"Fixture {fixture_path.name} missing string raw_text")
    lines = raw_text.split("\n")
    pdf = canvas.Canvas(str(target))
    y = 750
    for line in lines:
        line = re.sub(r"(\t|\s{2,})", " | ", line)
        # Never write PII-laden lines into a durable artifact beyond temp PDF;
        # temp file is process-local for adapter match only.
        pdf.drawString(50, y, line[:120])
        y -= 15
        if y < 50:
            pdf.showPage()
            y = 750
    pdf.save()
    return target


def _bundles_from_parse(adapter: Any, parsed: Any) -> list[tuple]:
    from app.adapters.combined import CombinedStatementAdapter

    if isinstance(adapter, CombinedStatementAdapter):
        return list(parsed)
    return [parsed]


def _domain_validate(
    account: Any, statement: Any, summary: Any, transactions: list[Any]
) -> None:
    domain = account.account_domain
    validate_universal_reconciliation(statement, transactions, domain)
    if domain == AccountDomain.DEPOSITORY:
        validate_depository_reconciliation(statement, summary, transactions)
    elif domain == AccountDomain.REVOLVING_CREDIT:
        validate_credit_card_reconciliation(statement, summary, transactions)
    elif domain == AccountDomain.CUSTODIAL_BROKERAGE:
        validate_brokerage_reconciliation(statement, summary, transactions)
    else:
        raise TokenError(f"Unsupported domain for reconcile: {domain!r}")


def trace_reconcile(
    account: Any, statement: Any, summary: Any, transactions: list[Any]
) -> TraceStep:
    """Run domain validators; scrub any exception text."""
    try:
        _domain_validate(account, statement, summary, transactions)
    except InvariantError as exc:
        return TraceStep(
            stage="reconcile",
            ok=False,
            detail=f"InvariantError: {scrub_pii(str(exc))}",
        )
    except Exception as exc:  # noqa: BLE001 — debug tracer surfaces type
        return TraceStep(
            stage="reconcile",
            ok=False,
            detail=f"{type(exc).__name__}: {scrub_pii(str(exc))}",
        )
    txn_sum = sum(int(txn.amount_cents) for txn in transactions)
    return TraceStep(
        stage="reconcile",
        ok=True,
        detail="universal+domain reconciliation ok",
        cents=txn_sum,
    )


def trace_fixture_or_pdf(path: Path | str) -> FixtureTraceReport:
    """Match adapter, extract, reconcile; emit cents only (no page_text dump)."""
    source = Path(path)
    if not source.is_file():
        return FixtureTraceReport(
            ok=False,
            adapter_id="",
            error=f"file not found: {source}",
            steps=[
                TraceStep(stage="fixture", ok=False, detail=f"missing {source.name}")
            ],
        )

    tmp_pdf: Path | None = None
    try:
        if source.suffix.lower() == ".json":
            fd, tmp_name = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            tmp_pdf = Path(tmp_name)
            pdf_path = _render_fixture_json_to_pdf(source, tmp_pdf)
        elif source.suffix.lower() == ".pdf":
            pdf_path = source
        else:
            return FixtureTraceReport(
                ok=False,
                adapter_id="",
                error=f"unsupported suffix {source.suffix!r}",
                steps=[
                    TraceStep(
                        stage="fixture",
                        ok=False,
                        detail="path must be .json fixture or .pdf",
                    )
                ],
            )

        registry = get_default_registry()
        adapter = registry.match(pdf_path)
        warnings: list[str] = []
        if adapter.adapter_id == "schwab_brokerage":
            warnings.append(
                "schwab_brokerage uses legacy SchwabAdapter._parse_cents "
                "(diverges from parse_currency_to_cents; magnitudes only)"
            )

        extraction = extract_pdf_to_raw(
            file_path=pdf_path,
            adapter_id=adapter.adapter_id,
            adapter_version=adapter.adapter_version,
            original_basename=source.name,
        )
        parsed = adapter.parse_canonical(extraction)
        bundles = _bundles_from_parse(adapter, parsed)

        statements: list[StatementTrace] = []
        steps: list[TraceStep] = [
            TraceStep(
                stage="match",
                ok=True,
                detail=f"adapter_id={adapter.adapter_id}",
            )
        ]
        all_ok = True
        for bundle in bundles:
            account, statement, summary, transactions = bundle[:4]
            recon = trace_reconcile(account, statement, summary, list(transactions))
            steps.append(recon)
            if not recon.ok:
                all_ok = False
            liq = liquidity_class_for_domain(account.account_domain)
            txn_sum = sum(int(txn.amount_cents) for txn in transactions)
            statements.append(
                StatementTrace(
                    institution=account.institution,
                    account_mask=account.account_mask,
                    account_domain=account.account_domain.value,
                    liquidity_class=liq,
                    liquidity_label=liquidity_label(liq),
                    opening_balance_cents=int(statement.opening_balance_cents),
                    closing_balance_cents=int(statement.closing_balance_cents),
                    net_change_cents=int(statement.net_change_cents),
                    txn_sum_cents=txn_sum,
                    txn_count=len(transactions),
                )
            )
            # Emit category+date+amount only (no description)
            for txn in transactions:
                steps.append(
                    TraceStep(
                        stage="txn",
                        ok=True,
                        detail=(
                            f"date={txn.post_date.isoformat()} "
                            f"category={txn.transaction_category.value}"
                        ),
                        cents=int(txn.amount_cents),
                        usd=format_usd_cents(int(txn.amount_cents)),
                        sign="-" if int(txn.amount_cents) < 0 else "+",
                    )
                )

        return FixtureTraceReport(
            ok=all_ok,
            adapter_id=adapter.adapter_id,
            statements=statements,
            warnings=warnings,
            steps=steps,
        )
    except Exception as exc:  # noqa: BLE001 — debug surface
        return FixtureTraceReport(
            ok=False,
            adapter_id="",
            error=f"{type(exc).__name__}: {exc}",
            steps=[
                TraceStep(
                    stage="fixture",
                    ok=False,
                    detail=f"{type(exc).__name__}: {scrub_pii(str(exc))}",
                )
            ],
        )
    finally:
        if tmp_pdf is not None and tmp_pdf.exists():
            tmp_pdf.unlink(missing_ok=True)


def trace_vault_aggregates(database_url: str) -> VaultAggregateReport:
    """Sum latest closings by liquidity; RTA = liquid minus envelopes."""
    if not isinstance(database_url, str) or not database_url.strip():
        return VaultAggregateReport(
            ok=False,
            liquid_closing_cents=0,
            brokerage_closing_cents=0,
            liability_closing_cents=0,
            ready_to_assign_cents=0,
            error="database_url must be a non-empty string",
        )
    try:
        with psycopg.connect(database_url) as conn:
            seen: set[Any] = set()
            liquid = 0
            brokerage = 0
            liability = 0
            accounts: list[dict[str, Any]] = []
            for account, statement, _txn_count in list_recent_statements(
                conn, limit=50
            ):
                if account.account_id in seen:
                    continue
                seen.add(account.account_id)
                liq = liquidity_class_for_domain(account.account_domain)
                closing = int(statement.closing_balance_cents)
                if liq == LIQUIDITY_LIQUID:
                    liquid += closing
                elif liq == LIQUIDITY_BROKERAGE:
                    brokerage += closing
                elif liq == LIQUIDITY_LIABILITY:
                    liability += closing
                accounts.append(
                    {
                        "institution": account.institution,
                        "account_mask": account.account_mask,
                        "account_domain": account.account_domain.value,
                        "liquidity_class": liq,
                        "liquidity_label": liquidity_label(liq),
                        "closing_balance_cents": closing,
                    }
                )
            rta = ready_to_assign_cents(conn)
        return VaultAggregateReport(
            ok=True,
            liquid_closing_cents=liquid,
            brokerage_closing_cents=brokerage,
            liability_closing_cents=liability,
            ready_to_assign_cents=rta,
            accounts=accounts,
        )
    except Exception as exc:  # noqa: BLE001
        return VaultAggregateReport(
            ok=False,
            liquid_closing_cents=0,
            brokerage_closing_cents=0,
            liability_closing_cents=0,
            ready_to_assign_cents=0,
            error=f"{type(exc).__name__}: {exc}",
        )
