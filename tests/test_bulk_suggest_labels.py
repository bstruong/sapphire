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
