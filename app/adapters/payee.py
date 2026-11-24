"""Payee / counterparty candidates from printed statement description text.

Never invent merchants. Opaque bank-type strings yield ``None``. Empty or
whitespace-only descriptions fail loud.
"""

from __future__ import annotations

from app.models.exceptions import TokenError

# Exact bank-type strings that are not counterparty/payee signals.
# Keep in sync with docs/specs/payee_signal_inventory.md.
OPAQUE_BANK_TYPES = frozenset(
    {
        "Electronic Withdrawal",
        "Electronic Deposit",
        "Interest Paid",
        "Incoming Wire",
        "Funds Transfer from Brokerage",
        "Mobile Deposit",
        "Check",
        "ACH Bill Pay",
    }
)


def payee_from_description(description: str) -> str | None:
    """Return a payee candidate from printed description text.

    Args:
        description: Raw bank description (must be non-empty after strip).

    Returns:
        Stripped description when it is not an opaque bank type; ``None``
        when the description is an opaque type with no counterparty.

    Raises:
        TokenError: If *description* is not a ``str`` or is empty/whitespace.
    """
    if not isinstance(description, str):
        raise TokenError("payee candidate requires description str")
    stripped = description.strip()
    if not stripped:
        raise TokenError("payee candidate refuses empty description")
    if stripped in OPAQUE_BANK_TYPES:
        return None
    return stripped
