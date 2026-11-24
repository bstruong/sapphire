#!/usr/bin/env python3
"""QA: Statement upload progress bar (determinate N-of-M / advancing single-file).

Verifies:
1. Statements form exposes a real progressbar (not a spinner) as hx-indicator.
2. CSS defines track + fill + advancing keyframes (Vault tone).
3. upload-item returns JSON per-file outcomes (success + fail-loud).
4. upload-summary renders batch HTML from collected results.
5. Existing single-file ledger upload and multi-file bulk HTML still work.
6. Shell script wires sequential bulk progress (upload-item + upload-summary).
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
    print("QA: Upload progress bar (N of M bulk · advancing single-file)")
    print("=" * 70)
    failures = 0

    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire"
        )

    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.db.connection import init_db

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    client = TestClient(create_app())

    print("\n[Step 1: Progress markup + CSS]")
    stmt = client.get("/api/v1/dashboard/statements", headers=HX)
    if (
        stmt.status_code == 200
        and 'id="upload-progress"' in stmt.text
        and 'role="progressbar"' in stmt.text
        and 'hx-indicator="#upload-progress"' in stmt.text
        and 'hx-indicator="#pane-vault-body"' not in stmt.text
        and "spinner" not in stmt.text.lower()
        and "style=" not in stmt.text
    ):
        _pass("Statements form: progressbar indicator (not spinner / pane dim)")
    else:
        _fail("Statements form missing progress bar markup")
        failures += 1

    css = client.get("/static/sapphire.css").text
    if (
        ".upload-progress-track" in css
        and ".upload-progress-fill" in css
        and "upload-progress-advance" in css
    ):
        _pass("CSS: track + fill + advancing keyframes")
    else:
        _fail("CSS missing progress bar rules")
        failures += 1

    shell = client.get("/dashboard").text
    if (
        "upload-item" in shell
        and "upload-summary" in shell
        and "Uploading " in shell
        and " of " in shell
    ):
        _pass("Shell JS: sequential bulk N-of-M via upload-item + upload-summary")
    else:
        _fail("Shell missing sequential progress wiring")
        failures += 1

    print("\n[Step 2: upload-item JSON happy + fail-loud]")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        good = _render_pdf("chase_checking_happy", tmp_path / "chase.pdf")
        bad = _unknown_pdf(tmp_path / "unknown.pdf")

        with good.open("rb") as handle:
            ok_resp = client.post(
                "/api/v1/dashboard/upload-item",
                files={"files": ("chase.pdf", handle, "application/pdf")},
                headers=HX,
            )
        if ok_resp.status_code == 200 and ok_resp.json().get("ok") is True:
            _pass("upload-item success → ok=true + statement_id")
        else:
            _fail(f"upload-item success failed: {ok_resp.status_code}")
            failures += 1

        with bad.open("rb") as handle:
            bad_resp = client.post(
                "/api/v1/dashboard/upload-item",
                files={"files": ("unknown.pdf", handle, "application/pdf")},
                headers=HX,
            )
        body = bad_resp.json() if bad_resp.status_code == 200 else {}
        if (
            bad_resp.status_code == 200
            and body.get("ok") is False
            and body.get("error_code") == "ADAPTER_MATCH_FAILED"
        ):
            _pass("upload-item fail-loud → ADAPTER_MATCH_FAILED")
        else:
            _fail("upload-item did not fail loud on unmatched PDF")
            failures += 1

        empty = client.post("/api/v1/dashboard/upload-item", headers=HX)
        if (
            empty.status_code == 400
            and empty.json().get("error_code") == "MISSING_FILE"
        ):
            _pass("upload-item empty → MISSING_FILE")
        else:
            _fail("upload-item empty did not fail loud")
            failures += 1

        print("\n[Step 3: upload-summary + legacy upload paths]")
        summary = client.post(
            "/api/v1/dashboard/upload-summary",
            headers={**HX, "Content-Type": "application/json"},
            content=json.dumps(
                {
                    "results": [
                        {
                            "filename": "chase.pdf",
                            "ok": True,
                            "statement_id": ok_resp.json().get("statement_id"),
                            "run_id": ok_resp.json().get("run_id"),
                            "error_code": None,
                            "detail": None,
                            "adapter_id": ok_resp.json().get("adapter_id"),
                        },
                        {
                            "filename": "unknown.pdf",
                            "ok": False,
                            "statement_id": None,
                            "run_id": None,
                            "error_code": "ADAPTER_MATCH_FAILED",
                            "detail": "No adapter matched",
                            "adapter_id": None,
                        },
                    ]
                }
            ),
        )
        if (
            summary.status_code == 200
            and "Uploaded 1 of 2" in summary.text
            and "ADAPTER_MATCH_FAILED" in summary.text
            and "style=" not in summary.text
        ):
            _pass("upload-summary → Uploaded 1 of 2 with fail-loud row")
        else:
            _fail("upload-summary render failed")
            failures += 1

        with good.open("rb") as handle:
            single = client.post(
                "/api/v1/dashboard/upload",
                files={"files": ("chase.pdf", handle, "application/pdf")},
                headers=HX,
            )
        if single.status_code == 200 and "Vault Ledger" in single.text:
            _pass("Single-file upload still returns Vault Ledger")
        else:
            _fail("Single-file ledger path broken")
            failures += 1

        schwab = _render_pdf("brokerage_cash_happy", tmp_path / "schwab.pdf")
        with good.open("rb") as h1, schwab.open("rb") as h2:
            bulk = client.post(
                "/api/v1/dashboard/upload",
                files=[
                    ("files", ("chase.pdf", h1, "application/pdf")),
                    ("files", ("schwab.pdf", h2, "application/pdf")),
                ],
                headers=HX,
            )
        if bulk.status_code == 200 and "Uploaded 2 of 2" in bulk.text:
            _pass("Legacy multi-file HTMX bulk still returns batch summary")
        else:
            _fail("Legacy bulk upload path broken")
            failures += 1

    print("\n" + "=" * 70)
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
