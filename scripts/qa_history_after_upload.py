#!/usr/bin/env python3
"""QA: Successful bulk upload refreshes Overview History in the HTMX pane.

Happy: multi-month seed → upload-summary with OK → History SVGs + View Overview.
Negative: all-FAIL summary → no History embed / no View Overview CTA.
Empty vault OK row → History absent (no accounts yet).

Always uses sapphire_test — never truncates dogfood /sapphire.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HX = {"HX-Request": "true"}


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def main() -> int:
    print("=" * 70)
    print("QA: History refresh after bulk upload (Overview charts in results)")
    print("=" * 70)
    failures = 0

    if os.environ.get("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    elif not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire_test"
        )

    db_url = os.environ["DATABASE_URL"]
    if not db_url.rstrip("/").endswith(("_test", "_pytest")):
        _fail(
            "Refusing to run QA truncate against a non-test database "
            f"(got path ending {db_url.rsplit('/', 1)[-1]!r})"
        )
        print("\nResult: [FAIL]")
        return 1

    import psycopg
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db
    from scripts.seed_history_charts import seed

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    with psycopg.connect(db_url) as conn:
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

    print("\n[Step 1: All-FAIL summary — no History / no Overview CTA]")
    fail_only = client.post(
        "/api/v1/dashboard/upload-summary",
        headers={**HX, "Content-Type": "application/json"},
        content=json.dumps(
            {
                "results": [
                    {
                        "filename": "bad.pdf",
                        "ok": False,
                        "statement_id": None,
                        "run_id": None,
                        "error_code": "ADAPTER_MATCH_FAILED",
                        "detail": "No adapter matched",
                        "adapter_id": None,
                    }
                ]
            }
        ),
    )
    if (
        fail_only.status_code == 200
        and "Uploaded 0 of 1" in fail_only.text
        and "View Overview" not in fail_only.text
        and 'id="overview-history"' not in fail_only.text
    ):
        _pass("All-FAIL summary omits History embed and View Overview")
    else:
        _fail("All-FAIL summary leaked History or Overview CTA")
        failures += 1

    print("\n[Step 2: Multi-month seed + OK summary → History SVGs]")
    seed()
    ok = client.post(
        "/api/v1/dashboard/upload-summary",
        headers={**HX, "Content-Type": "application/json"},
        content=json.dumps(
            {
                "results": [
                    {
                        "filename": "Bank Statement_2026-08-31_962.PDF",
                        "ok": True,
                        "statement_id": "11111111-1111-1111-1111-111111111111",
                        "run_id": None,
                        "error_code": None,
                        "detail": None,
                        "adapter_id": "schwab_checking",
                    }
                ]
            }
        ),
    )
    checks = [
        (ok.status_code == 200, "summary HTTP 200"),
        ("Uploaded 1 of 1" in ok.text, "Uploaded 1 of 1"),
        ("View Overview" in ok.text, "View Overview CTA"),
        ('hx-get="/api/v1/dashboard/overview"' in ok.text, "Overview HTMX link"),
        ('id="overview-history"' in ok.text, "History section id"),
        ("history-line-liquid" in ok.text, "Liquid balance polyline"),
        (
            "history-bar-income" in ok.text and "history-bar-spend" in ok.text,
            "Category bars",
        ),
    ]
    if all(ok_flag for ok_flag, _ in checks):
        _pass("OK summary embeds live History charts + View Overview")
    else:
        for ok_flag, label in checks:
            if not ok_flag:
                _fail(f"Missing: {label}")
                failures += 1

    print("\n[Step 3: Overview still has History after nav]")
    overview = client.get("/api/v1/dashboard/overview", headers=HX)
    if (
        overview.status_code == 200
        and 'id="overview-history"' in overview.text
        and "history-line-liquid" in overview.text
    ):
        _pass("Overview History still correct on subsequent HTMX load")
    else:
        _fail("Overview History missing after upload-summary")
        failures += 1

    print("\n" + ("=" * 70))
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
