"""Chase revolving-credit (credit card) statement adapter."""

from __future__ import annotations

from pathlib import Path

from app.adapters.credit_card import StandardCreditCardAdapter
from app.models.enums import AccountType


class ChaseCreditCardAdapter(StandardCreditCardAdapter):
    """Chase credit-card statements with Standard Card–compatible section layout.

    Matches page-1 Chase card branding exclusively. Parsing reuses the credit-card
    summary/transaction markers (ACCOUNT SUMMARY, TRANSACTIONS) so fixture-faithful
    Chase card PDFs reconcile with integer cents. Production layout drift fails
    loudly via the inherited validators.
    """

    adapter_id = "chase_credit_card"
    adapter_version = "1.0.0"
    institution_id = "Chase"
    account_types = frozenset({AccountType.CREDIT_CARD})

    def matches(self, file_path: Path) -> bool:
        """Heuristic check on first page text for Chase credit-card branding."""
        resolved = file_path.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"File not found: {resolved}")
        try:
            import pdfplumber

            with pdfplumber.open(resolved) as pdf:
                if not pdf.pages:
                    return False
                first_text = (pdf.pages[0].extract_text() or "").upper()
                if "STANDARD CARD" in first_text:
                    return False
                if "CHECKING" in first_text or "SAVINGS" in first_text:
                    return False
                if "COMBINED" in first_text:
                    return False
                chase_brand = (
                    "JPMORGAN CHASE" in first_text
                    or "JP MORGAN CHASE" in first_text
                    or (
                        "CHASE" in first_text
                        and (
                            "CHASE CREDIT CARD" in first_text
                            or "CHASE CARD" in first_text
                            or first_text.strip().startswith("CHASE")
                        )
                    )
                )
                if not chase_brand:
                    return False
                card_markers = (
                    "CREDIT CARD" in first_text
                    or "CARDMEMBER" in first_text
                    or "CARD MEMBER" in first_text
                    or "CARD STATEMENT" in first_text
                )
                return card_markers
        except Exception:  # noqa: BLE001
            return False
