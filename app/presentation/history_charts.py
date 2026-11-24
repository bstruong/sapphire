"""Server-rendered SVG for Overview History charts.

Integer-cent inputs only. Coordinates use integer arithmetic (no float
money math). CSS classes carry stroke/fill — no inline ``style=``.
"""

from __future__ import annotations

from html import escape

from app.db.history import (
    AccountBalanceSeries,
    CategoryMonthBucket,
    LiquidBalanceSeries,
)

_VIEW_W = 320
_VIEW_H = 100
_PAD_X = 20
_PAD_TOP = 12
_PAD_BOTTOM = 22


def _require_int_cents(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be int cents, got {type(value).__name__}")
    return value


def _scale_y(value: int, max_value: int) -> int:
    """Map cents onto SVG y (larger cents → higher on chart / smaller y)."""
    usable = _VIEW_H - _PAD_TOP - _PAD_BOTTOM
    if max_value <= 0:
        return _VIEW_H - _PAD_BOTTOM
    # Keep value in [0, max_value] for plotting height.
    clamped = min(max(value, 0), max_value)
    return _PAD_TOP + usable * (max_value - clamped) // max_value


def _scale_x(index: int, count: int) -> int:
    usable = _VIEW_W - 2 * _PAD_X
    if count <= 1:
        return _PAD_X + usable // 2
    return _PAD_X + usable * index // (count - 1)


def _month_label(year_month_or_date: str) -> str:
    """Short month label from ``YYYY-MM`` or ISO date prefix."""
    parts = year_month_or_date.split("-")
    if len(parts) < 2:
        return year_month_or_date
    month_num = int(parts[1])
    names = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )
    if month_num < 1 or month_num > 12:
        return year_month_or_date
    return names[month_num - 1]


def render_balance_history_svg(
    liquid_series: list[LiquidBalanceSeries],
    *,
    account_series: list[AccountBalanceSeries],
) -> str:
    """Polyline SVG for liquid total (+ optional per-account dashed lines)."""
    if len(liquid_series) < 2:
        raise ValueError("balance history SVG requires at least 2 periods")

    totals = [
        _require_int_cents(p.liquid_total_cents, "liquid_total_cents")
        for p in liquid_series
    ]
    max_cents = max(totals)
    for series in account_series:
        for point in series.points:
            cents = _require_int_cents(
                point.closing_balance_cents, "closing_balance_cents"
            )
            max_cents = max(max_cents, cents)

    n = len(liquid_series)
    liquid_points: list[str] = []
    for i, point in enumerate(liquid_series):
        x = _scale_x(i, n)
        y = _scale_y(point.liquid_total_cents, max_cents)
        liquid_points.append(f"{x},{y}")

    parts: list[str] = [
        (
            f'<svg class="history-chart" viewBox="0 0 {_VIEW_W} {_VIEW_H}" '
            'role="img" aria-label="Liquid closing balances by statement end">'
        ),
        (
            f'<polyline class="history-line-liquid" '
            f'points="{" ".join(liquid_points)}" />'
        ),
    ]

    # Align each account series to liquid period dates when present.
    end_dates = [p.statement_end_date for p in liquid_series]
    date_index = {d: i for i, d in enumerate(end_dates)}
    for series in account_series:
        coords: list[str] = []
        for point in series.points:
            idx = date_index.get(point.statement_end_date)
            if idx is None:
                continue
            x = _scale_x(idx, n)
            y = _scale_y(point.closing_balance_cents, max_cents)
            coords.append(f"{x},{y}")
        if len(coords) >= 2:
            label = escape(f"{series.institution} {series.account_mask}")
            parts.append(
                f'<polyline class="history-line-account" '
                f'points="{" ".join(coords)}" '
                f'aria-label="{label}" />'
            )

    # X labels: first, middle, last
    label_indexes = sorted({0, (n - 1) // 2, n - 1})
    for idx in label_indexes:
        end = liquid_series[idx].statement_end_date
        label = _month_label(end.isoformat())
        x = _scale_x(idx, n)
        parts.append(
            f'<text class="history-axis-label" x="{x}" y="{_VIEW_H - 6}">'
            f"{escape(label)}</text>"
        )

    parts.append("</svg>")
    return "".join(parts)


def render_category_history_svg(months: list[CategoryMonthBucket]) -> str:
    """Grouped income/spend bar SVG by calendar month."""
    if not months:
        raise ValueError("category history SVG requires at least one month")

    for month in months:
        _require_int_cents(month.income_cents, "income_cents")
        _require_int_cents(month.spend_cents, "spend_cents")

    max_cents = max(max(m.income_cents, m.spend_cents) for m in months)
    if max_cents <= 0:
        max_cents = 1

    n = len(months)
    group_gap = 12
    usable = _VIEW_W - 2 * _PAD_X
    group_w = usable // n if n else usable
    bar_w = max(8, (group_w - group_gap) // 2)

    parts: list[str] = [
        (
            f'<svg class="history-chart" viewBox="0 0 {_VIEW_W} {_VIEW_H}" '
            'role="img" aria-label="Category income and spend by month">'
        ),
    ]
    baseline = _VIEW_H - _PAD_BOTTOM
    for i, month in enumerate(months):
        group_left = _PAD_X + i * group_w
        income_h = (
            0
            if month.income_cents <= 0
            else (_VIEW_H - _PAD_TOP - _PAD_BOTTOM) * month.income_cents // max_cents
        )
        spend_h = (
            0
            if month.spend_cents <= 0
            else (_VIEW_H - _PAD_TOP - _PAD_BOTTOM) * month.spend_cents // max_cents
        )
        income_y = baseline - income_h
        spend_y = baseline - spend_h
        income_x = group_left + 4
        spend_x = income_x + bar_w + 2
        parts.append(
            f'<rect class="history-bar-income" x="{income_x}" y="{income_y}" '
            f'width="{bar_w}" height="{income_h}" />'
        )
        parts.append(
            f'<rect class="history-bar-spend" x="{spend_x}" y="{spend_y}" '
            f'width="{bar_w}" height="{spend_h}" />'
        )
        label = _month_label(month.year_month)
        label_x = group_left + group_w // 2
        parts.append(
            f'<text class="history-axis-label" x="{label_x}" y="{_VIEW_H - 6}" '
            f'text-anchor="middle">{escape(label)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)
