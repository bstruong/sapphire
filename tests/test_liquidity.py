"""Unit tests for liquid vs brokerage fund classification."""

from __future__ import annotations

import pytest

from app.models.enums import AccountDomain
from app.presentation.liquidity import (
    LIQUIDITY_BROKERAGE,
    LIQUIDITY_LIABILITY,
    LIQUIDITY_LIQUID,
    liquidity_class_for_domain,
    liquidity_label,
)


def test_depository_is_liquid() -> None:
    assert liquidity_class_for_domain(AccountDomain.DEPOSITORY) == LIQUIDITY_LIQUID
    assert liquidity_label(LIQUIDITY_LIQUID) == "Liquid"


def test_brokerage_is_non_liquid() -> None:
    assert (
        liquidity_class_for_domain(AccountDomain.CUSTODIAL_BROKERAGE)
        == LIQUIDITY_BROKERAGE
    )
    assert liquidity_label(LIQUIDITY_BROKERAGE) == "Non-liquid"


def test_credit_is_liability() -> None:
    assert (
        liquidity_class_for_domain(AccountDomain.REVOLVING_CREDIT)
        == LIQUIDITY_LIABILITY
    )
    assert liquidity_label(LIQUIDITY_LIABILITY) == "Liability"


def test_unknown_domain_fails_loud() -> None:
    with pytest.raises(ValueError, match="Unsupported account domain"):
        liquidity_class_for_domain("not_a_domain")
