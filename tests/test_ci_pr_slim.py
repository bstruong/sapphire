"""Unit tests for Track 1 PR CI slim policy encoded in .github/workflows/ci.yml."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def ci_text() -> str:
    # mutmut copies app/ (+ tests) into mutants/ without .github/; skip there.
    if not CI_YML.is_file():
        pytest.skip("ci.yml missing (not a full repo checkout)")
    return CI_YML.read_text(encoding="utf-8")


def test_playwright_cache_key_uses_os_arch_and_version(ci_text: str) -> None:
    assert "actions/cache@v4" in ci_text
    assert "~/.cache/ms-playwright" in ci_text
    assert "playwright-chromium-${{ runner.os }}" in ci_text
    assert "runner.arch" in ci_text
    assert "steps.playwright.outputs.version" in ci_text


def test_playwright_cold_install_only_on_cache_miss(ci_text: str) -> None:
    assert "cache-hit != 'true'" in ci_text
    assert "playwright install --with-deps chromium" in ci_text
    assert "cache-hit == 'true'" in ci_text
    assert "playwright install-deps chromium" in ci_text


def test_docs_and_non_python_script_paths_skip_tests_and_e2e(ci_text: str) -> None:
    assert "dorny/paths-filter@v3" in ci_text
    assert "needs_tests:" in ci_text
    assert "needs_e2e:" in ci_text
    for required in (
        "app/**",
        "tests/**",
        "scripts/**/*.py",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/**",
    ):
        assert required in ci_text
    # Broad globs that forced Playwright on shell scripts / any .py are gone.
    assert "- 'scripts/**'" not in ci_text
    assert "- '**/*.py'" not in ci_text
    assert "--ignore=tests/test_e2e_dashboard.py" in ci_text
    # Always-green aggregator waives the required check without Postgres.
    assert "name: Test Coverage Gate" in ci_text
    assert "Skipping pytest + Postgres" in ci_text or "coverage gate waived" in ci_text
    assert "needs.changes.outputs.needs_tests" in ci_text


def test_e2e_filter_targets_ui_surface_only(ci_text: str) -> None:
    e2e_block = ci_text.split("needs_e2e:", 1)[1]
    # Truncate at the next workflow job after the paths-filter block.
    for sep in ("\n  test:", "\n  test_gate:", "\n- name:"):
        if sep in e2e_block:
            e2e_block = e2e_block.split(sep, 1)[0]
            break
    for required in (
        "app/api/**",
        "app/routers/**",
        "app/templates/**",
        "app/static/**",
        "app/presentation/**",
        "app/middleware/**",
        "tests/test_e2e_dashboard.py",
        ".github/workflows/ci.yml",
    ):
        assert required in e2e_block
    # Pipeline-only / adapter-only changes must not pull Playwright.
    assert "app/pipeline/**" not in e2e_block
    assert "app/adapters/**" not in e2e_block


def test_app_paths_keep_coverage_gate_with_e2e(ci_text: str) -> None:
    assert "needs_e2e == 'true'" in ci_text
    assert "needs_tests == 'true'" in ci_text
    assert "--cov-fail-under=90" in ci_text
    assert "runs-on: ubuntu-latest" in ci_text
    assert "self-hosted" not in ci_text
    assert "check_mutation_score.py" not in ci_text
    assert "uv sync --frozen" in ci_text


def test_coverage_gate_uses_xdist_without_dropping_cov(ci_text: str) -> None:
    """Speed via xdist + per-worker DBs; ≥90% coverage gate must remain."""
    assert "-n 2" in ci_text
    assert "--dist loadgroup" in ci_text
    assert ci_text.count("--cov-fail-under=90") >= 2
    assert "--cov=app" in ci_text
    # Postgres service only on the real pytest job (not the always-green gate).
    assert "name: Pytest + Coverage" in ci_text
    assert "image: postgres:15" in ci_text


def test_postgres_not_required_on_docs_only_gate(ci_text: str) -> None:
    gate = ci_text.split("name: Test Coverage Gate", 1)[1]
    assert "image: postgres:15" not in gate.split("name:", 1)[0]
    assert "if: always()" in ci_text
