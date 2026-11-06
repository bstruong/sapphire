"""YNAB budgets / envelopes: Ready to Assign, assign, fail-loud over-assign."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.budget.envelopes import (
    assign_to_envelope,
    create_envelope,
    list_envelopes,
    ready_to_assign_cents,
)
from app.models.exceptions import AssignExceedsAvailableError, TokenError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    from app.api.app import create_app
    from app.db.connection import init_db

    init_db()
    with psycopg.connect(postgres_url) as conn:
        conn.execute(
            """
            TRUNCATE TABLE envelopes,
                           raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()
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


def _upload(client: TestClient, tmp_path: Path, fixture: str) -> None:
    pdf = _render_pdf_from_fixture(fixture, tmp_path / f"{fixture}.pdf")
    with pdf.open("rb") as handle:
        response = client.post(
            "/api/v1/dashboard/upload",
            files={"file": (pdf.name, handle, "application/pdf")},
            headers={"HX-Request": "true"},
        )
    assert response.status_code == 200, response.text


class TestEnvelopeModel:
    def test_create_and_list_integer_cents(self, postgres_url: str) -> None:
        from app.db.connection import init_db

        init_db(url=postgres_url)
        with psycopg.connect(postgres_url) as conn:
            conn.execute("TRUNCATE TABLE envelopes CASCADE")
            conn.commit()
            created = create_envelope(conn, name="SYNTHETIC Groceries")
            conn.commit()
            assert created.name == "SYNTHETIC Groceries"
            assert created.balance_cents == 0
            assert type(created.balance_cents) is int
            rows = list_envelopes(conn)
            assert len(rows) == 1
            assert rows[0].envelope_id == created.envelope_id

    def test_blank_name_fails_loud(self, postgres_url: str) -> None:
        from app.db.connection import init_db

        init_db(url=postgres_url)
        with psycopg.connect(postgres_url) as conn, pytest.raises(TokenError):
            create_envelope(conn, name="   ")


class TestReadyToAssign:
    def test_ready_equals_closings_minus_envelope_balances(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            available_before = ready_to_assign_cents(conn)
            assert type(available_before) is int
            assert available_before > 0
            env = create_envelope(conn, name="SYNTHETIC Buffer")
            assign_to_envelope(conn, envelope_id=env.envelope_id, amount_cents=1_00)
            conn.commit()
            available_after = ready_to_assign_cents(conn)
            assert available_after == available_before - 1_00

    def test_ready_to_assign_excludes_brokerage_and_credit(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        """Assignable cash is liquid depository only — never brokerage or cards.

        Checking-only statements must not inherit brokerage closings into
        Ready to Assign (that inflated dogfood to ~$54k from a ~$6k checking PDF).
        """
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            liquid_only = ready_to_assign_cents(conn)
        assert liquid_only == 129_500  # chase_checking_happy ending $1,295.00

        _upload(client, tmp_path, "brokerage_cash_happy")
        with psycopg.connect(postgres_url) as conn:
            after = ready_to_assign_cents(conn)
            rows = conn.execute(
                """
                SELECT a.account_domain, s.closing_balance_cents
                FROM accounts a
                JOIN statements s ON s.account_id = a.account_id
                ORDER BY a.account_domain
                """
            ).fetchall()
        assert ("depository", 129_500) in rows
        assert ("custodial_brokerage", 1_390_425) in rows
        assert after == liquid_only
        assert after != liquid_only + 1_390_425  # brokerage ending must stay out

    def test_empty_vault_ready_is_zero(self, postgres_url: str) -> None:
        from app.db.connection import init_db

        init_db(url=postgres_url)
        with psycopg.connect(postgres_url) as conn:
            conn.execute(
                """
                TRUNCATE TABLE envelopes, statements, accounts,
                               extraction_runs, raw_payloads CASCADE;
                """
            )
            conn.commit()
            assert ready_to_assign_cents(conn) == 0


class TestAssignFailLoud:
    def test_over_assign_raises_assign_exceeds_available(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Rent")
            conn.commit()
            available = ready_to_assign_cents(conn)
            with pytest.raises(AssignExceedsAvailableError, match="ASSIGN_EXCEEDS"):
                assign_to_envelope(
                    conn,
                    envelope_id=env.envelope_id,
                    amount_cents=available + 1,
                )

    def test_non_positive_amount_fails(self, postgres_url: str) -> None:
        from app.db.connection import init_db

        init_db(url=postgres_url)
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Groceries")
            conn.commit()
            with pytest.raises(TokenError):
                assign_to_envelope(conn, envelope_id=env.envelope_id, amount_cents=0)
            with pytest.raises(TokenError):
                assign_to_envelope(conn, envelope_id=env.envelope_id, amount_cents=-5)
            with pytest.raises(TokenError):
                assign_to_envelope(
                    conn,
                    envelope_id=env.envelope_id,
                    amount_cents=1.5,  # type: ignore[arg-type]
                )

    def test_unknown_envelope_fails(self, postgres_url: str) -> None:
        from app.db.connection import init_db

        init_db(url=postgres_url)
        with (
            psycopg.connect(postgres_url) as conn,
            pytest.raises(TokenError, match="ENVELOPE_NOT_FOUND"),
        ):
            assign_to_envelope(
                conn,
                envelope_id=uuid.uuid4(),
                amount_cents=100,
            )


class TestBudgetDashboard:
    def test_budget_tab_shows_ready_and_envelopes(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            create_envelope(conn, name="SYNTHETIC Groceries")
            conn.commit()
        page = client.get("/dashboard")
        assert page.status_code == 200
        assert "Budget" in page.text
        assert "Overview" in page.text

        budget = client.get(
            "/api/v1/dashboard/budget",
            headers={"HX-Request": "true"},
        )
        assert budget.status_code == 200
        html = budget.text
        assert "Ready to Assign" in html
        assert "SYNTHETIC Groceries" in html
        assert re.search(r"\$\d[\d,]*\.\d{2}\b", html) is not None
        assert re.findall(r"\$\d[\d,]*\.\d{3,}", html) == []

    def test_assign_form_happy_path_updates_balances(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Groceries")
            before = ready_to_assign_cents(conn)
            conn.commit()
            envelope_id = str(env.envelope_id)

        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={"envelope_id": envelope_id, "amount_usd": "50.00"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200, response.text
        assert "ASSIGN_EXCEEDS_AVAILABLE" not in response.text
        assert "SYNTHETIC Groceries" in response.text
        assert "$50.00" in response.text

        with psycopg.connect(postgres_url) as conn:
            rows = list_envelopes(conn)
            assert rows[0].balance_cents == 5_000
            assert ready_to_assign_cents(conn) == before - 5_000

    def test_assign_form_whole_dollar_updates_balances(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Entertainment")
            conn.commit()
            envelope_id = str(env.envelope_id)
            before = ready_to_assign_cents(conn)

        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={"envelope_id": envelope_id, "amount_usd": "50"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200
        assert "SYNTHETIC Entertainment" in response.text
        assert "$50.00" in response.text

        with psycopg.connect(postgres_url) as conn:
            rows = [
                e for e in list_envelopes(conn) if e.name == "SYNTHETIC Entertainment"
            ]
            assert rows[0].balance_cents == 5_000
            assert ready_to_assign_cents(conn) == before - 5_000

    def test_assign_over_available_returns_loud_error(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Rent")
            available = ready_to_assign_cents(conn)
            conn.commit()
            envelope_id = str(env.envelope_id)

        dollars = available // 100
        cents = available % 100
        too_much = f"{dollars + 1}.{cents:02d}"
        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={"envelope_id": envelope_id, "amount_usd": too_much},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        assert "ASSIGN_EXCEEDS_AVAILABLE" in response.text

    def test_assign_rejects_floatish_usd_token(
        self, client: TestClient, tmp_path: Path, postgres_url: str
    ) -> None:
        _upload(client, tmp_path, "chase_checking_happy")
        with psycopg.connect(postgres_url) as conn:
            env = create_envelope(conn, name="SYNTHETIC Buffer")
            conn.commit()
            envelope_id = str(env.envelope_id)

        response = client.post(
            "/api/v1/dashboard/budget/assign",
            data={"envelope_id": envelope_id, "amount_usd": "12.345"},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 400
        assert "INVALID_AMOUNT" in response.text or "TokenError" in response.text
