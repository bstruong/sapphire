"""Spend-category taxonomy + manual labels (category spend step 3)."""

from __future__ import annotations

import datetime
from uuid import uuid4

import psycopg
import pytest

from app.db.repository import (
    get_canonical_statement,
    persist_canonical_statement,
    persist_raw_extraction,
)
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
from app.models.exceptions import TokenError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
from app.spend.categories import (
    SYSTEM_SLUGS,
    clear_manual_label,
    get_labels_by_transaction_ids,
    list_spend_categories,
    set_manual_label,
)


def _seed_txn(db_conn: psycopg.Connection) -> CanonicalTransaction:
    payload = RawPayload(
        content_sha256="f" * 64,
        byte_length=900,
        original_basename="spend_label.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="Spend label")
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
        statement_start_date=datetime.date(2026, 10, 1),
        statement_end_date=datetime.date(2026, 10, 31),
        opening_balance_cents=100000,
        closing_balance_cents=50000,
        net_change_cents=-50000,
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=0,
        withdrawals_cents=50000,
        interest_paid_cents=0,
        fees_cents=0,
    )
    txn = CanonicalTransaction(
        statement_id=statement.statement_id,
        post_date=datetime.date(2026, 10, 5),
        amount_cents=-50000,
        description="Electronic Withdrawal",
        transaction_category=TransactionCategory.WITHDRAWAL,
        payee=None,
    )
    persist_canonical_statement(db_conn, account, statement, summary, [txn])
    db_conn.commit()
    return txn


class TestSpendCategorySeed:
    def test_system_slugs_seeded(self, db_conn: psycopg.Connection) -> None:
        cats = list_spend_categories(db_conn)
        slugs = {c.slug for c in cats}
        assert SYSTEM_SLUGS <= slugs
        assert all(c.is_system for c in cats if c.slug in SYSTEM_SLUGS)


class TestManualSpendLabel:
    def test_set_and_load_manual_label(self, db_conn: psycopg.Connection) -> None:
        txn = _seed_txn(db_conn)
        label = set_manual_label(
            db_conn, transaction_id=txn.transaction_id, slug="rent"
        )
        db_conn.commit()
        assert label.slug == "rent"
        assert label.source == "manual"
        loaded = get_labels_by_transaction_ids(db_conn, [txn.transaction_id])
        assert loaded[txn.transaction_id].display_name == "Rent"

    def test_unknown_slug_fails_loud(self, db_conn: psycopg.Connection) -> None:
        txn = _seed_txn(db_conn)
        with pytest.raises(TokenError, match="SPEND_CATEGORY_UNKNOWN"):
            set_manual_label(
                db_conn, transaction_id=txn.transaction_id, slug="groceries_typo"
            )

    def test_empty_slug_fails_loud(self, db_conn: psycopg.Connection) -> None:
        txn = _seed_txn(db_conn)
        with pytest.raises(TokenError, match="SPEND_CATEGORY_SLUG_REQUIRED"):
            set_manual_label(db_conn, transaction_id=txn.transaction_id, slug="  ")

    def test_missing_transaction_fails_loud(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(TokenError, match="TRANSACTION_NOT_FOUND"):
            set_manual_label(db_conn, transaction_id=uuid4(), slug="groceries")

    def test_clear_label_returns_uncategorized(
        self, db_conn: psycopg.Connection
    ) -> None:
        txn = _seed_txn(db_conn)
        set_manual_label(db_conn, transaction_id=txn.transaction_id, slug="dining")
        db_conn.commit()
        clear_manual_label(db_conn, transaction_id=txn.transaction_id)
        db_conn.commit()
        loaded = get_labels_by_transaction_ids(db_conn, [txn.transaction_id])
        assert txn.transaction_id not in loaded

    def test_upsert_replaces_prior_label(self, db_conn: psycopg.Connection) -> None:
        txn = _seed_txn(db_conn)
        set_manual_label(db_conn, transaction_id=txn.transaction_id, slug="rent")
        set_manual_label(db_conn, transaction_id=txn.transaction_id, slug="utilities")
        db_conn.commit()
        loaded = get_labels_by_transaction_ids(db_conn, [txn.transaction_id])
        assert loaded[txn.transaction_id].slug == "utilities"

    def test_roundtrip_keeps_description_and_payee(
        self, db_conn: psycopg.Connection
    ) -> None:
        txn = _seed_txn(db_conn)
        set_manual_label(db_conn, transaction_id=txn.transaction_id, slug="other")
        db_conn.commit()
        res = get_canonical_statement(db_conn, txn.statement_id)
        assert res is not None
        _stmt, _summary, txns = res
        assert txns[0].description == "Electronic Withdrawal"
        assert txns[0].payee is None

    def test_non_uuid_transaction_id_fails_loud(
        self, db_conn: psycopg.Connection
    ) -> None:
        with pytest.raises(TokenError, match="UUID"):
            set_manual_label(db_conn, transaction_id="not-a-uuid", slug="rent")  # type: ignore[arg-type]
        with pytest.raises(TokenError, match="UUID"):
            clear_manual_label(db_conn, transaction_id="not-a-uuid")  # type: ignore[arg-type]
        with pytest.raises(TokenError, match="UUID"):
            get_labels_by_transaction_ids(db_conn, ["nope"])  # type: ignore[list-item]

    def test_empty_id_list_returns_empty(self, db_conn: psycopg.Connection) -> None:
        assert get_labels_by_transaction_ids(db_conn, []) == {}
