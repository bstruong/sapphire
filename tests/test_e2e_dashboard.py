import re
import subprocess
import time

import pytest
from playwright.sync_api import Page, expect


@pytest.fixture(scope="module")
def live_server():
    # Start the FastAPI server
    proc = subprocess.Popen(
        [
            "uv",
            "run",
            "uvicorn",
            "app.api.app:create_app",
            "--factory",
            "--port",
            "8000",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for the server to be ready
    import urllib.request
    from urllib.error import URLError

    timeout = 10
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            urllib.request.urlopen("http://localhost:8000/dashboard")
            break
        except URLError:
            time.sleep(0.5)

    yield "http://localhost:8000"

    proc.terminate()
    proc.wait()


def test_htmx_dom_swap(page: Page, live_server: str):
    page.goto(f"{live_server}/dashboard")

    # Check that htmx is loaded
    assert page.evaluate("window.htmx !== undefined")

    # Ledger refresh lives in the Audit view of the sidebar navigation
    page.locator("#nav-audit").click()
    expect(page.locator("#submit-btn")).to_be_visible(timeout=5000)
    expect(page.locator("#topbar-title")).to_have_text("Audit")

    # Refresh latest ledger (templated DB view — empty or populated)
    page.fill("#question", "Show latest Vault ledger")
    page.locator("#submit-btn").click()

    result_div = page.locator(".ledger-result")
    expect(result_div).to_contain_text("Vault Ledger", timeout=5000)


def test_theme_toggle_interaction(page: Page, live_server: str):
    page.add_init_script("window.localStorage.setItem('sapphire-theme', 'light');")
    page.goto(f"{live_server}/dashboard")

    theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert theme == "light"

    toggle = page.locator("#theme-toggle")
    toggle.click()
    theme_dark = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert theme_dark == "dark"
    stored_dark = page.evaluate("window.localStorage.getItem('sapphire-theme')")
    assert stored_dark == "dark"

    toggle.click()
    theme_light = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert theme_light == "light"
    stored_light = page.evaluate("window.localStorage.getItem('sapphire-theme')")
    assert stored_light == "light"


def test_manual_ui_nav_path(page: Page, live_server: str):
    """Overview → Budget (over-assign) → Statements (bad upload) → Audit."""
    page.add_init_script("window.localStorage.setItem('sapphire-theme', 'light');")
    page.goto(f"{live_server}/dashboard")

    expect(page.locator("#nav-overview")).to_be_visible()
    expect(page.locator("#topbar-title")).to_have_text("Overview")
    expect(page.locator("#pane-vault-body")).to_contain_text("Account overview")

    page.locator("#nav-budget").click()
    expect(page.locator("#topbar-title")).to_have_text("Budget", timeout=5000)
    expect(page.locator("#budget-assign-form")).to_be_visible()
    expect(page.locator("#envelope-create-form")).to_be_visible()

    # Loud over-assign when envelopes exist; otherwise create fails loudly on blank.
    # Empty vault: assign disabled — create a temp envelope then over-assign if possible.
    create = page.locator("#envelope-create-form")
    if create.locator("input[name='name']").count():
        create.locator("input[name='name']").fill("SYNTHETIC E2E Envelope")
        create.locator("button[type='submit']").click()
        expect(page.locator("#assign-envelope")).to_contain_text(
            "SYNTHETIC E2E Envelope", timeout=5000
        )
        page.locator("#assign-amount").fill("999999999")
        page.locator("#assign-btn").click()
        expect(page.locator("[role='alert']")).to_contain_text(
            "ASSIGN_EXCEEDS_AVAILABLE", timeout=5000
        )
        expect(page.locator("[role='alert']")).to_contain_text("Nothing was saved")

    page.locator("#nav-statements").click()
    expect(page.locator("#topbar-title")).to_have_text("Statements", timeout=5000)
    expect(page.locator("#upload-dropzone")).to_be_visible()
    expect(page.locator("#upload-form")).to_be_visible()

    page.set_input_files(
        "#statement-file",
        {
            "name": "not-a-pdf.txt",
            "mimeType": "text/plain",
            "buffer": b"not a pdf",
        },
    )
    expect(page.locator("#upload-prompt")).to_contain_text(
        "not-a-pdf.txt", timeout=5000
    )
    expect(page.locator("#upload-dropzone")).to_have_class(re.compile(r"has-file"))
    page.locator("#upload-btn").click()
    expect(page.locator("[role='alert']")).to_contain_text(
        "Nothing was saved", timeout=5000
    )

    page.locator("#nav-audit").click()
    expect(page.locator("#topbar-title")).to_have_text("Audit", timeout=5000)
    expect(page.locator("#submit-btn")).to_be_visible()
    expect(page.locator("#frontier-verify-btn")).to_be_visible()

    page.locator("#theme-toggle").click()
    theme = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert theme == "dark"
