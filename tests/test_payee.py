"""Payee candidate policy + CanonicalTransaction.payee invariants."""

from __future__ import annotations

import datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.adapters.payee import OPAQUE_BANK_TYPES, payee_from_description
from app.models.canonical import CanonicalTransaction
from app.models.enums import TransactionCategory
from app.models.exceptions import TokenError


class TestPayeeFromDescription:
    def test_merchant_description_becomes_payee(self) -> None:
        assert (
            payee_from_description("SYNTHETIC ACME GROCERY") == "SYNTHETIC ACME GROCERY"
        )

    def test_trims_whitespace(self) -> None:
        assert payee_from_description("  TING* STORE  ") == "TING* STORE"

    def test_opaque_bank_types_yield_none(self) -> None:
        for opaque in sorted(OPAQUE_BANK_TYPES):
            assert payee_from_description(opaque) is None

    def test_empty_string_fails_loud(self) -> None:
        with pytest.raises(TokenError, match="empty"):
            payee_from_description("")

    def test_whitespace_only_fails_loud(self) -> None:
        with pytest.raises(TokenError, match="empty"):
            payee_from_description("   \t  ")

    def test_non_str_fails_loud(self) -> None:
        with pytest.raises(TokenError, match="str"):
            payee_from_description(None)  # type: ignore[arg-type]


class TestCanonicalTransactionPayee:
    def test_payee_optional_defaults_none(self) -> None:
        txn = CanonicalTransaction(
            statement_id=uuid4(),
            post_date=datetime.date(2026, 10, 1),
            amount_cents=-100,
            description="Electronic Withdrawal",
            transaction_category=TransactionCategory.WITHDRAWAL,
        )
        assert txn.payee is None

    def test_payee_accepts_synthetic_merchant(self) -> None:
        txn = CanonicalTransaction(
            statement_id=uuid4(),
            post_date=datetime.date(2026, 10, 1),
            amount_cents=-45231,
            description="Purchase",
            transaction_category=TransactionCategory.PURCHASE,
            payee="SYNTHETIC Groceries Co",
        )
        assert txn.payee == "SYNTHETIC Groceries Co"

    def test_empty_payee_string_fails_loud(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalTransaction(
                statement_id=uuid4(),
                post_date=datetime.date(2026, 10, 1),
                amount_cents=-100,
                description="Purchase",
                transaction_category=TransactionCategory.PURCHASE,
                payee="",
            )

    def test_whitespace_payee_fails_loud(self) -> None:
        with pytest.raises(ValidationError):
            CanonicalTransaction(
                statement_id=uuid4(),
                post_date=datetime.date(2026, 10, 1),
                amount_cents=-100,
                description="Purchase",
                transaction_category=TransactionCategory.PURCHASE,
                payee="   ",
            )

    def test_untrimmed_payee_fails_loud(self) -> None:
        with pytest.raises(ValidationError, match="whitespace"):
            CanonicalTransaction(
                statement_id=uuid4(),
                post_date=datetime.date(2026, 10, 1),
                amount_cents=-100,
                description="Purchase",
                transaction_category=TransactionCategory.PURCHASE,
                payee=" SYNTHETIC ACME ",
            )
