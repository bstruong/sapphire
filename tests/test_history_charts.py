"""History charts on Overview: closing balances + category income/spend.

Pitch: docs/specs/pitch_history_charts.md
Wireframe: docs/specs/visuals/history_charts_wireframe.html
"""

from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

HX = {"HX-Request": "true"}


def _truncate(postgres_url: str) -> None:
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


@pytest.fixture()
def client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    from app.api.app import create_app
    from app.db.connection import init_db

    init_db()
    _truncate(postgres_url)
    return TestClient(create_app())


def _insert_account(
    conn: psycopg.Connection,
    *,
    institution: str = "Chase",
    mask: str = "*0000",
    domain: str = "depository",
    account_type: str = "checking",
) -> uuid.UUID:
    account_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO accounts (
            account_id, institution, account_mask, account_domain, account_type, currency
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (account_id, institution, mask, domain, account_type, "USD"),
    )
    return account_id


def _insert_statement(
    conn: psycopg.Connection,
    account_id: uuid.UUID,
    *,
    start: datetime.date,
    end: datetime.date,
    opening_cents: int,
    closing_cents: int,
) -> uuid.UUID:
    payload_id = uuid.uuid4()
    run_id = uuid.uuid4()
    statement_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO raw_payloads (
            raw_payload_id, content_sha256, byte_length, original_basename, ingested_at
        ) VALUES (%s, %s, %s, %s, NOW())
        """,
        (payload_id, uuid.uuid4().hex + uuid.uuid4().hex, 256, "synthetic.pdf"),
    )
    conn.execute(
        """
        INSERT INTO extraction_runs (
            run_id, raw_payload_id, adapter_id, adapter_version, started_at, status
        ) VALUES (%s, %s, %s, %s, NOW(), %s)
        """,
        (run_id, payload_id, "synthetic", "0.0.0", "canonical_persisted"),
    )
    conn.execute(
        """
        INSERT INTO statements (
            statement_id, account_id, run_id, raw_payload_id,
            statement_start_date, statement_end_date,
            opening_balance_cents, closing_balance_cents, net_change_cents
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            statement_id,
            account_id,
            run_id,
            payload_id,
            start,
            end,
            opening_cents,
            closing_cents,
            closing_cents - opening_cents,
        ),
    )
    return statement_id


def _insert_txn(
    conn: psycopg.Connection,
    statement_id: uuid.UUID,
    *,
    post_date: datetime.date,
    amount_cents: int,
    category: str,
    description: str = "SYNTHETIC Payroll",
) -> None:
    conn.execute(
        """
        INSERT INTO transactions (
            transaction_id, statement_id, post_date, amount_cents,
            description, transaction_category
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (uuid.uuid4(), statement_id, post_date, amount_cents, description, category),
    )


def _seed_multi_month(conn: psycopg.Connection) -> None:
    """Two liquid periods + categorized income/spend across months."""
    checking = _insert_account(conn, mask="*0000")
    savings = _insert_account(
        conn, institution="Schwab", mask="*0001", account_type="savings"
    )
    # June
    s1 = _insert_statement(
        conn,
        checking,
        start=datetime.date(2026, 6, 1),
        end=datetime.date(2026, 6, 30),
        opening_cents=500_000,
        closing_cents=600_000,
    )
    _insert_statement(
        conn,
        savings,
        start=datetime.date(2026, 6, 1),
        end=datetime.date(2026, 6, 30),
        opening_cents=200_000,
        closing_cents=210_000,
    )
    _insert_txn(
        conn,
        s1,
        post_date=datetime.date(2026, 6, 5),
        amount_cents=320_000,
        category="deposit",
        description="SYNTHETIC Payroll",
    )
    _insert_txn(
        conn,
        s1,
        post_date=datetime.date(2026, 6, 10),
        amount_cents=-150_000,
        category="withdrawal",
        description="SYNTHETIC Rent",
    )
    # July
    s2 = _insert_statement(
        conn,
        checking,
        start=datetime.date(2026, 7, 1),
        end=datetime.date(2026, 7, 31),
        opening_cents=600_000,
        closing_cents=720_000,
    )
    _insert_statement(
        conn,
        savings,
        start=datetime.date(2026, 7, 1),
        end=datetime.date(2026, 7, 31),
        opening_cents=210_000,
        closing_cents=225_000,
    )
    _insert_txn(
        conn,
        s2,
        post_date=datetime.date(2026, 7, 5),
        amount_cents=320_000,
        category="deposit",
        description="SYNTHETIC Payroll",
    )
    _insert_txn(
        conn,
        s2,
        post_date=datetime.date(2026, 7, 12),
        amount_cents=-80_000,
        category="purchase",
        description="SYNTHETIC Grocer",
    )
    # August
    s3 = _insert_statement(
        conn,
        checking,
        start=datetime.date(2026, 8, 1),
        end=datetime.date(2026, 8, 31),
        opening_cents=720_000,
        closing_cents=800_000,
    )
    _insert_statement(
        conn,
        savings,
        start=datetime.date(2026, 8, 1),
        end=datetime.date(2026, 8, 31),
        opening_cents=225_000,
        closing_cents=240_000,
    )
    _insert_txn(
        conn,
        s3,
        post_date=datetime.date(2026, 8, 5),
        amount_cents=340_000,
        category="deposit",
        description="SYNTHETIC Payroll",
    )
    _insert_txn(
        conn,
        s3,
        post_date=datetime.date(2026, 8, 18),
        amount_cents=-95_000,
        category="withdrawal",
        description="SYNTHETIC Utility",
    )
    conn.commit()


class TestHistoryBalanceQuery:
    def test_liquid_closing_by_period_sums_depository_only(
        self, postgres_url: str
    ) -> None:
        from app.db.history import list_liquid_closing_by_period

        _truncate(postgres_url)
        with psycopg.connect(postgres_url) as conn:
            checking = _insert_account(conn, mask="*0000")
            brokerage = _insert_account(
                conn,
                institution="Schwab",
                mask="*5678",
                domain="custodial_brokerage",
                account_type="brokerage_cash",
            )
            _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 6, 1),
                end=datetime.date(2026, 6, 30),
                opening_cents=100_000,
                closing_cents=150_000,
            )
            _insert_statement(
                conn,
                brokerage,
                start=datetime.date(2026, 6, 1),
                end=datetime.date(2026, 6, 30),
                opening_cents=9_000_000,
                closing_cents=9_100_000,
            )
            _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 7, 1),
                end=datetime.date(2026, 7, 31),
                opening_cents=150_000,
                closing_cents=200_000,
            )
            conn.commit()
            series = list_liquid_closing_by_period(conn)

        assert [(p.statement_end_date, p.liquid_total_cents) for p in series] == [
            (datetime.date(2026, 6, 30), 150_000),
            (datetime.date(2026, 7, 31), 200_000),
        ]
        assert all(type(p.liquid_total_cents) is int for p in series)

    def test_per_account_closing_points_are_integer_cents(
        self, postgres_url: str
    ) -> None:
        from app.db.history import list_closing_balance_points

        _truncate(postgres_url)
        with psycopg.connect(postgres_url) as conn:
            checking = _insert_account(conn)
            _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 6, 1),
                end=datetime.date(2026, 6, 30),
                opening_cents=100_000,
                closing_cents=150_000,
            )
            conn.commit()
            points = list_closing_balance_points(conn)

        assert len(points) == 1
        assert points[0].closing_balance_cents == 150_000
        assert type(points[0].closing_balance_cents) is int
        assert points[0].account_mask == "*0000"


class TestHistoryCategoryQuery:
    def test_monthly_income_vs_spend_uses_abs_sums(self, postgres_url: str) -> None:
        from app.db.history import list_category_month_totals

        _truncate(postgres_url)
        with psycopg.connect(postgres_url) as conn:
            checking = _insert_account(conn)
            stmt = _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 7, 1),
                end=datetime.date(2026, 7, 31),
                opening_cents=100_000,
                closing_cents=200_000,
            )
            _insert_txn(
                conn,
                stmt,
                post_date=datetime.date(2026, 7, 5),
                amount_cents=320_000,
                category="deposit",
            )
            _insert_txn(
                conn,
                stmt,
                post_date=datetime.date(2026, 7, 8),
                amount_cents=10_000,
                category="interest_paid",
            )
            _insert_txn(
                conn,
                stmt,
                post_date=datetime.date(2026, 7, 12),
                amount_cents=-80_000,
                category="withdrawal",
            )
            _insert_txn(
                conn,
                stmt,
                post_date=datetime.date(2026, 7, 15),
                amount_cents=-20_000,
                category="purchase",
            )
            conn.commit()
            months = list_category_month_totals(conn)

        assert len(months) == 1
        assert months[0].year_month == "2026-07"
        assert months[0].income_cents == 330_000
        assert months[0].spend_cents == 100_000
        assert type(months[0].income_cents) is int
        assert type(months[0].spend_cents) is int

    def test_unknown_category_bucket_fails_loud(self) -> None:
        from app.db.history import bucket_for_category
        from app.models.exceptions import TokenError

        with pytest.raises(TokenError, match="category"):
            bucket_for_category("not_a_real_category")


class TestHistorySvg:
    def test_balance_svg_uses_integer_coords_and_css_classes(self) -> None:
        from app.db.history import LiquidBalanceSeries
        from app.presentation.history_charts import render_balance_history_svg

        series = [
            LiquidBalanceSeries(
                statement_end_date=datetime.date(2026, 6, 30),
                liquid_total_cents=600_000,
            ),
            LiquidBalanceSeries(
                statement_end_date=datetime.date(2026, 7, 31),
                liquid_total_cents=720_000,
            ),
            LiquidBalanceSeries(
                statement_end_date=datetime.date(2026, 8, 31),
                liquid_total_cents=800_000,
            ),
        ]
        svg = render_balance_history_svg(series, account_series=[])
        assert "<svg" in svg
        assert "history-line-liquid" in svg
        assert "style=" not in svg
        assert "float" not in svg.lower()
        # polyline points are integers
        assert 'points="' in svg
        points_attr = svg.split('points="', 1)[1].split('"', 1)[0]
        for pair in points_attr.split():
            x_s, y_s = pair.split(",")
            assert x_s.isdigit() or (x_s.startswith("-") and x_s[1:].isdigit())
            assert y_s.isdigit() or (y_s.startswith("-") and y_s[1:].isdigit())

    def test_category_svg_renders_income_and_spend_bars(self) -> None:
        from app.db.history import CategoryMonthBucket
        from app.presentation.history_charts import render_category_history_svg

        months = [
            CategoryMonthBucket(
                year_month="2026-07", income_cents=320_000, spend_cents=100_000
            ),
            CategoryMonthBucket(
                year_month="2026-08", income_cents=340_000, spend_cents=95_000
            ),
        ]
        svg = render_category_history_svg(months)
        assert "history-bar-income" in svg
        assert "history-bar-spend" in svg
        assert "style=" not in svg

    def test_svg_rejects_non_int_cents(self) -> None:
        from app.db.history import LiquidBalanceSeries
        from app.presentation.history_charts import render_balance_history_svg

        good = LiquidBalanceSeries(
            statement_end_date=datetime.date(2026, 6, 30),
            liquid_total_cents=100_000,
        )
        bad = LiquidBalanceSeries(
            statement_end_date=datetime.date(2026, 7, 31),
            liquid_total_cents=100.5,  # type: ignore[arg-type]
        )
        with pytest.raises(TypeError, match="int"):
            render_balance_history_svg([good, bad], account_series=[])


class TestOverviewHistorySection:
    def test_multi_month_seed_renders_both_charts(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            _seed_multi_month(conn)
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert 'id="overview-history"' in html
        assert "History" in html
        assert "Closing balances" in html
        assert "Category income" in html or "income / spend" in html.lower()
        assert "history-line-liquid" in html
        assert "history-bar-income" in html
        assert "history-bar-spend" in html
        assert "style=" not in html
        assert "cdn." not in html.lower()

    def test_single_period_shows_loud_balance_empty(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            checking = _insert_account(conn)
            stmt = _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 9, 1),
                end=datetime.date(2026, 9, 30),
                opening_cents=100_000,
                closing_cents=150_000,
            )
            _insert_txn(
                conn,
                stmt,
                post_date=datetime.date(2026, 9, 5),
                amount_cents=50_000,
                category="deposit",
            )
            conn.commit()
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert 'id="overview-history"' in html
        assert 'role="alert"' in html
        assert "HISTORY_BALANCE_INSUFFICIENT" in html
        assert "history-line-liquid" not in html

    def test_no_categorized_txns_shows_loud_category_empty(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            checking = _insert_account(conn)
            _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 6, 1),
                end=datetime.date(2026, 6, 30),
                opening_cents=100_000,
                closing_cents=110_000,
            )
            _insert_statement(
                conn,
                checking,
                start=datetime.date(2026, 7, 1),
                end=datetime.date(2026, 7, 31),
                opening_cents=110_000,
                closing_cents=120_000,
            )
            conn.commit()
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert "HISTORY_CATEGORY_EMPTY" in html
        assert "history-bar-income" not in html
        # Balance chart still renders (≥2 periods)
        assert "history-line-liquid" in html

    def test_empty_vault_still_uses_overview_empty(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert "No persisted statements yet" in html
        assert 'id="overview-history"' not in html


class TestSeedHistoryChartsScript:
    def test_seed_script_populates_multi_month_history(
        self, client: TestClient, postgres_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", postgres_url)
        try:
            from scripts.seed_history_charts import seed
        except ModuleNotFoundError:
            pytest.skip("scripts/ missing (not a full repo checkout)")

        seed()
        seed()  # idempotent
        html = client.get("/api/v1/dashboard/overview", headers=HX).text
        assert 'id="overview-history"' in html
        assert "history-line-liquid" in html
        assert "history-bar-income" in html
        assert Path("docs/specs/visuals/history_charts_wireframe.html").is_file()
