"""Category spend pie aggregation + SVG (integer cents)."""

from __future__ import annotations

import datetime

import psycopg
import pytest

from app.db.history import (
    SpendCategorySlice,
    list_available_spend_months,
    list_spend_category_month_totals,
)
from app.db.repository import persist_canonical_statement, persist_raw_extraction
from app.models.canonical import (
    Account,
    CanonicalStatement,
    CanonicalTransaction,
    DepositorySummary,
)
from app.models.enums import (
    AccountDomain,
    AccountType,
    RunStatus,
    TransactionCategory,
)
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
from app.presentation.history_charts import render_spend_category_pie_svg
from app.spend.categories import set_manual_label


def _seed_month(
    db_conn: psycopg.Connection,
    *,
    sha: str,
    post_date: datetime.date,
    rows: list[tuple[int, str | None]],
) -> list[CanonicalTransaction]:
    """rows: (amount_cents, spend_slug_or_None)."""
    payload = RawPayload(
        content_sha256=sha,
        byte_length=700,
        original_basename=f"pie_{sha[:8]}.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="Pie seed")
    persist_raw_extraction(
        db_conn, RawExtraction(payload=payload, run=run, pages=[page])
    )
    account = Account(
        institution="SYNTHETIC Bank",
        account_mask="*0000",
        account_domain=AccountDomain.DEPOSITORY,
        account_type=AccountType.CHECKING,
    )
    statement = CanonicalStatement(
        account_id=account.account_id,
        run_id=run.run_id,
        raw_payload_id=payload.raw_payload_id,
        statement_start_date=post_date.replace(day=1),
        statement_end_date=post_date,
        opening_balance_cents=500000,
        closing_balance_cents=500000 + sum(a for a, _ in rows),
        net_change_cents=sum(a for a, _ in rows),
    )
    withdrawals = sum(-a for a, _ in rows if a < 0)
    deposits = sum(a for a, _ in rows if a > 0)
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=deposits,
        withdrawals_cents=withdrawals,
        interest_paid_cents=0,
        fees_cents=0,
    )
    txns: list[CanonicalTransaction] = []
    for amount, _slug in rows:
        cat = (
            TransactionCategory.DEPOSIT
            if amount > 0
            else TransactionCategory.WITHDRAWAL
        )
        txns.append(
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=post_date,
                amount_cents=amount,
                description="SYNTHETIC pie row",
                transaction_category=cat,
            )
        )
    persist_canonical_statement(db_conn, account, statement, summary, txns)
    for txn, (_amount, slug) in zip(txns, rows, strict=True):
        if slug is not None:
            set_manual_label(db_conn, transaction_id=txn.transaction_id, slug=slug)
    db_conn.commit()
    return txns


class TestSpendCategoryMonthTotals:
    def test_groups_labeled_and_uncategorized(
        self, db_conn: psycopg.Connection
    ) -> None:
        _seed_month(
            db_conn,
            sha="b1" * 32,
            post_date=datetime.date(2026, 10, 15),
            rows=[
                (-185000, "rent"),
                (-45231, "groceries"),
                (-23400, None),
                (10000, None),  # income ignored
            ],
        )
        slices = list_spend_category_month_totals(db_conn, year=2026, month=10)
        by_slug = {s.slug: s.spend_cents for s in slices}
        assert by_slug["rent"] == 185000
        assert by_slug["groceries"] == 45231
        assert by_slug["uncategorized"] == 23400
        assert sum(by_slug.values()) == 185000 + 45231 + 23400

    def test_empty_month_returns_empty(self, db_conn: psycopg.Connection) -> None:
        assert list_spend_category_month_totals(db_conn, year=2020, month=1) == []

    def test_invalid_month_fails_loud(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(ValueError, match="month"):
            list_spend_category_month_totals(db_conn, year=2026, month=13)

    def test_available_months_lists_spend_only(
        self, db_conn: psycopg.Connection
    ) -> None:
        _seed_month(
            db_conn,
            sha="b2" * 32,
            post_date=datetime.date(2026, 9, 10),
            rows=[(-5000, "dining")],
        )
        _seed_month(
            db_conn,
            sha="b3" * 32,
            post_date=datetime.date(2026, 8, 10),
            rows=[(5000, None)],  # deposit only — not a spend month
        )
        months = list_available_spend_months(db_conn)
        assert "2026-09" in months
        assert "2026-08" not in months


class TestSpendCategoryPieSvg:
    def test_renders_donut_with_integer_arcs(self) -> None:
        slices = (
            SpendCategorySlice(
                slug="rent", display_name="Rent", spend_cents=200, color="#6f8cff"
            ),
            SpendCategorySlice(
                slug="uncategorized",
                display_name="Uncategorized",
                spend_cents=100,
                color="#68758d",
            ),
        )
        svg = render_spend_category_pie_svg(slices, year=2026, month=10)
        assert 'class="pie-svg"' in svg
        assert "stroke-dasharray" in svg
        assert "Rent" not in svg or "aria" in svg  # legend is HTML, not SVG text names
        assert "$3.00" in svg or "300" in svg  # center total

    def test_rejects_empty_slices(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            render_spend_category_pie_svg((), year=2026, month=10)

    def test_rejects_non_int_cents(self) -> None:
        with pytest.raises(TypeError):
            render_spend_category_pie_svg(
                (
                    SpendCategorySlice(
                        slug="rent",
                        display_name="Rent",
                        spend_cents=1.5,  # type: ignore[arg-type]
                        color="#6f8cff",
                    ),
                ),
                year=2026,
                month=10,
            )
