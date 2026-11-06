#!/usr/bin/env python3
"""QA: Statements in the browser (Schwab + Chase).

Happy: Chase and Schwab fixture PDFs upload via dashboard HTML path and
return templated integer-cent ledgers.
Negative: unmatched PDF is rejected loudly (ADAPTER_MATCH_FAILED).
"""

from __future__ import annotations

import json
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
    print("QA: Statements in the browser — Chase + Schwab upload → templated ledger")
    failures = 0

    # Ensure DB
    import os

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
        _fail(f"database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    client = TestClient(create_app())
    dash = client.get("/dashboard")
    if dash.status_code == 200 and "Upload to Vault" in dash.text:
        _pass("dashboard shows Upload to Vault")
    else:
        _fail("dashboard missing upload control")
        failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for fixture, adapter_id in (
            ("chase_checking_happy", "chase_depository"),
            ("brokerage_cash_happy", "schwab_brokerage"),
        ):
            pdf = _render_pdf(fixture, tmp_path / f"{fixture}.pdf")
            with pdf.open("rb") as handle:
                response = client.post(
                    "/api/v1/dashboard/upload",
                    files={"file": (pdf.name, handle, "application/pdf")},
                    headers={"HX-Request": "true"},
                )
            if (
                response.status_code == 200
                and "Vault Ledger" in response.text
                and adapter_id in response.text
                and re.search(r"\$\d[\d,]*\.\d{2}\b", response.text)
                and not re.findall(r"\$\d[\d,]*\.\d{3,}", response.text)
            ):
                _pass(f"upload {fixture} → USD ledger ({adapter_id})")
            else:
                _fail(
                    f"upload {fixture} unexpected "
                    f"status={response.status_code} body={response.text[:200]}"
                )
                failures += 1
        # Negative unmatched
        from reportlab.pdfgen import canvas

        bad = tmp_path / "unknown.pdf"
        c = canvas.Canvas(str(bad))
        c.drawString(50, 750, "UNKNOWN CREDIT UNION CHECKING")
        c.save()
        with bad.open("rb") as handle:
            neg = client.post(
                "/api/v1/dashboard/upload",
                files={"file": ("unknown.pdf", handle, "application/pdf")},
                headers={"HX-Request": "true"},
            )
        if neg.status_code == 400 and "ADAPTER_MATCH_FAILED" in neg.text:
            _pass("negative: unmatched PDF rejected loudly")
        else:
            _fail(
                f"negative unmatched expected 400 ADAPTER_MATCH_FAILED, got {neg.status_code}"
            )
            failures += 1

    if failures:
        print(f"\nResult: [FAIL] ({failures} check(s) failed)")
        return 1
    print("\nResult: [PASS] statements-in-browser checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
