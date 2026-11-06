#!/usr/bin/env python3
"""QA: YNAB budgets / envelopes (Ready to Assign + assign).

Happy: seed Vault closings + SYNTHETIC envelope → assign $50.00 → balances update.
Negative: over-assign → ASSIGN_EXCEEDS_AVAILABLE; invalid USD → INVALID_AMOUNT.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

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
    print("QA: YNAB budgets / envelopes — Ready to Assign + assign fail-loud")
    failures = 0

    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire"
        )

    import psycopg
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.budget.envelopes import list_envelopes, ready_to_assign_cents
    from app.db.connection import init_db

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            """
            TRUNCATE TABLE envelopes, raw_tokens, raw_pages, transactions, holdings,
                           statement_depository_summaries, statement_credit_summaries,
                           statement_brokerage_summaries, statements, accounts,
                           extraction_runs, raw_payloads CASCADE;
            """
        )
        conn.commit()

    client = TestClient(create_app())

    dash = client.get("/dashboard")
    if dash.status_code == 200 and "Budget" in dash.text and "Overview" in dash.text:
        _pass("dashboard exposes Overview and Budget controls")
    else:
        _fail("dashboard missing Budget / Overview controls")
        failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pdf = _render_pdf("chase_checking_happy", tmp_path / "chase.pdf")
        with pdf.open("rb") as handle:
            up = client.post(
                "/api/v1/dashboard/upload",
                files={"file": (pdf.name, handle, "application/pdf")},
                headers={"HX-Request": "true"},
            )
        if up.status_code != 200:
            _fail(f"upload failed: {up.status_code}")
            print("\nResult: [FAIL]")
            return 1
        _pass("uploaded SYNTHETIC Chase checking statement")

    create = client.post(
        "/api/v1/dashboard/budget/envelopes",
        data={"name": "SYNTHETIC Groceries"},
        headers={"HX-Request": "true"},
    )
    if create.status_code == 200 and "SYNTHETIC Groceries" in create.text:
        _pass("created SYNTHETIC Groceries envelope")
    else:
        _fail("create envelope failed")
        failures += 1

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        available = ready_to_assign_cents(conn)
        envelopes = list_envelopes(conn)
        if type(available) is not int or available <= 0:
            _fail("Ready to Assign not a positive int cents value")
            failures += 1
        else:
            _pass(
                f"Ready to Assign is positive integer cents (len={len(str(available))})"
            )
        if not envelopes:
            _fail("no envelopes after create")
            failures += 1
            print("\nResult: [FAIL]")
            return 1
        envelope_id = str(envelopes[0].envelope_id)

    budget = client.get(
        "/api/v1/dashboard/budget",
        headers={"HX-Request": "true"},
    )
    if (
        budget.status_code == 200
        and "Ready to Assign" in budget.text
        and "SYNTHETIC Groceries" in budget.text
        and re.search(r"\$\d[\d,]*\.\d{2}\b", budget.text)
        and not re.findall(r"\$\d[\d,]*\.\d{3,}", budget.text)
    ):
        _pass("Budget pane shows Ready to Assign + envelope as USD")
    else:
        _fail("Budget pane missing Ready to Assign / USD formatting")
        failures += 1

    assign = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "50.00"},
        headers={"HX-Request": "true"},
    )
    if assign.status_code == 200 and "$50.00" in assign.text:
        _pass("happy assign $50.00 updates Budget pane")
    else:
        _fail(f"happy assign failed: status={assign.status_code}")
        failures += 1

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        after = list_envelopes(conn)[0]
        if after.balance_cents == 5_000 and type(after.balance_cents) is int:
            _pass("storage remains integer cents (5000)")
        else:
            _fail(f"expected balance_cents=5000 int, got {after.balance_cents!r}")
            failures += 1

    assign_whole = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "25"},
        headers={"HX-Request": "true"},
    )
    if assign_whole.status_code == 200 and "$75.00" in assign_whole.text:
        _pass("whole-dollar assign $25 updates Budget pane to $75.00")
    else:
        _fail(f"whole-dollar assign failed: status={assign_whole.status_code}")
        failures += 1

    over = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "999999.00"},
        headers={"HX-Request": "true"},
    )
    if over.status_code == 400 and "ASSIGN_EXCEEDS_AVAILABLE" in over.text:
        _pass("over-assign → ASSIGN_EXCEEDS_AVAILABLE (loud, no clamp)")
    else:
        _fail("over-assign did not fail loud")
        failures += 1

    bad = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "12.345"},
        headers={"HX-Request": "true"},
    )
    if bad.status_code == 400 and "INVALID_AMOUNT" in bad.text:
        _pass("invalid USD token → INVALID_AMOUNT")
    else:
        _fail("invalid USD did not fail loud")
        failures += 1

    if failures:
        print(f"\nResult: [FAIL] ({failures} check(s) failed)")
        return 1
    print("\nResult: [PASS] YNAB budgets / envelopes verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
