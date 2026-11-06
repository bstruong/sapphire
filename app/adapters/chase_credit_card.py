"""Chase revolving-credit (credit card) statement adapter."""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.adapters.credit_card import StandardCreditCardAdapter
from app.extraction.core import parse_currency_to_cents
from app.models.canonical import CanonicalTransaction, CreditCardSummary
from app.models.enums import (
    AccountType,
    TransactionCategory,
    validate_category_for_domain,
    validate_category_sign,
)
from app.models.exceptions import (
    AmbiguousAccountsError,
    InvariantError,
    MissingSectionError,
    TokenError,
)
from app.models.raw import RawExtraction
from app.models.schema import TableSchema

logger = logging.getLogger(__name__)

_SUMMARY_MONEY = r"([($]?-?\$?-?[\d,]+\.\d{2}\)?|\+\$[\d,]+\.\d{2})"
_TXN_LINE = re.compile(
    r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(.+?)\s+(-?\$?[\d,]+\.\d{2})\s*$"
)


def _require_non_negative(field_name: str, cents: int) -> int:
    if cents < 0:
        raise InvariantError(f"{field_name} must be >= 0")
    return cents


class ChaseCreditCardAdapter(StandardCreditCardAdapter):
    """Chase Ultimate Rewards / card statements from layout-faithful redacted PDFs.

    Matches page-1 Chase card chrome (Ultimate Rewards + Account Summary /
    Opening/Closing Date). Parsing follows real Chase labels (`Payment, Credits`,
    `TRANSACTIONS THIS CYCLE`) and fails loudly on drift. Unsigned Amount-column
    values are liability increases (Chase print convention); sign is never inferred
    from merchant description text.
    """

    adapter_id = "chase_credit_card"
    adapter_version = "1.1.0"
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
                    or "CHASE ULTIMATE" in first_text
                    or ("CHASE" in first_text and "ULTIMATE REWARDS" in first_text)
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
                    or "OPENING/CLOSING DATE" in first_text
                    or (
                        "ACCOUNT SUMMARY" in first_text
                        and "PREVIOUS BALANCE" in first_text
                    )
                )
                return card_markers
        except Exception:  # noqa: BLE001
            return False

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Chase card section anchors (real statement labels).

        Prefer `Previous Balance` over the `ACCOUNT SUMMARY` heading: raw Chase
        PDFs often omit that heading in pdfplumber text even when the summary
        buckets are present. Redacted exports keep both.
        """
        return (
            TableSchema(
                name="card_transactions",
                headers=("date", "description", "amount"),
                section_markers=("Previous Balance", "TRANSACTIONS THIS CYCLE"),
            ),
        )

    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, datetime.date, datetime.date]:
        """Extract account mask and Opening/Closing Date period."""
        logger.info(
            "Parsing Chase credit card header",
            extra={"run_id": str(extraction.run.run_id)},
        )
        all_text = "\n".join(p.page_text for p in extraction.pages)

        account_matches = re.findall(
            r"Account\s+Number[:\s]+(\[[A-Z_]+_\d+\]|[*\dX-]+)",
            all_text,
            re.IGNORECASE,
        )
        if not account_matches:
            account_matches = re.findall(
                r"(?:Card|Account)\s*(?:Number)?[:\s#]+(\[[A-Z_]+_\d+\]|[*\dX-]+)",
                all_text,
                re.IGNORECASE,
            )
        unique_masks = sorted(set(account_matches))
        if len(unique_masks) > 1:
            raise AmbiguousAccountsError(
                f"Multiple card masks detected: {unique_masks}"
            )
        if not unique_masks:
            raise MissingSectionError("Could not locate card number on statement.")
        card_mask = unique_masks[0]

        date_match = re.search(
            r"Opening/Closing Date[:\s]+"
            r"(\d{1,2}/\d{1,2}/\d{2,4})\s*[-–—]\s*(\d{1,2}/\d{1,2}/\d{2,4})",
            all_text,
            re.IGNORECASE,
        )
        if not date_match:
            date_match = re.search(
                r"Statement Period[:\s]+"
                r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\s*(?:to|-)\s*"
                r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
                all_text,
                re.IGNORECASE,
            )
        if not date_match:
            raise MissingSectionError("Could not locate statement period dates.")

        start_date = self._parse_date(date_match.group(1))
        end_date = self._parse_date(date_match.group(2))
        return card_mask, start_date, end_date

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, CreditCardSummary]:
        """Extract Chase summary buckets (Payment, Credits label; abs payments)."""
        all_text = "\n".join(p.page_text for p in extraction.pages)

        def extract_signed_cents(label: str) -> int:
            match = re.search(
                rf"{re.escape(label)}[:\s]+{_SUMMARY_MONEY}",
                all_text,
                re.IGNORECASE,
            )
            if not match:
                raise MissingSectionError(f"Missing summary field: {label}")
            return parse_currency_to_cents(match.group(1).strip(), allow_zero=True)

        prev_balance_cents = extract_signed_cents("Previous Balance")
        payments_raw = extract_signed_cents("Payment, Credits")
        payments_credits_cents = _require_non_negative(
            "payments_credits_cents", abs(payments_raw)
        )
        purchases_cents = _require_non_negative(
            "purchases_cents", extract_signed_cents("Purchases")
        )
        cash_advances_cents = _require_non_negative(
            "cash_advances_cents", extract_signed_cents("Cash Advances")
        )
        balance_transfers_cents = _require_non_negative(
            "balance_transfers_cents", extract_signed_cents("Balance Transfers")
        )
        fees_charged_cents = _require_non_negative(
            "fees_charged_cents", extract_signed_cents("Fees Charged")
        )
        interest_charged_cents = _require_non_negative(
            "interest_charged_cents", extract_signed_cents("Interest Charged")
        )
        new_balance_cents = extract_signed_cents("New Balance")

        due_date: datetime.date | None = None
        due_match = re.search(
            r"Payment Due Date[:\s]+(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})",
            all_text,
            re.IGNORECASE,
        )
        if due_match:
            due_date = self._parse_date(due_match.group(1))

        if self.requires_payment_due_date and due_date is None:
            raise MissingSectionError(
                "Payment due date is required on card statements."
            )

        min_match = re.search(
            r"Minimum Payment Due[:\s]+([-(]?\$?[\d,]+\.\d{2}\)?)",
            all_text,
            re.IGNORECASE,
        )
        if not min_match:
            raise MissingSectionError(
                "Minimum payment due is required on card statements."
            )
        val_min = min_match.group(1).strip()
        is_min_neg = (
            val_min.startswith("-")
            or (val_min.startswith("(") and val_min.endswith(")"))
            or "$-" in val_min
        )
        if is_min_neg:
            raise InvariantError("minimum_payment_due_cents must be >= 0")
        min_payment_cents = parse_currency_to_cents(val_min, allow_zero=True)

        summary = CreditCardSummary(
            statement_id=extraction.run.run_id,
            payment_due_date=due_date,
            minimum_payment_due_cents=min_payment_cents,
            previous_balance_cents=prev_balance_cents,
            payments_credits_cents=payments_credits_cents,
            purchases_cents=purchases_cents,
            cash_advances_cents=cash_advances_cents,
            balance_transfers_cents=balance_transfers_cents,
            fees_charged_cents=fees_charged_cents,
            interest_charged_cents=interest_charged_cents,
            new_balance_cents=new_balance_cents,
        )
        return prev_balance_cents, new_balance_cents, summary

    def parse_transactions(
        self, extraction: RawExtraction, statement_id: UUID
    ) -> list[CanonicalTransaction]:
        """Parse Chase activity rows above TRANSACTIONS THIS CYCLE total."""
        all_text = "\n".join(p.page_text for p in extraction.pages)
        marker = "TRANSACTIONS THIS CYCLE"
        marker_at = all_text.upper().find(marker)
        if marker_at < 0:
            raise MissingSectionError("Could not locate TRANSACTIONS THIS CYCLE.")

        # Chase prints detail rows first, then the cycle-total footer line.
        header_match = re.search(
            r"Merchant Name or Transaction Description\s*\$?\s*Amount",
            all_text,
            re.IGNORECASE,
        )
        if header_match and header_match.end() < marker_at:
            section_body = all_text[header_match.end() : marker_at]
        else:
            # Fall back: scan a bounded window preceding the cycle total.
            section_body = all_text[max(0, marker_at - 4000) : marker_at]

        _, start_date, end_date = self.parse_header(extraction)
        txns: list[CanonicalTransaction] = []

        for raw_line in section_body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = _TXN_LINE.match(line)
            if not match:
                continue
            raw_date, raw_desc, raw_amt = match.groups()
            post_date = self._parse_txn_date(raw_date, start_date, end_date)
            desc_upper = raw_desc.upper()

            has_explicit_sign = raw_amt.startswith(("+", "-")) or (
                raw_amt.startswith("(") and raw_amt.endswith(")")
            )
            # Chase Amount column: unsigned values are liability increases.
            amount_cents = parse_currency_to_cents(
                raw_amt if has_explicit_sign else f"+{raw_amt}"
            )

            if re.search(r"\bPAYMENT\b", desc_upper):
                category = TransactionCategory.PAYMENT
                expects_balance_increase = False
            elif re.search(r"\b(REFUND|RETURN|CREDIT)\b", desc_upper):
                category = TransactionCategory.CREDIT
                expects_balance_increase = False
            elif re.search(r"\b(FEE REFUND|FEE REVERSAL|WAIVER)\b", desc_upper):
                category = TransactionCategory.FEE_REVERSAL
                expects_balance_increase = False
            elif re.search(r"\bCASH ADVANCE\b", desc_upper):
                category = TransactionCategory.CASH_ADVANCE
                expects_balance_increase = True
            elif re.search(r"\bBALANCE TRANSFER\b", desc_upper):
                category = TransactionCategory.BALANCE_TRANSFER
                expects_balance_increase = True
            elif re.search(r"\bINTEREST\b", desc_upper):
                category = TransactionCategory.INTEREST_CHARGED
                expects_balance_increase = True
            elif re.search(r"\b(FEE|LATE|ANNUAL)\b", desc_upper):
                category = TransactionCategory.FEE
                expects_balance_increase = True
            else:
                category = TransactionCategory.PURCHASE
                expects_balance_increase = True

            glyph_is_increase = amount_cents > 0
            if glyph_is_increase != expects_balance_increase:
                raise TokenError(
                    f"Category {category.value!r} disagrees with printed amount "
                    f"sign for {raw_amt!r}."
                )

            validate_category_for_domain(self.account_domain, category)
            validate_category_sign(self.account_domain, category, amount_cents)

            txns.append(
                CanonicalTransaction(
                    statement_id=statement_id,
                    post_date=post_date,
                    amount_cents=amount_cents,
                    description=raw_desc,
                    transaction_category=category,
                )
            )

        if not txns:
            raise MissingSectionError(
                "TRANSACTIONS THIS CYCLE contained no date/amount rows."
            )
        return txns

    @staticmethod
    def _parse_date(date_str: str) -> datetime.date:
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
            try:
                return datetime.datetime.strptime(date_str, fmt).date()  # noqa: DTZ007
            except ValueError:
                continue
        raise TokenError(f"Invalid date format: {date_str}")

    @classmethod
    def _parse_txn_date(
        cls,
        date_str: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> datetime.date:
        if re.fullmatch(r"\d{1,2}/\d{1,2}", date_str):
            month_s, day_s = date_str.split("/")
            month, day = int(month_s), int(day_s)
            for year in (end_date.year, start_date.year, end_date.year - 1):
                try:
                    candidate = datetime.date(year, month, day)
                except ValueError as exc:
                    raise TokenError(f"Invalid date format in row: {date_str}") from exc
                if start_date <= candidate <= end_date:
                    return candidate
            raise TokenError(
                f"Transaction date {date_str!r} falls outside statement period "
                f"[{start_date.isoformat()}, {end_date.isoformat()}]."
            )
        return cls._parse_date(date_str)
