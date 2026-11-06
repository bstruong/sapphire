"""Classify Vault account balances as liquid cash vs brokerage funds.

Schwab Bank Investor Checking (depository) is liquid. Custodial brokerage
cash/portfolio is non-liquid invested funds. Revolving credit is a liability
and is excluded from both cash buckets.
"""

from __future__ import annotations

from app.models.enums import AccountDomain

LIQUIDITY_LIQUID = "liquid"
LIQUIDITY_BROKERAGE = "brokerage"
LIQUIDITY_LIABILITY = "liability"


def liquidity_class_for_domain(domain: AccountDomain | str) -> str:
    """Return liquidity bucket for an account domain.

    Raises:
        TokenError-equivalent ValueError: unknown domain (fail loud).
    """
    value = domain.value if isinstance(domain, AccountDomain) else str(domain)
    if value == AccountDomain.DEPOSITORY.value:
        return LIQUIDITY_LIQUID
    if value == AccountDomain.CUSTODIAL_BROKERAGE.value:
        return LIQUIDITY_BROKERAGE
    if value == AccountDomain.REVOLVING_CREDIT.value:
        return LIQUIDITY_LIABILITY
    raise ValueError(f"Unsupported account domain for liquidity class: {value!r}")


def liquidity_label(liquidity_class: str) -> str:
    """Human label for overview/ledger chips."""
    if liquidity_class == LIQUIDITY_LIQUID:
        return "Liquid"
    if liquidity_class == LIQUIDITY_BROKERAGE:
        return "Non-liquid"
    if liquidity_class == LIQUIDITY_LIABILITY:
        return "Liability"
    raise ValueError(f"Unsupported liquidity class: {liquidity_class!r}")
