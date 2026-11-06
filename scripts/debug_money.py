#!/usr/bin/env python3
"""Money conversion debug CLI — exact cents OK; PII scrubbed from output.

Usage:
  uv run python scripts/debug_money.py parse '$1,295.00'
  uv run python scripts/debug_money.py format 129500
  uv run python scripts/debug_money.py fixture tests/fixtures/chase_checking_happy.json
  uv run python scripts/debug_money.py vault
  uv run python scripts/debug_money.py diff-stage --expected 100 --actual 200 --label net
  uv run python scripts/debug_money.py pipeline tests/fixtures/chase_checking_happy.json

Manual UI QA: not applicable (CLI only).
Never paste raw statement bodies, SSN, DL, or street addresses into cloud chat —
use this CLI's scrubbed stdout instead.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from app.db.connection import DEFAULT_DATABASE_URL
from app.debug.money_trace import (
    scrub_pii,
    trace_diff_stage,
    trace_fixture_or_pdf,
    trace_format,
    trace_parse,
    trace_vault_aggregates,
)


def _cmd_parse(args: argparse.Namespace) -> int:
    step = trace_parse(args.token, allow_zero=args.allow_zero)
    print(step.line())
    return 0 if step.ok else 1


def _cmd_format(args: argparse.Namespace) -> int:
    step = trace_format(args.cents)
    print(step.line())
    return 0 if step.ok else 1


def _cmd_fixture(args: argparse.Namespace) -> int:
    report = trace_fixture_or_pdf(Path(args.path))
    print(report.to_text())
    return 0 if report.ok else 1


def _cmd_vault(args: argparse.Namespace) -> int:
    url = args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    # Refuse accidental pytest DB for human dogfood unless explicitly passed.
    print(f"# database_host={url.split('@')[-1]}")
    report = trace_vault_aggregates(url)
    print(report.to_text())
    return 0 if report.ok else 1


def _cmd_diff_stage(args: argparse.Namespace) -> int:
    step = trace_diff_stage(
        expected=args.expected, actual=args.actual, label=args.label
    )
    print(step.line())
    return 0 if step.ok else 1


def _cmd_pipeline(args: argparse.Namespace) -> int:
    """Fixture extract + reconcile in one report (parse→extract→recon)."""
    path = Path(args.path)
    print(f"# pipeline path={path.name}")
    if args.token:
        parse_step = trace_parse(args.token)
        print(parse_step.line())
        if not parse_step.ok:
            return 1
    report = trace_fixture_or_pdf(path)
    print(report.to_text())
    if report.ok and report.statements:
        stmt = report.statements[0]
        fmt = trace_format(stmt.closing_balance_cents)
        print(fmt.line())
        diff = trace_diff_stage(
            expected=stmt.net_change_cents,
            actual=stmt.txn_sum_cents,
            label="net_vs_txn_sum",
        )
        print(diff.line())
        return 0 if diff.ok and fmt.ok else 1
    return 0 if report.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Trace money conversion stages (integer cents). "
            "Stdout may include amounts; PII is scrubbed."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="Parse a currency token to cents")
    p_parse.add_argument("token", help="Currency token, e.g. '$1,295.00'")
    p_parse.add_argument(
        "--allow-zero",
        action="store_true",
        help="Allow $0.00 (summary balances)",
    )
    p_parse.set_defaults(func=_cmd_parse)

    p_fmt = sub.add_parser("format", help="Format cents as USD and round-trip")
    p_fmt.add_argument("cents", type=int, help="Integer cents")
    p_fmt.set_defaults(func=_cmd_format)

    p_fix = sub.add_parser(
        "fixture", help="Adapter match + extract + reconcile (JSON or PDF)"
    )
    p_fix.add_argument("path", help="Path to fixture .json or statement .pdf")
    p_fix.set_defaults(func=_cmd_fixture)

    p_vault = sub.add_parser(
        "vault", help="Liquid / brokerage / liability / Ready-to-Assign from DB"
    )
    p_vault.add_argument(
        "--database-url",
        default=None,
        help="Postgres URL (default DATABASE_URL or dogfood /sapphire)",
    )
    p_vault.set_defaults(func=_cmd_vault)

    p_diff = sub.add_parser("diff-stage", help="Compare two int-cent values")
    p_diff.add_argument("--expected", type=int, required=True)
    p_diff.add_argument("--actual", type=int, required=True)
    p_diff.add_argument("--label", required=True)
    p_diff.set_defaults(func=_cmd_diff_stage)

    p_pipe = sub.add_parser(
        "pipeline", help="Run fixture extract+recon (+ optional token parse)"
    )
    p_pipe.add_argument("path", help="Fixture .json or .pdf")
    p_pipe.add_argument(
        "--token",
        default=None,
        help="Optional currency token to parse before fixture",
    )
    p_pipe.set_defaults(func=_cmd_pipeline)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] {type(exc).__name__}: {scrub_pii(str(exc))}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
