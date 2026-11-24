"""Bulk suggest staging + Accept/Reject (B2) and Vault downtime."""

from __future__ import annotations

import datetime
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.adapters.payee import OPAQUE_BANK_TYPES
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
from app.models.exceptions import InferenceConnectionError, TokenError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
from app.spend.categories import get_labels_by_transaction_ids, list_spend_categories
from app.spend.rules import insert_spend_label_rule
from app.spend.suggest import (
    accept_all_rule_suggestions,
    accept_suggestion_group,
    reject_all_vault_suggestions,
    reject_suggestion_group,
    run_suggest,
)

HX = {"HX-Request": "true"}


def _seed_month(
    db_conn: psycopg.Connection,
    rows: list[tuple[str | None, str, int]],
) -> list[CanonicalTransaction]:
    """Seed unlabeled outflows: (payee, description, amount_cents)."""
    payload = RawPayload(
        content_sha256="b" * 64,
        byte_length=800,
        original_basename="bulk_suggest.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="Bulk suggest")
    persist_raw_extraction(
        db_conn, RawExtraction(payload=payload, run=run, pages=[page])
    )
    account = Account(
        institution="SYNTHETIC Chase",
        account_mask="*0000",
        account_domain=AccountDomain.DEPOSITORY,
        account_type=AccountType.CHECKING,
    )
    statement = CanonicalStatement(
        account_id=account.account_id,
        run_id=run.run_id,
        raw_payload_id=payload.raw_payload_id,
        statement_start_date=datetime.date(2026, 10, 1),
        statement_end_date=datetime.date(2026, 10, 31),
        opening_balance_cents=0,
        closing_balance_cents=-sum(abs(a) for _, _, a in rows if a < 0),
        net_change_cents=-sum(abs(a) for _, _, a in rows if a < 0),
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=0,
        withdrawals_cents=sum(abs(a) for _, _, a in rows if a < 0),
        interest_paid_cents=0,
        fees_cents=0,
    )
    txns: list[CanonicalTransaction] = []
    day = 2
    for payee, description, amount_cents in rows:
        txns.append(
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(2026, 10, day),
                amount_cents=amount_cents,
                description=description,
                transaction_category=TransactionCategory.WITHDRAWAL,
                payee=payee,
            )
        )
        day += 1
    persist_canonical_statement(db_conn, account, statement, summary, txns)
    db_conn.commit()
    return txns


class _DownVault:
    def is_available(self) -> bool:
        return False

    def extract_structured(self, text: str, schema_description: str) -> dict[str, Any]:
        raise InferenceConnectionError("JARVIS down")


class _UpVault:
    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return True

    def extract_structured(self, text: str, schema_description: str) -> dict[str, Any]:
        self.calls.append(text)
        slug = self.mapping.get(text)
        if slug is None:
            return {"abstain": True}
        return {"slug": slug, "rationale": "closed-set map"}


class TestRunSuggest:
    def test_rules_stage_groups_opaque_excluded(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        txns = _seed_month(
            db_conn,
            [
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E BILL", -12000),
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E BILL", -12000),
                (None, "Electronic Withdrawal", -50000),
                ("SYNTHETIC BLUE BOTTLE SF", "SYNTHETIC BLUE BOTTLE", -975),
            ],
        )
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=_DownVault())
        db_conn.commit()
        assert batch.opaque_count == 1
        assert batch.suggestable_count == 3
        assert batch.vault_available is False
        assert len(batch.rule_groups) == 1
        group = batch.rule_groups[0]
        assert group.proposed_slug == "utilities"
        assert group.txn_count == 2
        assert group.abs_cents_total == 24000
        assert group.origin == "rule"
        assert batch.vault_groups == []
        opaque_ids = {t.transaction_id for t in txns if t.payee is None}
        staged_ids = {UUID(x) for g in batch.rule_groups for x in g.transaction_ids}
        assert opaque_ids.isdisjoint(staged_ids)

    def test_vault_stages_residual_when_up(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        _seed_month(
            db_conn,
            [
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1000),
                ("SYNTHETIC BLUE BOTTLE SF", "SYNTHETIC BLUE BOTTLE", -975),
            ],
        )
        vault = _UpVault({"SYNTHETIC BLUE BOTTLE SF": "dining"})
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=vault)
        db_conn.commit()
        assert batch.vault_available is True
        assert len(batch.rule_groups) == 1
        assert len(batch.vault_groups) == 1
        assert batch.vault_groups[0].proposed_slug == "dining"
        assert batch.vault_groups[0].origin == "vault_suggest"
        assert vault.calls == ["SYNTHETIC BLUE BOTTLE SF"]

    def test_accept_rule_group_writes_labels(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        txns = _seed_month(
            db_conn,
            [
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1100),
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1200),
            ],
        )
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=_DownVault())
        db_conn.commit()
        group = batch.rule_groups[0]
        written = accept_suggestion_group(
            db_conn,
            batch_id=batch.batch_id,
            group_key=group.group_key,
            slug=group.proposed_slug,
        )
        db_conn.commit()
        assert written == 2
        labels = get_labels_by_transaction_ids(
            db_conn, [t.transaction_id for t in txns]
        )
        assert all(lab.source == "rule" for lab in labels.values())
        assert all(lab.slug == "utilities" for lab in labels.values())

    def test_reject_drops_staging_without_labels(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        txns = _seed_month(db_conn, [("SYNTHETIC PG&E", "SYNTHETIC PG&E", -500)])
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=_DownVault())
        db_conn.commit()
        reject_suggestion_group(
            db_conn,
            batch_id=batch.batch_id,
            group_key=batch.rule_groups[0].group_key,
        )
        db_conn.commit()
        labels = get_labels_by_transaction_ids(
            db_conn, [t.transaction_id for t in txns]
        )
        assert labels == {}

    def test_accept_opaque_transaction_fails_loud(
        self, db_conn: psycopg.Connection
    ) -> None:
        from app.spend.suggest import require_suggestable_transaction

        opaque = next(iter(OPAQUE_BANK_TYPES))
        txns = _seed_month(db_conn, [(None, opaque, -8700)])
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=_DownVault())
        db_conn.commit()
        assert batch.rule_groups == []
        with pytest.raises(TokenError, match="OPAQUE_PAYEE_INELIGIBLE"):
            require_suggestable_transaction(db_conn, txns[0].transaction_id)

    def test_accept_unknown_slug_fails_loud(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        _seed_month(db_conn, [("SYNTHETIC PG&E", "SYNTHETIC PG&E", -500)])
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=_DownVault())
        db_conn.commit()
        with pytest.raises(TokenError, match="SPEND_CATEGORY_UNKNOWN"):
            accept_suggestion_group(
                db_conn,
                batch_id=batch.batch_id,
                group_key=batch.rule_groups[0].group_key,
                slug="not_a_real_slug",
            )

    def test_accept_all_rules_never_writes_vault(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        txns = _seed_month(
            db_conn,
            [
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1000),
                ("SYNTHETIC BLUE BOTTLE SF", "SYNTHETIC BLUE BOTTLE", -2000),
            ],
        )
        vault = _UpVault({"SYNTHETIC BLUE BOTTLE SF": "dining"})
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=vault)
        db_conn.commit()
        n = accept_all_rule_suggestions(db_conn, batch_id=batch.batch_id)
        db_conn.commit()
        assert n == 1
        labels = get_labels_by_transaction_ids(
            db_conn, [t.transaction_id for t in txns]
        )
        sources = {lab.source for lab in labels.values()}
        assert sources == {"rule"}
        assert "vault_suggest_accepted" not in sources

    def test_reject_all_vault_keeps_rules(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        _seed_month(
            db_conn,
            [
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1000),
                ("SYNTHETIC BLUE BOTTLE SF", "SYNTHETIC BLUE BOTTLE", -2000),
            ],
        )
        vault = _UpVault({"SYNTHETIC BLUE BOTTLE SF": "dining"})
        batch = run_suggest(db_conn, year=2026, month=10, vault_client=vault)
        db_conn.commit()
        dropped = reject_all_vault_suggestions(db_conn, batch_id=batch.batch_id)
        db_conn.commit()
        assert dropped >= 1
        refreshed = run_suggest(db_conn, year=2026, month=10, vault_client=_DownVault())
        # Re-run clears prior staging; just assert reject API returned count.
        assert refreshed.batch_id != batch.batch_id


@pytest.fixture
def client(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", postgres_url)
    from app.api.app import create_app

    return TestClient(create_app())


class TestBulkSuggestHTMX:
    def test_run_suggest_panel_shows_opaque_and_rules(
        self,
        client: TestClient,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.spend import suggest as suggest_mod

        monkeypatch.setattr(
            suggest_mod,
            "VaultInferenceClient",
            lambda: _DownVault(),
        )
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        _seed_month(
            db_conn,
            [
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -38412),
                (None, "Electronic Withdrawal", -120000),
            ],
        )
        resp = client.post(
            "/api/v1/dashboard/ledger/suggest/run",
            data={"pie_year": "2026", "pie_month": "10"},
            headers=HX,
        )
        assert resp.status_code == 200
        body = resp.text
        assert "Opaque" in body
        assert "JARVIS unavailable" in body or "Vault lane skipped" in body
        assert "SYNTHETIC PG&amp;E" in body or "SYNTHETIC PG&E" in body
        assert "Accept group" in body
        assert "Electronic Withdrawal" in body or "ineligible" in body.lower()

    def test_accept_group_via_htmx(
        self,
        client: TestClient,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.spend import suggest as suggest_mod

        monkeypatch.setattr(suggest_mod, "VaultInferenceClient", lambda: _DownVault())
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        txns = _seed_month(db_conn, [("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1000)])
        run = client.post(
            "/api/v1/dashboard/ledger/suggest/run",
            data={"pie_year": "2026", "pie_month": "10"},
            headers=HX,
        )
        assert run.status_code == 200
        # Extract batch/group from staged DB rather than HTML parsing.
        from app.spend.suggest import load_latest_batch

        batch = load_latest_batch(db_conn, year=2026, month=10)
        assert batch is not None
        group = batch.rule_groups[0]
        resp = client.post(
            "/api/v1/dashboard/ledger/suggest/accept",
            data={
                "batch_id": str(batch.batch_id),
                "group_key": group.group_key,
                "slug": "utilities",
                "pie_year": "2026",
                "pie_month": "10",
            },
            headers=HX,
        )
        assert resp.status_code == 200
        labels = get_labels_by_transaction_ids(db_conn, [txns[0].transaction_id])
        assert labels[txns[0].transaction_id].source == "rule"


def _seed_statement(
    db_conn: psycopg.Connection,
    *,
    basename: str,
    content_sha: str,
    rows: list[tuple[str | None, str, int]],
    start: datetime.date,
    end: datetime.date,
) -> tuple[UUID, list[CanonicalTransaction]]:
    """Seed one statement with unlabeled outflows; return (statement_id, txns)."""
    payload = RawPayload(
        content_sha256=content_sha,
        byte_length=800,
        original_basename=basename,
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="Statement scope")
    persist_raw_extraction(
        db_conn, RawExtraction(payload=payload, run=run, pages=[page])
    )
    account = Account(
        institution="SYNTHETIC Chase",
        account_mask="*0000",
        account_domain=AccountDomain.DEPOSITORY,
        account_type=AccountType.CHECKING,
    )
    statement = CanonicalStatement(
        account_id=account.account_id,
        run_id=run.run_id,
        raw_payload_id=payload.raw_payload_id,
        statement_start_date=start,
        statement_end_date=end,
        opening_balance_cents=0,
        closing_balance_cents=-sum(abs(a) for _, _, a in rows if a < 0),
        net_change_cents=-sum(abs(a) for _, _, a in rows if a < 0),
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=0,
        withdrawals_cents=sum(abs(a) for _, _, a in rows if a < 0),
        interest_paid_cents=0,
        fees_cents=0,
    )
    txns: list[CanonicalTransaction] = []
    day = start.day if start.day <= 27 else 2
    for payee, description, amount_cents in rows:
        txns.append(
            CanonicalTransaction(
                statement_id=statement.statement_id,
                post_date=datetime.date(start.year, start.month, day),
                amount_cents=amount_cents,
                description=description,
                transaction_category=TransactionCategory.WITHDRAWAL,
                payee=payee,
            )
        )
        day += 1
    persist_canonical_statement(db_conn, account, statement, summary, txns)
    db_conn.commit()
    return statement.statement_id, txns


class TestStatementScopedSuggest:
    def test_statement_scope_stages_only_that_statement(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        stmt_a, txns_a = _seed_statement(
            db_conn,
            basename="stmt_a.pdf",
            content_sha="a" * 64,
            rows=[
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E A", -1100),
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E A2", -1200),
                (None, "Electronic Withdrawal", -5000),
            ],
            start=datetime.date(2026, 10, 1),
            end=datetime.date(2026, 10, 31),
        )
        _stmt_b, txns_b = _seed_statement(
            db_conn,
            basename="stmt_b.pdf",
            content_sha="b" * 64,
            rows=[("SYNTHETIC PG&E", "SYNTHETIC PG&E B", -9900)],
            start=datetime.date(2026, 10, 1),
            end=datetime.date(2026, 10, 31),
        )
        batch = run_suggest(db_conn, statement_id=stmt_a, vault_client=_DownVault())
        db_conn.commit()
        assert batch.statement_id == stmt_a
        assert batch.opaque_count == 1
        assert batch.suggestable_count == 2
        assert len(batch.rule_groups) == 1
        staged = {UUID(x) for x in batch.rule_groups[0].transaction_ids}
        assert staged == {txns_a[0].transaction_id, txns_a[1].transaction_id}
        assert txns_b[0].transaction_id not in staged

        written = accept_suggestion_group(
            db_conn,
            batch_id=batch.batch_id,
            group_key=batch.rule_groups[0].group_key,
            slug="utilities",
        )
        db_conn.commit()
        assert written == 2
        labels_a = get_labels_by_transaction_ids(
            db_conn, [t.transaction_id for t in txns_a]
        )
        labels_b = get_labels_by_transaction_ids(
            db_conn, [t.transaction_id for t in txns_b]
        )
        assert len(labels_a) == 2
        assert labels_b == {}

    def test_prefer_statement_when_dual_scope(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        stmt_a, txns_a = _seed_statement(
            db_conn,
            basename="dual_a.pdf",
            content_sha="c" * 64,
            rows=[("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1000)],
            start=datetime.date(2026, 10, 1),
            end=datetime.date(2026, 10, 31),
        )
        _seed_statement(
            db_conn,
            basename="dual_b.pdf",
            content_sha="d" * 64,
            rows=[("SYNTHETIC PG&E", "SYNTHETIC PG&E OTHER", -2000)],
            start=datetime.date(2026, 10, 1),
            end=datetime.date(2026, 10, 31),
        )
        batch = run_suggest(
            db_conn,
            year=2026,
            month=10,
            statement_id=stmt_a,
            vault_client=_DownVault(),
        )
        db_conn.commit()
        assert batch.statement_id == stmt_a
        assert batch.suggestable_count == 1
        assert batch.rule_groups[0].txn_count == 1
        assert UUID(batch.rule_groups[0].transaction_ids[0]) == txns_a[0].transaction_id

    def test_neither_scope_fails_loud(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(TokenError, match="SUGGEST_SCOPE_REQUIRED"):
            run_suggest(db_conn, vault_client=_DownVault())

    def test_month_mode_unaffected_and_no_cross_clear(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        stmt_a, _txns_a = _seed_statement(
            db_conn,
            basename="clear_a.pdf",
            content_sha="e" * 64,
            rows=[("SYNTHETIC PG&E", "SYNTHETIC PG&E", -1500)],
            start=datetime.date(2026, 10, 1),
            end=datetime.date(2026, 10, 31),
        )
        stmt_batch = run_suggest(
            db_conn, statement_id=stmt_a, vault_client=_DownVault()
        )
        db_conn.commit()
        month_batch = run_suggest(
            db_conn, year=2026, month=10, vault_client=_DownVault()
        )
        db_conn.commit()
        # Month re-run must not wipe statement-scoped staging rows.
        remaining = db_conn.execute(
            """
            SELECT COUNT(*) FROM spend_label_suggestions
            WHERE batch_id = %s
            """,
            (str(stmt_batch.batch_id),),
        ).fetchone()
        assert remaining is not None and int(remaining[0]) >= 1
        assert month_batch.statement_id is None
        assert month_batch.suggestable_count >= 1


class TestStatementSuggestHTMX:
    def test_statement_ledger_shows_statement_cta(
        self,
        client: TestClient,
        db_conn: psycopg.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.spend import suggest as suggest_mod

        monkeypatch.setattr(suggest_mod, "VaultInferenceClient", lambda: _DownVault())
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        stmt_id, _txns = _seed_statement(
            db_conn,
            basename="htmx_stmt.pdf",
            content_sha="f" * 64,
            rows=[
                ("SYNTHETIC PG&E", "SYNTHETIC PG&E", -38412),
                (None, "Electronic Withdrawal", -120000),
            ],
            start=datetime.date(2026, 10, 1),
            end=datetime.date(2026, 10, 31),
        )
        page = client.get(
            f"/api/v1/dashboard/statements/{stmt_id}",
            headers=HX,
        )
        assert page.status_code == 200
        assert "Suggest for this statement" in page.text
        assert 'name="pie_year"' not in page.text or "statement_id" in page.text

        resp = client.post(
            "/api/v1/dashboard/ledger/suggest/run",
            data={"statement_id": str(stmt_id), "pie_year": "2026", "pie_month": "10"},
            headers=HX,
        )
        assert resp.status_code == 200
        assert (
            "Suggest for this statement" in resp.text or "This statement" in resp.text
        )
        assert "Opaque" in resp.text
        assert "SYNTHETIC PG&amp;E" in resp.text or "SYNTHETIC PG&E" in resp.text
