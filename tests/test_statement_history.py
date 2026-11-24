"""Full statement history: Recent → View all, account/year filters, Open → ledger.

Wireframe: docs/specs/visuals/statement_history_wireframe.html
"""

from __future__ import annotations

import datetime
import re
import uuid

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
    institution: str = "Charles Schwab Bank",
    mask: str = "*4821",
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
    opening_cents: int = 100_00,
    closing_cents: int = 120_00,
    txn_count: int = 3,
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
    conn.execute(
        """
        INSERT INTO statement_depository_summaries (
            statement_id, deposits_cents, withdrawals_cents,
            interest_paid_cents, fees_cents
        ) VALUES (%s, %s, %s, %s, %s)
        """,
        (statement_id, max(closing_cents - opening_cents, 0), 0, 0, 0),
    )
    for i in range(txn_count):
        conn.execute(
            """
            INSERT INTO transactions (
                transaction_id, statement_id, post_date, amount_cents,
                description, transaction_category
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                uuid.uuid4(),
                statement_id,
                start + datetime.timedelta(days=i + 1),
                10_00,
                "SYNTHETIC Payroll",
                "deposit",
            ),
        )
    return statement_id


def _seed_history(conn: psycopg.Connection) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Two accounts across 2025/2026; returns (schwab_id, chase_id, newest_stmt)."""
    schwab = _insert_account(conn, institution="Charles Schwab Bank", mask="*4821")
    chase = _insert_account(
        conn,
        institution="SYNTHETIC Chase",
        mask="*0000",
        domain="revolving_credit",
        account_type="credit_card",
    )
    _insert_statement(
        conn,
        schwab,
        start=datetime.date(2025, 12, 1),
        end=datetime.date(2025, 12, 31),
        txn_count=2,
    )
    _insert_statement(
        conn,
        schwab,
        start=datetime.date(2026, 1, 1),
        end=datetime.date(2026, 1, 31),
        txn_count=4,
    )
    newest = _insert_statement(
        conn,
        schwab,
        start=datetime.date(2026, 3, 1),
        end=datetime.date(2026, 3, 31),
        txn_count=5,
    )
    _insert_statement(
        conn,
        chase,
        start=datetime.date(2026, 2, 1),
        end=datetime.date(2026, 2, 28),
        txn_count=6,
    )
    return schwab, chase, newest


class TestRecentEntryPoint:
    def test_recent_has_view_all_history_cta(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            _seed_history(conn)
            conn.commit()
        html = client.get("/api/v1/dashboard/statements", headers=HX).text
        assert 'id="view-all-history"' in html
        assert "View all history" in html
        assert 'hx-get="/api/v1/dashboard/statements/history"' in html


class TestStatementHistoryView:
    def test_requires_htmx(self, client: TestClient) -> None:
        assert client.get("/api/v1/dashboard/statements/history").status_code == 400

    def test_empty_vault_shows_loud_empty(self, client: TestClient) -> None:
        html = client.get("/api/v1/dashboard/statements/history", headers=HX).text
        assert 'data-nav="statements"' in html
        assert "Statement history" in html
        assert "No statements match this filter" in html or "No statements yet" in html
        assert 'id="account-filter"' in html
        assert 'id="year-filter"' in html

    def test_lists_all_grouped_by_year(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            _seed_history(conn)
            conn.commit()
        html = client.get("/api/v1/dashboard/statements/history", headers=HX).text
        assert 'id="statement-history"' in html
        assert "4 statements" in html
        assert 'class="year-label"' in html
        assert "2026" in html
        assert "2025" in html
        assert "Charles Schwab Bank" in html
        assert "SYNTHETIC Chase" in html
        assert html.index("2026") < html.index("2025")
        assert re.search(
            r'hx-get="/api/v1/dashboard/statements/[0-9a-f-]{36}\?from=history"',
            html,
        )

    def test_filter_by_account(self, client: TestClient, postgres_url: str) -> None:
        with psycopg.connect(postgres_url) as conn:
            schwab, _chase, _newest = _seed_history(conn)
            conn.commit()
        html = client.get(
            f"/api/v1/dashboard/statements/history?account={schwab}",
            headers=HX,
        ).text
        assert "3 statements" in html
        assert "Charles Schwab Bank" in html
        # Chase remains in the account filter dropdown, but not in the result list.
        list_html = html.split('id="history-list"', 1)[1]
        assert "SYNTHETIC Chase" not in list_html
        assert f'value="{schwab}"' in html
        assert "selected" in html

    def test_filter_by_year(self, client: TestClient, postgres_url: str) -> None:
        with psycopg.connect(postgres_url) as conn:
            _seed_history(conn)
            conn.commit()
        html = client.get(
            "/api/v1/dashboard/statements/history?year=2025",
            headers=HX,
        ).text
        assert "1 statements" in html or "1 statement" in html
        assert "2025-12-01" in html
        assert "2026-03-01" not in html

    def test_filter_no_match_loud_empty(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            _seed_history(conn)
            conn.commit()
        html = client.get(
            "/api/v1/dashboard/statements/history?year=2019",
            headers=HX,
        ).text
        assert "No statements match this filter" in html
        assert "0 statements" in html

    def test_invalid_account_rejected(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/dashboard/statements/history?account=not-a-uuid",
            headers=HX,
        )
        assert resp.status_code == 400
        assert "INVALID_ACCOUNT_FILTER" in resp.text

    def test_invalid_year_rejected(self, client: TestClient) -> None:
        resp = client.get(
            "/api/v1/dashboard/statements/history?year=abc",
            headers=HX,
        )
        assert resp.status_code == 400
        assert "INVALID_YEAR_FILTER" in resp.text

    def test_open_from_history_back_link(
        self, client: TestClient, postgres_url: str
    ) -> None:
        with psycopg.connect(postgres_url) as conn:
            _schwab, _chase, newest = _seed_history(conn)
            conn.commit()
        html = client.get(
            f"/api/v1/dashboard/statements/{newest}?from=history",
            headers=HX,
        ).text
        assert "Vault Ledger" in html
        assert 'hx-get="/api/v1/dashboard/statements/history"' in html
        assert "Back to statement history" in html


class TestListStatementHistoryRepo:
    def test_filters_and_orders(
        self, postgres_url: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DATABASE_URL", postgres_url)
        from app.db.connection import init_db
        from app.db.repository import list_statement_history
        from app.models.exceptions import PersistenceError

        init_db()
        _truncate(postgres_url)
        with psycopg.connect(postgres_url) as conn:
            schwab, chase, _ = _seed_history(conn)
            conn.commit()

        with psycopg.connect(postgres_url) as conn:
            all_rows = list_statement_history(conn)
            assert len(all_rows) == 4
            ends = [stmt.statement_end_date for _acc, stmt, _n in all_rows]
            assert ends == sorted(ends, reverse=True)

            schwab_rows = list_statement_history(conn, account_id=schwab)
            assert len(schwab_rows) == 3
            assert all(acc.account_id == schwab for acc, _s, _n in schwab_rows)

            year_rows = list_statement_history(conn, year=2025)
            assert len(year_rows) == 1
            assert year_rows[0][1].statement_end_date.year == 2025

            both = list_statement_history(conn, account_id=chase, year=2026)
            assert len(both) == 1

            with pytest.raises(PersistenceError, match="year"):
                list_statement_history(conn, year=999)
