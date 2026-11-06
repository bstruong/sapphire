#!/usr/bin/env python3
"""QA: Dashboard Refresh (Modern UI/UX, Light-Default Theme Toggle, Single Nav Shell).

Verifies:
1. Shell & Navigation: Single sidebar navigation with HTMX endpoints, light theme default, theme toggle.
2. Styling & Asset Integrity: No inline styles (style=), local CSS with zero remote CDN dependencies.
3. Overview Surface: Prepopulated KPI cards (Total closing, Ready to Assign, Last statement), clickable account rows.
4. Budget Surface: Inline envelope assignment, Ready to Assign balance, and loud over-assign rejection.
5. Statements Surface: Local upload form with Vault boundary note, and loud invalid file rejection.
6. Audit Surface: Local ledger query, Frontier aggregate verification, and invalid group_by rejection.
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


def main() -> int:
    print("=" * 70)
    print("QA: Dashboard Refresh (Modern Aesthetic, Light-Default Theme, Local Shell)")
    print("=" * 70)
    failures = 0

    if not os.environ.get("DATABASE_URL"):
        os.environ["DATABASE_URL"] = (
            "postgresql://postgres:sapphire@localhost:54329/sapphire"
        )

    import psycopg
    from fastapi.testclient import TestClient

    from app.api.app import create_app
    from app.budget.envelopes import create_envelope
    from app.db.connection import init_db

    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        _fail(f"Database unavailable: {type(exc).__name__}")
        print("\nResult: [FAIL]")
        return 1

    # Clean isolation for verification
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

    # 1. Shell & Asset Integrity
    print("\n[Step 1: Shell & Asset Integrity]")
    resp = client.get("/dashboard")
    if resp.status_code == 200:
        html = resp.text
        if '<html lang="en" data-theme="light">' in html:
            _pass("Dashboard defaults to light theme (<html data-theme='light'>)")
        else:
            _fail("Dashboard missing light theme default")
            failures += 1

        if (
            'id="theme-toggle"' in html
            and "sapphire-theme" in html
            and "prefers-color-scheme" in html
        ):
            _pass("Theme toggle present; defaults from prefers-color-scheme when unset")
        else:
            _fail("Theme toggle, localStorage, or prefers-color-scheme missing")
            failures += 1

        pinned = (
            "pane-vault-body",
            "topbar-title",
            "topbar-upload",
            "nav-overview",
            "nav-budget",
            "nav-statements",
            "nav-audit",
        )
        missing_ids = [pid for pid in pinned if f'id="{pid}"' not in html]
        if not missing_ids:
            _pass("Single unified sidebar navigation with all pinned shell IDs")
        else:
            _fail(f"Shell missing pinned IDs: {missing_ids}")
            failures += 1

        if "style=" not in html and "<style" not in html:
            _pass("Strict zero-inline-style policy verified on dashboard shell")
        else:
            _fail("Found inline style attributes or <style> tags in shell")
            failures += 1

        if re.findall(r'(?:src|href)="https?://', html) == []:
            _pass("Zero external CDN script/font/style dependencies in dashboard HTML")
        else:
            _fail("Found external CDN dependencies in dashboard HTML")
            failures += 1
    else:
        _fail(f"GET /dashboard returned HTTP {resp.status_code}")
        failures += 1

    # 2. Local CSS Verification
    print("\n[Step 2: Local Static CSS Verification]")
    css_resp = client.get("/static/sapphire.css")
    if css_resp.status_code == 200:
        css = css_resp.text
        if '[data-theme="light"]' in css and '[data-theme="dark"]' in css:
            _pass(
                "Local sapphire.css defines both light and dark theme token specifications"
            )
        else:
            _fail("Local sapphire.css missing light or dark token blocks")
            failures += 1
        if "http://" not in css and "https://" not in css and "@import" not in css:
            _pass("Local sapphire.css contains zero external font/asset imports")
        else:
            _fail("Local sapphire.css contains external URLs or @import rules")
            failures += 1
    else:
        _fail("Failed to retrieve /static/sapphire.css")
        failures += 1

    # 3. Overview Surface & Empty State
    print("\n[Step 3: Overview Surface & Empty State]")
    ov_empty = client.get("/api/v1/dashboard/overview", headers=HX)
    if ov_empty.status_code == 200 and "No persisted statements yet" in ov_empty.text:
        _pass("Empty overview displays clean onboarding empty-state with upload action")
    else:
        _fail("Empty overview did not display expected empty state")
        failures += 1

    # Seed statement & envelopes
    print("\n[Step 4: Seed Data & Overview Populated Verification]")
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = _render_pdf("chase_checking_happy", Path(tmp_dir) / "chase.pdf")
        with pdf_path.open("rb") as f:
            up_resp = client.post(
                "/api/v1/dashboard/upload",
                files={"file": (pdf_path.name, f, "application/pdf")},
                headers=HX,
            )
        if up_resp.status_code == 200 and "Vault Ledger" in up_resp.text:
            _pass("Ingested Chase checking PDF statement into Vault")
        else:
            _fail(f"Statement upload failed with HTTP {up_resp.status_code}")
            failures += 1

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        env = create_envelope(conn, name="SYNTHETIC Groceries")
        conn.commit()
        envelope_id = str(env.envelope_id)
    _pass("Created initial SYNTHETIC Groceries envelope")

    ov_pop = client.get("/api/v1/dashboard/overview", headers=HX)
    if ov_pop.status_code == 200:
        html = ov_pop.text
        if (
            "Liquid cash" in html
            and "Brokerage funds" in html
            and "Ready to Assign" in html
            and "Last statement" in html
        ):
            _pass(
                "Populated Overview renders liquid vs brokerage KPIs (plus Ready to Assign, Last statement)"
            )
        else:
            _fail("Populated Overview missing required KPI cards")
            failures += 1

        if "Chase" in html and "Checking" in html:
            _pass(
                "Populated Overview renders interactive account listing with institution branding"
            )
        else:
            _fail("Populated Overview missing account listing details")
            failures += 1
    else:
        _fail(f"Populated overview GET returned HTTP {ov_pop.status_code}")
        failures += 1

    # 5. Budget Surface & Positive/Negative Assignment Verification
    print("\n[Step 5: Budget Surface Verification]")
    bg_resp = client.get("/api/v1/dashboard/budget", headers=HX)
    if (
        bg_resp.status_code == 200
        and "SYNTHETIC Groceries" in bg_resp.text
        and "Ready to Assign" in bg_resp.text
    ):
        _pass("Budget view displays Ready to Assign balance and envelope inventory")
    else:
        _fail("Budget view missing envelopes or Ready to Assign")
        failures += 1

    # Happy path assign
    assign_happy = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "50.00"},
        headers=HX,
    )
    if assign_happy.status_code == 200 and "$50.00" in assign_happy.text:
        _pass("Assigned $50.00 to SYNTHETIC Groceries successfully")
    else:
        _fail(f"Happy path assignment failed: {assign_happy.status_code}")
        failures += 1

    # Negative over-assign
    assign_over = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "9999999.00"},
        headers=HX,
    )
    if (
        assign_over.status_code == 400
        and "ASSIGN_EXCEEDS_AVAILABLE" in assign_over.text
        and 'role="alert"' in assign_over.text
    ):
        _pass(
            "Loud negative rejection: over-assignment rejected with HTTP 400 and ASSIGN_EXCEEDS_AVAILABLE alert"
        )
    else:
        _fail("Over-assignment was not rejected with expected error")
        failures += 1

    # Negative invalid amount
    assign_invalid = client.post(
        "/api/v1/dashboard/budget/assign",
        data={"envelope_id": envelope_id, "amount_usd": "abc"},
        headers=HX,
    )
    if assign_invalid.status_code == 400 and "INVALID_AMOUNT" in assign_invalid.text:
        _pass(
            "Loud negative rejection: non-numeric amount rejected with HTTP 400 and INVALID_AMOUNT alert"
        )
    else:
        _fail("Invalid amount was not rejected with expected error")
        failures += 1

    # 6. Statements Surface Verification
    print("\n[Step 6: Statements Surface Verification]")
    stmt_resp = client.get("/api/v1/dashboard/statements", headers=HX)
    if (
        stmt_resp.status_code == 200
        and "Upload to Vault" in stmt_resp.text
        and "never leave the Vault" in stmt_resp.text
        and 'id="upload-form"' in stmt_resp.text
        and 'id="upload-dropzone"' in stmt_resp.text
        and 'id="statement-file"' in stmt_resp.text
        and 'id="upload-prompt"' in stmt_resp.text
        and 'id="upload-filename"' in stmt_resp.text
        and 'id="upload-btn"' in stmt_resp.text
        and "style=" not in stmt_resp.text
    ):
        _pass(
            "Statements view displays local upload dropzone and privacy guarantee notice"
        )
    else:
        _fail("Statements view missing upload form, dropzone, or privacy notice")
        failures += 1

    dash = client.get("/dashboard")
    if (
        dash.status_code == 200
        and "statement-file" in dash.text
        and ".files" in dash.text
    ):
        _pass(
            "Dashboard shell wires change handler to show chosen filename in dropzone"
        )
    else:
        _fail("Dashboard shell missing file-input change handler for dropzone label")
        failures += 1

    # Negative invalid upload
    up_bad = client.post(
        "/api/v1/dashboard/upload",
        files={"file": ("malformed.txt", b"plain text", "text/plain")},
        headers=HX,
    )
    if (
        up_bad.status_code == 400
        and 'role="alert"' in up_bad.text
        and "nothing was saved" in up_bad.text.lower()
        and "<code>" in up_bad.text
    ):
        _pass(
            "Loud negative rejection: non-PDF upload rejected with HTTP 400 error fragment"
        )
    else:
        _fail("Invalid file upload did not return HTTP 400 with nothing-saved alert")
        failures += 1

    # 7. Audit Surface Verification
    print("\n[Step 7: Audit Surface Verification]")
    audit_resp = client.get("/api/v1/dashboard/audit", headers=HX)
    if (
        audit_resp.status_code == 200
        and "Vault ledger" in audit_resp.text
        and "Frontier verification" in audit_resp.text
        and 'id="submit-btn"' in audit_resp.text
        and 'id="frontier-verify-btn"' in audit_resp.text
        and "style=" not in audit_resp.text
    ):
        _pass("Audit view displays dual ledger and frontier verification interfaces")
    else:
        _fail("Audit view missing ledger or frontier verification interface")
        failures += 1

    # Negative frontier audit (empty grouping)
    audit_bad = client.post(
        "/api/v1/dashboard/frontier-audit",
        data={"group_by": ""},
        headers=HX,
    )
    if (
        audit_bad.status_code == 400
        and "Rejected" in audit_bad.text
        and "INVALID_GROUP_BY" in audit_bad.text
        and "nothing was saved" in audit_bad.text.lower()
    ):
        _pass(
            "Loud negative rejection: empty grouping rejected with HTTP 400 and error alert"
        )
    else:
        _fail("Empty frontier audit grouping was not rejected with HTTP 400")
        failures += 1

    # Budget / assign form pinned IDs after happy path
    if (
        'id="budget-assign-form"' in assign_happy.text
        and 'id="envelope-create-form"' in assign_happy.text
    ):
        _pass("Budget forms retain pinned IDs after successful assign")
    else:
        _fail("Budget assign/create form IDs missing after happy-path assign")
        failures += 1

    # Summary
    print("\n" + "=" * 70)
    if failures == 0:
        print("ALL VERIFICATIONS PASSED [PASS]")
        print("=" * 70)
        return 0
    else:
        print(f"{failures} VERIFICATION(S) FAILED [FAIL]")
        print("=" * 70)
        return 1


if __name__ == "__main__":
    sys.exit(main())
