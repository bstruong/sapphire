#!/usr/bin/env python3
"""QA Acceptance Script: Track 1 PR CI slim (Playwright cache + path-aware E2E).

Target Audience: Product Manager & Quality Assurance
Purpose:
    Verify PR CI policy without reading the workflow diff: Playwright browsers
    are cached, docs/markdown-only changes skip Playwright + E2E, app-touching
    paths keep the coverage gate with E2E, and PR CI remains on ubuntu-latest.

Usage:
    uv run python scripts/qa_ci_pr_slim.py

Manual UI QA: not applicable (no user-visible UI).
"""

from __future__ import annotations

import sys
from pathlib import Path


def print_header(title: str) -> None:
    print(f"\n{'=' * 75}")
    print(f"[QA VERIFICATION] {title}")
    print(f"{'=' * 75}")


def assert_condition(name: str, passed: bool, detail: str = "") -> None:
    status = "[PASS]" if passed else "[FAIL]"
    print(f"{status} {name}")
    if detail:
        print(f"      Detail: {detail}")
    if not passed:
        raise AssertionError(f"QA Assertion Failed: {name} - {detail}")


def test_playwright_cache_policy(ci: str) -> None:
    print_header("1. Playwright browser cache (avoid cold --with-deps every run)")
    assert_condition(
        "actions/cache@v4 restores ~/.cache/ms-playwright",
        "actions/cache@v4" in ci and "~/.cache/ms-playwright" in ci,
    )
    assert_condition(
        "Cache key includes runner OS, arch, and Playwright version",
        "playwright-${{ runner.os }}" in ci
        and "runner.arch" in ci
        and "steps.playwright.outputs.version" in ci,
    )
    assert_condition(
        "Cold install uses playwright install --with-deps only on cache miss",
        "cache-hit != 'true'" in ci and "playwright install --with-deps" in ci,
    )
    assert_condition(
        "Cache hit installs OS deps only (playwright install-deps)",
        "cache-hit == 'true'" in ci and "playwright install-deps" in ci,
    )


def test_path_aware_e2e_skip(ci: str) -> None:
    print_header("2. Path-aware E2E / Playwright skip (docs & markdown-only)")
    assert_condition(
        "dorny/paths-filter@v3 drives needs_e2e",
        "dorny/paths-filter@v3" in ci and "needs_e2e:" in ci,
    )
    for path in (
        "app/**",
        "tests/**",
        "scripts/**",
        "pyproject.toml",
        "uv.lock",
        ".github/workflows/**",
        "**/*.py",
    ):
        assert_condition(
            f"needs_e2e includes {path}",
            path in ci,
            "App-touching / lockfile / workflow / Python paths keep full suite.",
        )
    assert_condition(
        "Docs/markdown-only path runs pytest without E2E",
        "--ignore=tests/test_e2e_dashboard.py" in ci and "needs_e2e != 'true'" in ci,
    )
    assert_condition(
        "App-touching path keeps coverage gate including E2E",
        "needs_e2e == 'true'" in ci and "--cov-fail-under=90" in ci,
    )


def test_hosting_and_scope(ci: str, nightly: str) -> None:
    print_header("3. Scope guards (Track 1 only)")
    assert_condition(
        "PR CI remains on ubuntu-latest (not self-hosted)",
        "runs-on: ubuntu-latest" in ci and "self-hosted" not in ci,
    )
    assert_condition(
        "PR CI does not run mutation",
        "check_mutation_score.py" not in ci and "mutation:" not in ci,
    )
    assert_condition(
        "mutation-nightly.yml left untouched by Track 1 policy checks",
        "check_mutation_score.py" in nightly and "--full-allowlist" in nightly,
        "Track 2 owns mutation redraw; this script only confirms nightly still exists.",
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("=" * 75)
    print("SAPPHIRE Product Manager & QA Acceptance Test")
    print("   Feature: Track 1 PR CI slim (Playwright cache + path-aware E2E)")
    print("=" * 75)

    ci_path = root / ".github" / "workflows" / "ci.yml"
    nightly_path = root / ".github" / "workflows" / "mutation-nightly.yml"
    try:
        assert_condition("ci.yml exists", ci_path.is_file())
        assert_condition("mutation-nightly.yml exists", nightly_path.is_file())
        ci = ci_path.read_text(encoding="utf-8")
        nightly = nightly_path.read_text(encoding="utf-8")
        test_playwright_cache_policy(ci)
        test_path_aware_e2e_skip(ci)
        test_hosting_and_scope(ci, nightly)
        print("\n" + "=" * 75)
        print("QA RESULT: ALL ACCEPTANCE GATES PASSED")
        print("=" * 75)
        return 0
    except AssertionError as err:
        print("\n" + "=" * 75)
        print(f"QA RESULT: FAILED - {err}")
        print("=" * 75)
        return 1


if __name__ == "__main__":
    sys.exit(main())
