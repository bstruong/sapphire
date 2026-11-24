"""History series queries for Overview charts (integer cents only).

Closing balances by statement end date; monthly income vs spend from
``transaction_category`` buckets. Fail loud on unknown categories.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import psycopg

from app.models.enums import AccountDomain, TransactionCategory
from app.models.exceptions import PersistenceError, TokenError

logger = logging.getLogger(__name__)

# Personal-finance income vs spend for Overview History bars.
# TRADE_CASH and PAYMENT are excluded (ambiguous / transfer-like).
INCOME_CATEGORIES: frozenset[TransactionCategory] = frozenset(
    {
        TransactionCategory.DEPOSIT,
        TransactionCategory.INTEREST_PAID,
        TransactionCategory.OTHER_CREDIT,
        TransactionCategory.DIVIDEND,
        TransactionCategory.INTEREST,
        TransactionCategory.TRANSFER_IN,
        TransactionCategory.CREDIT,
        TransactionCategory.FEE_REVERSAL,
    }
)

SPEND_CATEGORIES: frozenset[TransactionCategory] = frozenset(
    {
        TransactionCategory.WITHDRAWAL,
        TransactionCategory.PURCHASE,
        TransactionCategory.FEE,
        TransactionCategory.OTHER_DEBIT,
        TransactionCategory.CASH_ADVANCE,
        TransactionCategory.BALANCE_TRANSFER,
        TransactionCategory.INTEREST_CHARGED,
        TransactionCategory.TRANSFER_OUT,
    }
)

_BUCKET_INCOME = "income"
_BUCKET_SPEND = "spend"
_BUCKET_EXCLUDE = "exclude"


@dataclass(frozen=True)
class LiquidBalanceSeries:
    """Liquid (depository) closing total for one statement end date."""

    statement_end_date: date
    liquid_total_cents: int


@dataclass(frozen=True)
class BalancePoint:
    """One account's closing balance at a statement end date."""

    statement_end_date: date
    account_id: UUID
    institution: str
    account_mask: str
    account_domain: AccountDomain
    closing_balance_cents: int


@dataclass(frozen=True)
class AccountBalanceSeries:
    """Ordered closing points for one account (for per-account polylines)."""

    account_id: UUID
    institution: str
    account_mask: str
    points: tuple[BalancePoint, ...]


@dataclass(frozen=True)
class CategoryMonthBucket:
    """Calendar-month income vs spend totals (absolute integer cents)."""

    year_month: str
    income_cents: int
    spend_cents: int


def bucket_for_category(category: str | TransactionCategory) -> str:
    """Return ``income``, ``spend``, or ``exclude`` for a pipeline category.

    Raises:
        TokenError: Unknown category string (fail loud; no silent skip).
    """
    if isinstance(category, TransactionCategory):
        cat = category
    else:
        try:
            cat = TransactionCategory(str(category))
        except ValueError as exc:
            raise TokenError(
                f"Unknown transaction_category for history bucket: {category!r}"
            ) from exc
    if cat in INCOME_CATEGORIES:
        return _BUCKET_INCOME
    if cat in SPEND_CATEGORIES:
        return _BUCKET_SPEND
    if cat in {
        TransactionCategory.PAYMENT,
        TransactionCategory.TRADE_CASH,
    }:
        return _BUCKET_EXCLUDE
    raise TokenError(f"Unmapped transaction_category for history bucket: {cat!r}")


def list_closing_balance_points(conn: psycopg.Connection) -> list[BalancePoint]:
    """All statement closing balances ordered by end date, then account."""
    query = """
    SELECT s.statement_end_date, a.account_id, a.institution, a.account_mask,
           a.account_domain, s.closing_balance_cents
    FROM statements s
    JOIN accounts a ON s.account_id = a.account_id
    ORDER BY s.statement_end_date ASC, a.institution ASC, a.account_mask ASC;
    """
    points: list[BalancePoint] = []
    with conn.cursor() as cur:
        cur.execute(query)
        for row in cur.fetchall():
            closing = row[5]
            if type(closing) is not int:
                raise PersistenceError(
                    f"closing_balance_cents must be int, got {type(closing).__name__}"
                )
            points.append(
                BalancePoint(
                    statement_end_date=row[0],
                    account_id=UUID(str(row[1])),
                    institution=str(row[2]),
                    account_mask=str(row[3]),
                    account_domain=AccountDomain(str(row[4])),
                    closing_balance_cents=closing,
                )
            )
    logger.info(
        "Loaded closing balance history points",
        extra={"point_count": len(points)},
    )
    return points


def list_liquid_closing_by_period(
    conn: psycopg.Connection,
) -> list[LiquidBalanceSeries]:
    """Sum depository closing balances grouped by statement_end_date."""
    query = """
    SELECT s.statement_end_date, SUM(s.closing_balance_cents)::bigint
    FROM statements s
    JOIN accounts a ON s.account_id = a.account_id
    WHERE a.account_domain = %s
    GROUP BY s.statement_end_date
    ORDER BY s.statement_end_date ASC;
    """
    series: list[LiquidBalanceSeries] = []
    with conn.cursor() as cur:
        cur.execute(query, (AccountDomain.DEPOSITORY.value,))
        for row in cur.fetchall():
            total = int(row[1])
            if type(total) is not int:
                raise PersistenceError(
                    f"liquid_total_cents must be int, got {type(total).__name__}"
                )
            series.append(
                LiquidBalanceSeries(
                    statement_end_date=row[0],
                    liquid_total_cents=total,
                )
            )
    logger.info(
        "Loaded liquid closing balance series",
        extra={"period_count": len(series)},
    )
    return series


def list_account_balance_series(
    conn: psycopg.Connection,
    *,
    domain: AccountDomain = AccountDomain.DEPOSITORY,
) -> list[AccountBalanceSeries]:
    """Per-account closing series for one domain (default: liquid)."""
    points = [
        p for p in list_closing_balance_points(conn) if p.account_domain == domain
    ]
    by_account: dict[UUID, list[BalancePoint]] = {}
    meta: dict[UUID, tuple[str, str]] = {}
    for point in points:
        by_account.setdefault(point.account_id, []).append(point)
        meta[point.account_id] = (point.institution, point.account_mask)
    result: list[AccountBalanceSeries] = []
    for account_id, acct_points in by_account.items():
        institution, mask = meta[account_id]
        result.append(
            AccountBalanceSeries(
                account_id=account_id,
                institution=institution,
                account_mask=mask,
                points=tuple(acct_points),
            )
        )
    result.sort(key=lambda s: (s.institution, s.account_mask))
    return result


def list_category_month_totals(
    conn: psycopg.Connection,
) -> list[CategoryMonthBucket]:
    """Monthly absolute sums of income vs spend transaction categories."""
    query = """
    SELECT to_char(t.post_date, 'YYYY-MM') AS year_month,
           t.transaction_category,
           SUM(ABS(t.amount_cents))::bigint AS abs_sum_cents
    FROM transactions t
    GROUP BY year_month, t.transaction_category
    ORDER BY year_month ASC, t.transaction_category ASC;
    """
    by_month: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        cur.execute(query)
        for row in cur.fetchall():
            year_month = str(row[0])
            category = str(row[1])
            abs_sum = int(row[2])
            if type(abs_sum) is not int:
                raise PersistenceError(
                    f"category abs_sum_cents must be int, got {type(abs_sum).__name__}"
                )
            bucket = bucket_for_category(category)
            if bucket == _BUCKET_EXCLUDE:
                continue
            slot = by_month.setdefault(
                year_month, {_BUCKET_INCOME: 0, _BUCKET_SPEND: 0}
            )
            slot[bucket] += abs_sum

    months: list[CategoryMonthBucket] = []
    for year_month in sorted(by_month):
        slot = by_month[year_month]
        income = slot[_BUCKET_INCOME]
        spend = slot[_BUCKET_SPEND]
        if income == 0 and spend == 0:
            continue
        months.append(
            CategoryMonthBucket(
                year_month=year_month,
                income_cents=income,
                spend_cents=spend,
            )
        )
    logger.info(
        "Loaded category month history",
        extra={"month_count": len(months)},
    )
    return months
