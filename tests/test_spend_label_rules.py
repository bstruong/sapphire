"""Spend-label rules: normalize + exact match (bulk suggest B1)."""

from __future__ import annotations

import datetime
from uuid import uuid4

import psycopg
import pytest

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
from app.models.exceptions import TokenError
from app.models.raw import ExtractionRun, RawExtraction, RawPage, RawPayload
from app.spend.categories import list_spend_categories
from app.spend.rules import (
    insert_spend_label_rule,
    list_enabled_rules,
    match_payee_rule,
    normalize_payee,
)


def _seed_payee_txn(
    db_conn: psycopg.Connection,
    *,
    payee: str | None,
    description: str = "SYNTHETIC PURCHASE",
    amount_cents: int = -5000,
    post_date: datetime.date | None = None,
) -> CanonicalTransaction:
    payload = RawPayload(
        content_sha256="a" * 64,
        byte_length=400,
        original_basename="rule_match.pdf",
    )
    run = ExtractionRun(
        raw_payload_id=payload.raw_payload_id,
        adapter_id="standard_depository",
        adapter_version="1.0.0",
        status=RunStatus.VALIDATED,
    )
    page = RawPage(run_id=run.run_id, page_number=1, page_text="Rule match")
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
        closing_balance_cents=95000,
        net_change_cents=-5000,
    )
    summary = DepositorySummary(
        statement_id=statement.statement_id,
        deposits_cents=0,
        withdrawals_cents=abs(amount_cents),
        interest_paid_cents=0,
        fees_cents=0,
    )
    txn = CanonicalTransaction(
        statement_id=statement.statement_id,
        post_date=post_date or datetime.date(2026, 10, 12),
        amount_cents=amount_cents,
        description=description,
        transaction_category=TransactionCategory.WITHDRAWAL,
        payee=payee,
    )
    persist_canonical_statement(db_conn, account, statement, summary, [txn])
    db_conn.commit()
    return txn


class TestNormalizePayee:
    def test_lower_and_collapse_whitespace(self) -> None:
        assert normalize_payee("  SYNTHETIC   Whole Foods  ") == "synthetic whole foods"

    def test_empty_fails_loud(self) -> None:
        with pytest.raises(TokenError, match="PAYEE_REQUIRED"):
            normalize_payee("   ")

    def test_non_str_fails_loud(self) -> None:
        with pytest.raises(TokenError, match="PAYEE_REQUIRED"):
            normalize_payee(None)  # type: ignore[arg-type]


class TestSpendLabelRules:
    def test_insert_and_exact_match(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        rule = insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC PG&E",
            spend_category_id=cats["utilities"].spend_category_id,
        )
        db_conn.commit()
        assert rule.payee_pattern == "synthetic pg&e"
        assert rule.enabled is True

        hit = match_payee_rule(db_conn, "  Synthetic  PG&E ")
        assert hit is not None
        assert hit.slug == "utilities"
        assert hit.rule_id == rule.rule_id

    def test_no_match_returns_none(self, db_conn: psycopg.Connection) -> None:
        assert match_payee_rule(db_conn, "SYNTHETIC UNKNOWN MERCHANT") is None

    def test_null_payee_fails_loud_on_match(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(TokenError, match="PAYEE_REQUIRED"):
            match_payee_rule(db_conn, None)  # type: ignore[arg-type]

    def test_duplicate_normalized_pattern_fails_loud(
        self, db_conn: psycopg.Connection
    ) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC SPOTIFY",
            spend_category_id=cats["other"].spend_category_id,
        )
        db_conn.commit()
        with pytest.raises(TokenError, match="SPEND_LABEL_RULE_AMBIGUOUS"):
            insert_spend_label_rule(
                db_conn,
                payee_pattern="  synthetic   spotify ",
                spend_category_id=cats["dining"].spend_category_id,
            )

    def test_unknown_category_fails_loud(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(TokenError, match="SPEND_CATEGORY"):
            insert_spend_label_rule(
                db_conn,
                payee_pattern="SYNTHETIC X",
                spend_category_id=uuid4(),
            )

    def test_list_enabled_only(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC A",
            spend_category_id=cats["rent"].spend_category_id,
            enabled=True,
        )
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC B",
            spend_category_id=cats["groceries"].spend_category_id,
            enabled=False,
        )
        db_conn.commit()
        enabled = list_enabled_rules(db_conn)
        patterns = {r.payee_pattern for r in enabled}
        assert "synthetic a" in patterns
        assert "synthetic b" not in patterns

    def test_seed_helper_usable_for_matcher(self, db_conn: psycopg.Connection) -> None:
        cats = {c.slug: c for c in list_spend_categories(db_conn)}
        insert_spend_label_rule(
            db_conn,
            payee_pattern="SYNTHETIC WHOLE FOODS *0000",
            spend_category_id=cats["groceries"].spend_category_id,
        )
        db_conn.commit()
        _seed_payee_txn(db_conn, payee="SYNTHETIC WHOLE FOODS *0000")
        hit = match_payee_rule(db_conn, "SYNTHETIC WHOLE FOODS *0000")
        assert hit is not None
        assert hit.slug == "groceries"
