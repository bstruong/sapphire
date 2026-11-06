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
    assert "playwright-${{ runner.os }}" in ci_text
    assert "runner.arch" in ci_text
    assert "steps.playwright.outputs.version" in ci_text


def test_playwright_cold_install_only_on_cache_miss(ci_text: str) -> None:
    assert "cache-hit != 'true'" in ci_text
    assert "playwright install --with-deps" in ci_text
    assert "cache-hit == 'true'" in ci_text
    assert "playwright install-deps" in ci_text


def test_docs_markdown_path_skips_e2e_and_playwright(ci_text: str) -> None:
    assert "dorny/paths-filter@v3" in ci_text
    assert "needs_e2e:" in ci_text
    for required in (
        "app/**",
        "tests/**",
        "scripts/**",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/**",
        "**/*.py",
    ):
        assert required in ci_text
    assert "--ignore=tests/test_e2e_dashboard.py" in ci_text
    assert "needs_e2e != 'true'" in ci_text


def test_app_paths_keep_coverage_gate_with_e2e(ci_text: str) -> None:
    assert "needs_e2e == 'true'" in ci_text
    assert "--cov-fail-under=90" in ci_text
    assert "runs-on: ubuntu-latest" in ci_text
    assert "self-hosted" not in ci_text
    assert "check_mutation_score.py" not in ci_text
