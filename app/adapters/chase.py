"""Chase depository statement adapter (checking / savings)."""

from __future__ import annotations

from pathlib import Path

from app.adapters.depository import StandardDepositoryAdapter
from app.models.enums import AccountType


class ChaseDepositoryAdapter(StandardDepositoryAdapter):
    """Chase checking/savings statements with Standard Bank–compatible section layout.

    Matches page-1 Chase branding exclusively. Parsing reuses the depository
    summary/transaction markers (ACCOUNT SUMMARY, TRANSACTION DETAILS) so
    fixture-faithful Chase PDFs reconcile with integer cents. Production Chase
    layout drift fails loudly via the inherited validators.
    """

    adapter_id = "chase_depository"
    adapter_version = "1.0.0"
    institution_id = "Chase"
    account_types = frozenset({AccountType.CHECKING, AccountType.SAVINGS})

    def matches(self, file_path: Path) -> bool:
        """Heuristic check on first page text for Chase branding."""
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")
        try:
            import pdfplumber

            with pdfplumber.open(resolved) as pdf:
                if not pdf.pages:
                    return False
                first_text = (pdf.pages[0].extract_text() or "").upper()
                if "STANDARD BANK" in first_text:
                    return False
                chase_brand = (
                    "JPMorgan Chase".upper() in first_text
                    or "JPMORGAN CHASE" in first_text
                    or (
                        "CHASE" in first_text
                        and (
                            "CHASE BANK" in first_text
                            or "CHASE CHECKING" in first_text
                            or "CHASE SAVINGS" in first_text
                            or first_text.strip().startswith("CHASE")
                        )
                    )
                )
                if not chase_brand:
                    return False
                # Credit-card packets belong to ChaseCreditCardAdapter exclusively.
                if (
                    "CREDIT CARD" in first_text
                    or "CARDMEMBER" in first_text
                    or "CARD MEMBER" in first_text
                    or "CARD STATEMENT" in first_text
                ):
                    return False
                if "COMBINED" in first_text or (
                    "CHECKING SUMMARY" in first_text and "SAVINGS SUMMARY" in first_text
                ):
                    return False
                return "CHECKING" in first_text or "SAVINGS" in first_text
        except Exception:  # noqa: BLE001
            return False
