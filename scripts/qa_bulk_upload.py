#!/usr/bin/env python3
"""QA: Multi-file bulk statement upload (dashboard HTMX Slice 1).

Verifies:
1. Statements form accepts multiple PDFs (name=files, multiple attribute).
2. Happy batch: two fixture PDFs → Uploaded 2 of 2 with per-file OK rows.
3. Mixed batch: one good + one unmatched → partial success, FAIL-loud code.
4. Over-limit / empty request → loud 400 before any ingest.
5. Single-file upload still returns the Vault Ledger (demo continuity).
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
HX = {"HX-Request": "true"}


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


def _unknown_pdf(target: Path) -> Path:
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(target))
    c.drawString(50, 750, "UNKNOWN CREDIT UNION")
    c.drawString(50, 735, "CHECKING")
    c.save()
    return target


def main() -> int:
    print("=" * 70)
    print("QA: Bulk statement upload (dashboard HTMX, partial success)")
    print("=" * 70)
    failures = 0

    # Prefer isolated pytest DB — this script TRUNCATEs and must not wipe dogfood.
    if os.environ.get("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    elif not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire_test"
        )
    if not os.environ["DATABASE_URL"].rstrip("/").endswith(("_test", "_pytest")):
        _fail(
            "Refusing to TRUNCATE a non-test database "
            f"(got {os.environ['DATABASE_URL'].rsplit('/', 1)[-1]!r})"
        )
        print("\nResult: [FAIL]")
        return 1

    import psycopg
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db
    from app.pipeline.ingest_service import MAX_BATCH_FILES

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
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

    print("\n[Step 1: Statements form accepts multiple files]")
    resp = client.get("/api/v1/dashboard/statements", headers=HX)
    if (
        resp.status_code == 200
        and 'name="files"' in resp.text
        and "multiple" in resp.text
    ):
        _pass("Statements form has name=files and multiple")
    else:
        _fail("Statements form missing multiple/files")
        failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        chase = _render_pdf("chase_checking_happy", tmp_path / "chase.pdf")
        schwab = _render_pdf("brokerage_cash_happy", tmp_path / "schwab.pdf")
        unknown = _unknown_pdf(tmp_path / "unknown.pdf")

        print("\n[Step 2: Happy batch — two PDFs]")
        with chase.open("rb") as h1, schwab.open("rb") as h2:
            resp = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", ("chase.pdf", h1, "application/pdf")),
                    ("files", ("schwab.pdf", h2, "application/pdf")),
                ],
                headers=HX,
            )
        if (
            resp.status_code == 200
            and "Uploaded 2 of 2" in resp.text
            and "View Overview" in resp.text
            and 'id="overview-history"' in resp.text
        ):
            _pass("Dashboard bulk upload: Uploaded 2 of 2 + History refresh")
        else:
            _fail(f"Happy batch unexpected: status={resp.status_code}")
            failures += 1

        print("\n[Step 3: Mixed batch — partial success]")
        with chase.open("rb") as h1, unknown.open("rb") as h2:
            resp = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", ("chase.pdf", h1, "application/pdf")),
                    ("files", ("unknown.pdf", h2, "application/pdf")),
                ],
                headers=HX,
            )
        if (
            resp.status_code == 200
            and "Uploaded 1 of 2" in resp.text
            and "ADAPTER_MATCH_FAILED" in resp.text
            and "View Overview" in resp.text
        ):
            _pass("Mixed batch: 1 OK + ADAPTER_MATCH_FAILED + Overview CTA")
        else:
            _fail(f"Mixed batch unexpected: status={resp.status_code}")
            failures += 1

        print("\n[Step 4: Empty + over-limit rejection]")
        empty = client.post("/api/v1/dashboard/upload", headers=HX)
        if empty.status_code == 400 and "MISSING_FILE" in empty.text:
            _pass("Empty upload → MISSING_FILE")
        else:
            _fail("Empty upload did not fail loud")
            failures += 1

        over_paths = []
        for i in range(MAX_BATCH_FILES + 1):
            p = tmp_path / f"over_{i}.pdf"
            p.write_bytes(b"%PDF-1.4 minimal")
            over_paths.append(p)
        handles = [p.open("rb") for p in over_paths]
        try:
            over = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", (p.name, h, "application/pdf"))
                    for p, h in zip(over_paths, handles, strict=True)
                ],
                headers=HX,
            )
        finally:
            for h in handles:
                h.close()
        if over.status_code == 400 and "TOO_MANY_FILES" in over.text:
            _pass(f"Over-limit (>{MAX_BATCH_FILES}) → TOO_MANY_FILES")
        else:
            _fail("Over-limit did not fail loud")
            failures += 1

        print("\n[Step 5: Single-file ledger continuity]")
        with chase.open("rb") as handle:
            single = client.post(
                "/api/v1/dashboard/upload",
                files=[("files", ("chase.pdf", handle, "application/pdf"))],
                headers=HX,
            )
        if single.status_code == 200 and "Vault Ledger" in single.text:
            _pass("Single-file upload still returns Vault Ledger")
        else:
            _fail(f"Single-file unexpected: status={single.status_code}")
            failures += 1

    print("\n" + ("=" * 70))
    if failures:
        print(f"Result: [FAIL] ({failures} check(s) failed)")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
