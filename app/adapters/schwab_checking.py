"""Charles Schwab Bank Investor Checking (depository) adapter."""

from __future__ import annotations

import datetime
import logging
import re
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

from app.adapters.depository import StandardDepositoryAdapter
from app.extraction.core import parse_currency_to_cents
from app.models.canonical import CanonicalTransaction, DepositorySummary
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

_SUMMARY_MONEY = r"(\(?\$?-?[\d,]+\.\d{2}\)?|\+\$[\d,]+\.\d{2})"
_TXN_TWO_MONEY = re.compile(
    r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(.+?)\s+"
    r"(\$?[\d,]+\.\d{2})\s+(\$?[\d,]+\.\d{2})\s*$"
)
_TXN_ONE_MONEY = re.compile(
    r"^(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+(.+?)\s+(\$?[\d,]+\.\d{2})\s*$"
)


def _require_non_negative(field_name: str, cents: int) -> int:
    if cents < 0:
        raise InvariantError(f"{field_name} must be >= 0")
    return cents


class SchwabCheckingAdapter(StandardDepositoryAdapter):
    """Schwab Bank Investor Checking statements (depository domain).

    Matches page-1 Schwab Bank chrome exclusively (mutual exclusion with
    ``schwab_brokerage``). Summary uses Beginning/Ending Balance labels.
    Activity uses Posted/Debits/Credits/Balance; polarity comes from the
    running-balance delta (not description text).
    """

    adapter_id = "schwab_checking"
    adapter_version = "1.0.0"
    institution_id = "Charles Schwab Bank"
    account_types = frozenset({AccountType.CHECKING})

    def matches(self, file_path: Path) -> bool:
        """Heuristic check on first page text for Schwab Bank checking."""
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
                # Brokerage packets belong to SchwabAdapter exclusively.
                if (
                    "TRANSACTION ACTIVITY" in first_text
                    or "BROKERAGE CASH" in first_text
                ):
                    return False
                if "POSITIONS" in first_text and "BROKERAGE" in first_text:
                    return False

                bank_brand = (
                    "SCHWAB BANK" in first_text
                    or "CHARLES SCHWAB BANK" in first_text
                    or ("CHARLES SCHWAB" in first_text and "MEMBER FDIC" in first_text)
                    or (
                        "SCHWAB" in first_text
                        and "DEBIT CARD" in first_text
                        and "MEMBER FDIC" in first_text
                    )
                )
                return bank_brand
        except Exception:  # noqa: BLE001
            return False

    def declared_schemas(self) -> Sequence[TableSchema]:
        """Schwab Bank section anchors present in raw and redacted extracts."""
        return (
            TableSchema(
                name="depository_transactions",
                headers=("date", "description", "amount", "balance"),
                section_markers=("Beginning Balance", "Ending Balance"),
            ),
        )

    def parse_header(
        self, extraction: RawExtraction
    ) -> tuple[str, AccountType, datetime.date, datetime.date]:
        """Extract account mask, checking type, and statement period."""
        logger.info(
            "Parsing Schwab checking header",
            extra={"run_id": str(extraction.run.run_id)},
        )
        all_text = "\n".join(p.page_text for p in extraction.pages)

        account_matches = re.findall(
            r"Account Number[:\s]+(\[[A-Z_]+_\d+\]|[*\dX-]+)",
            all_text,
            re.IGNORECASE,
        )
        if not account_matches:
            # Header row often pairs labels; token may sit on the next line.
            account_matches = re.findall(
                r"Account Number.*?(\[[A-Z_]+_\d+\])",
                all_text,
                re.IGNORECASE | re.DOTALL,
            )
        unique_masks = sorted(set(account_matches))
        if len(unique_masks) > 1:
            raise AmbiguousAccountsError(
                f"Multiple account masks detected: {unique_masks}"
            )
        if not unique_masks:
            raise MissingSectionError("Could not locate account number on statement.")

        start_date, end_date = self._parse_statement_period(all_text)
        return unique_masks[0], AccountType.CHECKING, start_date, end_date

    def parse_summary(
        self, extraction: RawExtraction
    ) -> tuple[int, int, DepositorySummary]:
        """Extract Schwab Bank summary buckets."""
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

        opening_cents = extract_signed_cents("Beginning Balance")
        deposits_raw = extract_signed_cents("Deposits and Credits")
        deposits_cents = _require_non_negative("deposits_cents", abs(deposits_raw))
        interest_paid_cents = _require_non_negative(
            "interest_paid_cents", abs(extract_signed_cents("Interest Paid"))
        )
        withdrawals_raw = extract_signed_cents("Withdrawals and Other Debits")
        withdrawals_cents = _require_non_negative(
            "withdrawals_cents", abs(withdrawals_raw)
        )
        fees_cents = _require_non_negative(
            "fees_cents", abs(extract_signed_cents("Other Fees"))
        )
        closing_cents = extract_signed_cents("Ending Balance")

        summary = DepositorySummary(
            statement_id=extraction.run.run_id,
            deposits_cents=deposits_cents,
            withdrawals_cents=withdrawals_cents,
            interest_paid_cents=interest_paid_cents,
            fees_cents=fees_cents,
        )
        return opening_cents, closing_cents, summary

    def parse_transactions(
        self, extraction: RawExtraction, statement_id: UUID
    ) -> list[CanonicalTransaction]:
        """Parse Debits/Credits activity using running-balance polarity."""
        all_text = "\n".join(p.page_text for p in extraction.pages)
        header = re.search(
            r"Posted\s+Description\s+Debits\s+Credits\s+Balance",
            all_text,
            re.IGNORECASE,
        )
        if not header:
            raise MissingSectionError(
                "Could not locate Posted Description Debits Credits Balance header."
            )

        # Activity ends before interest-rate / disclosure chrome when present.
        tail = all_text[header.end() :]
        end = re.search(
            r"\n(?:Interest Rates|Year-to-Date Totals|IMPORTANT INFORMATION|\Z)",
            tail,
            re.IGNORECASE,
        )
        section_body = tail[: end.start()] if end else tail

        _, _, start_date, end_date = self.parse_header(extraction)
        opening_cents, _, _ = self.parse_summary(extraction)
        running = opening_cents
        txns: list[CanonicalTransaction] = []

        for raw_line in section_body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            up = line.upper()
            if up.startswith(("BEGINNING BALANCE", "ENDING BALANCE")):
                # Opening echo may be `MM/DD Beginning Balance $X`
                one = _TXN_ONE_MONEY.match(line)
                if one and "BEGINNING BALANCE" in up:
                    running = parse_currency_to_cents(one.group(3), allow_zero=True)
                continue
            if up.startswith("POSTED"):
                continue

            two = _TXN_TWO_MONEY.match(line)
            one = None if two else _TXN_ONE_MONEY.match(line)
            if not two and not one:
                continue

            if two:
                raw_date, raw_desc, raw_a, raw_b = two.groups()
                mag_cents = parse_currency_to_cents(f"+{raw_a.lstrip('+')}")
                balance_after = parse_currency_to_cents(raw_b, allow_zero=True)
            else:
                assert one is not None
                raw_date, raw_desc, raw_a = one.groups()
                # Single trailing money on a dated row is treated as balance-only echo.
                if "BALANCE" in raw_desc.upper():
                    running = parse_currency_to_cents(raw_a, allow_zero=True)
                    continue
                raise TokenError(
                    f"Schwab activity row missing amount/balance pair (got {line!r})."
                )

            post_date = self._parse_txn_date(raw_date, start_date, end_date)

            if balance_after == running - mag_cents:
                amount_cents = -mag_cents
            elif balance_after == running + mag_cents:
                amount_cents = mag_cents
            else:
                raise TokenError(
                    "Activity amount does not reconcile to running balance "
                    f"(prior={running}, amount={mag_cents}, after={balance_after})."
                )

            desc_upper = raw_desc.upper()
            if amount_cents > 0:
                if "INTEREST" in desc_upper:
                    category = TransactionCategory.INTEREST_PAID
                elif "FEE REFUND" in desc_upper or "REVERSAL" in desc_upper:
                    category = TransactionCategory.FEE_REVERSAL
                elif "DEPOSIT" in desc_upper or "CREDIT" in desc_upper:
                    category = TransactionCategory.DEPOSIT
                else:
                    category = TransactionCategory.OTHER_CREDIT
            else:
                if "FEE" in desc_upper:
                    category = TransactionCategory.FEE
                elif (
                    "WITHDRAWAL" in desc_upper
                    or "ATM" in desc_upper
                    or "CHECK" in desc_upper
                    or "PURCHASE" in desc_upper
                    or "AUTOPAY" in desc_upper
                ):
                    category = TransactionCategory.WITHDRAWAL
                else:
                    category = TransactionCategory.OTHER_DEBIT

            validate_category_for_domain(self.account_domain, category)
            validate_category_sign(self.account_domain, category, amount_cents)

            txns.append(
                CanonicalTransaction(
                    statement_id=statement_id,
                    post_date=post_date,
                    amount_cents=amount_cents,
                    description=raw_desc,
                    transaction_category=category,
                    balance_after_cents=balance_after,
                )
            )
            running = balance_after

        if not txns:
            raise MissingSectionError("Schwab checking activity contained no rows.")
        return txns

    _MONTH_NAMES = (
        "January|February|March|April|May|June|July|August|"
        "September|October|November|December"
    )

    @classmethod
    def _parse_statement_period(
        cls, all_text: str
    ) -> tuple[datetime.date, datetime.date]:
        """Parse Schwab Bank statement period chrome.

        Accepts:
        - ``08/01/2026 - 08/31/2026`` (slash range; redacted fixtures)
        - ``August 1-31, 2026`` (compact month range; live Bank PDFs)
        - ``August 1 - 31, 2026`` (spaced compact range)
        - ``August 1, 2026 through August 31, 2026`` (full month names)
        """
        slash = re.search(
            r"(\d{1,2}/\d{1,2}/\d{2,4})\s*(?:to|-|through|–|—)\s*"
            r"(\d{1,2}/\d{1,2}/\d{2,4})",
            all_text,
            re.IGNORECASE,
        )
        if slash:
            return cls._parse_date(slash.group(1)), cls._parse_date(slash.group(2))

        compact = re.search(
            rf"({cls._MONTH_NAMES})\s+(\d{{1,2}})\s*[-–—]\s*(\d{{1,2}}),\s*(\d{{4}})",
            all_text,
            re.IGNORECASE,
        )
        if compact:
            month_name, day_start, day_end, year_s = compact.groups()
            year = int(year_s)
            month = datetime.datetime.strptime(month_name.title(), "%B").month  # noqa: DTZ007
            try:
                start_date = datetime.date(year, month, int(day_start))
                end_date = datetime.date(year, month, int(day_end))
            except ValueError as exc:
                raise TokenError(
                    f"Invalid compact statement period: {compact.group(0)!r}"
                ) from exc
            return start_date, end_date

        full = re.search(
            rf"({cls._MONTH_NAMES})\s+(\d{{1,2}}),?\s+(\d{{4}})\s*"
            rf"(?:to|through|-|–|—)\s*"
            rf"({cls._MONTH_NAMES})\s+(\d{{1,2}}),?\s+(\d{{4}})",
            all_text,
            re.IGNORECASE,
        )
        if full:
            m1, d1, y1, m2, d2, y2 = full.groups()
            start_date = datetime.date(
                int(y1),
                datetime.datetime.strptime(m1.title(), "%B").month,  # noqa: DTZ007
                int(d1),
            )
            end_date = datetime.date(
                int(y2),
                datetime.datetime.strptime(m2.title(), "%B").month,  # noqa: DTZ007
                int(d2),
            )
            return start_date, end_date

        raise MissingSectionError("Could not locate statement period dates.")

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
