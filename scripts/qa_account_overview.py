#!/usr/bin/env python3
"""QA: Account overview home (YNAB-useful Vault).

Happy: seeded Chase/Schwab → dashboard load shows high-level integer-cent overview;
       Details drill-in shows transaction ledger.
Negative: empty DB empty-state; unknown statement_id → STATEMENT_NOT_FOUND.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _render_pdf(fixture_name: str, target: Path) -> Path:
    from reportlab.pdfgen import canvas

    data = json.loads((FIXTURES / f"{fixture_name}.json").read_text(encoding="utf-8"))
    lines = data["raw_text"].split("\n")
    c = canvas.Canvas(str(target))
    y = 750
    for line in lines:
        line = re.sub(r"(\t|\s{2,})", " | ", line)
        c.drawString(50, y, line)
        y -= 15
        if y < 50:
            c.showPage()
            y = 750
    c.save()
    return target


def main() -> int:
    print("QA: Account overview home — load aggregates + transaction drill-in")
    failures = 0

    # Destructive truncate — always use the isolated pytest DB, never dogfood.
    if not os.environ.get("TEST_DATABASE_URL"):
        os.environ["TEST_DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire_test"
        )
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]

    import psycopg
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    # Clean slate for empty-state check
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
            TRUNCATE TABLE raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()

    client = TestClient(create_app())
    empty = client.get("/dashboard")
    if (
        empty.status_code == 200
        and "Account overview" in empty.text
        and "No persisted statements yet" in empty.text
    ):
        _pass("empty DB → overview empty state on load")
    else:
        _fail("empty DB did not show overview empty state")
        failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for fixture in ("chase_checking_happy", "brokerage_cash_happy"):
            pdf = _render_pdf(fixture, tmp_path / f"{fixture}.pdf")
            with pdf.open("rb") as handle:
                up = client.post(
                    "/api/v1/dashboard/upload",
                    files={"file": (pdf.name, handle, "application/pdf")},
                    headers={"HX-Request": "true"},
                )
            if up.status_code != 200:
                _fail(f"upload {fixture} failed status={up.status_code}")
                failures += 1

        dash = client.get("/dashboard")
        if (
            dash.status_code == 200
            and "Account overview" in dash.text
            and "Accounts" in dash.text
            and "Liquid cash" in dash.text
            and "Brokerage funds" in dash.text
            and "Liquid" in dash.text
            and "Non-liquid" in dash.text
            and "SYNTHETIC Payroll" not in dash.text
            and re.search(r"\$\d[\d,]*\.\d{2}\b", dash.text)
            and not re.findall(r"\$\d[\d,]*\.\d{3,}", dash.text)
        ):
            _pass("seeded DB → liquid vs brokerage USD overview on load (no txn dump)")
        else:
            _fail("seeded overview missing liquidity split or still shows ledger home")
            failures += 1

        match = re.search(
            r"/api/v1/dashboard/statements/([0-9a-f-]{36})",
            dash.text,
            re.IGNORECASE,
        )
        if match:
            detail = client.get(
                f"/api/v1/dashboard/statements/{match.group(1)}",
                headers={"HX-Request": "true"},
            )
            if detail.status_code == 200 and "Vault Ledger" in detail.text:
                _pass("Details drill-in → Vault Ledger")
            else:
                _fail("drill-in did not return ledger")
                failures += 1
        else:
            _fail("overview missing Details statement link")
            failures += 1

    bad = client.get(
        f"/api/v1/dashboard/statements/{uuid4()}",
        headers={"HX-Request": "true"},
    )
    if bad.status_code == 400 and "STATEMENT_NOT_FOUND" in bad.text:
        _pass("negative: unknown statement fails loud")
    else:
        _fail(f"negative expected 400 STATEMENT_NOT_FOUND, got {bad.status_code}")
        failures += 1

    if failures:
        print(f"\nResult: [FAIL] ({failures} check(s) failed)")
        return 1
    print("\nResult: [PASS] account overview checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
