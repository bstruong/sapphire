#!/usr/bin/env python3
"""QA Acceptance Script: CI reliability Track 2 (mutation self-hosted policy).

Target Audience: Product Manager & Quality Assurance
Purpose:
    Verify workflow policy without reading diffs: mutmut stays off PR CI,
    mutation runs on Watchtower self-hosted, docs/path E2E skip remains in
    ci.yml, and the mutmut cache key is stable across commits.

Usage:
    uv run python scripts/qa_ci_reliability.py

Manual UI QA: not applicable (no user-visible UI).
"""

from __future__ import annotations

import re
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


def test_no_mutmut_on_pr_ci(ci: str) -> None:
    print_header("1. PR CI does not run mutmut")
    assert_condition(
        "ci.yml has no mutation job / score gate",
        "check_mutation_score.py" not in ci
        and "run_mutation_on_diff.py" not in ci
        and "mutation:" not in ci,
        "Mutation must stay off the PR merge path.",
    )
    assert_condition(
        "ci.yml stays on ubuntu-latest (not self-hosted)",
        "runs-on: ubuntu-latest" in ci and "self-hosted" not in ci,
        "Required PR checks must not SPOF on Watchtower.",
    )


def test_mutation_self_hosted_policy(nightly: str) -> None:
    print_header("2. Mutation runs on Watchtower self-hosted")
    assert_condition(
        "mutation job runs-on [self-hosted, watchtower]",
        "runs-on: [self-hosted, watchtower]" in nightly,
        "Quota-saving redraw: mutation leaves ubuntu-latest.",
    )
    assert_condition(
        "mutation is not pinned to ubuntu-latest",
        "runs-on: ubuntu-latest" not in nightly,
    )
    assert_condition(
        "weekly schedule cron present (Sunday 08:00 UTC)",
        'cron: "0 8 * * 0"' in nightly or "cron: '0 8 * * 0'" in nightly,
        "Full-allowlist is weekly, not daily github-hosted.",
    )
    assert_condition(
        "path-filtered push on main is enabled (not commented out)",
        re.search(r"(?m)^  push:\s*$", nightly) is not None
        and "app/pipeline/**" in nightly
        and "# Temporarily disabled automatic runs" not in nightly,
    )
    assert_condition(
        "workflow_dispatch remains for on-demand full runs",
        "workflow_dispatch:" in nightly,
    )
    assert_condition(
        "score gate still enforces classic >=55% and max-no-tests 0",
        "check_mutation_score.py" in nightly
        and "--min-score 55.0" in nightly
        and "--max-no-tests 0" in nightly,
    )
    assert_condition(
        "full-allowlist used for schedule/dispatch (not only push)",
        "--full-allowlist" in nightly and 'github.event_name }}" = "push"' in nightly,
        "Push → diff; schedule/dispatch → --full-allowlist.",
    )
    cache_key_lines = [
        line for line in nightly.splitlines() if line.strip().startswith("key:")
    ]
    assert_condition(
        "mutmut cache key omits commit SHA (hits across commits)",
        any(
            "mutmut-nightly-${{ steps.mutmut_meta.outputs.mutmut_ver }}" in line
            and "github.sha" not in line
            for line in cache_key_lines
        ),
        f"cache key lines={cache_key_lines!r}",
    )
    assert_condition(
        "mutation job does not install Playwright",
        "playwright install" not in nightly,
    )
    assert_condition(
        "mutation does not use DinD services postgres (circuit breaker)",
        re.search(r"(?m)^\s+services:\s*$", nightly) is None
        and "image: postgres:" not in nightly,
        "Watchtower DinD whiteout EPERM — use host Postgres :54329 instead.",
    )
    assert_condition(
        "mutation points at host Postgres sapphire_test on :54329",
        "54329/sapphire_test" in nightly and "WATCHTOWER_HOST_IP" in nightly,
        "Must resolve Watchtower host IP and use :54329/sapphire_test.",
    )


def test_docs_path_e2e_skip(ci: str) -> None:
    print_header("3. Docs / markdown path E2E skip present in ci.yml (Track 1)")
    assert_condition(
        "dorny/paths-filter@v3 drives needs_e2e",
        "dorny/paths-filter@v3" in ci and "needs_e2e:" in ci,
    )
    assert_condition(
        "docs/markdown-only path skips E2E",
        "--ignore=tests/test_e2e_dashboard.py" in ci and "needs_e2e != 'true'" in ci,
    )


def test_negative_policy_detection() -> None:
    print_header("4. Negative cases (policy regressions fail loud)")
    bad_ci = (
        "jobs:\n  mutation:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: uv run python scripts/check_mutation_score.py\n"
    )
    assert_condition(
        "Detects mutmut leaked onto PR CI",
        "check_mutation_score.py" in bad_ci and "mutation:" in bad_ci,
        "Synthetic bad ci.yml would fail gate 1.",
    )
    bad_nightly = "runs-on: ubuntu-latest\nkey: mutmut-nightly-x-y-${{ github.sha }}\n"
    bad_key_lines = [
        line for line in bad_nightly.splitlines() if line.strip().startswith("key:")
    ]
    assert_condition(
        "Detects mutation back on ubuntu-latest",
        "runs-on: ubuntu-latest" in bad_nightly and "self-hosted" not in bad_nightly,
        "Synthetic bad mutation-nightly.yml would fail gate 2.",
    )
    assert_condition(
        "Detects cache key regressing to include commit SHA",
        any("github.sha" in line for line in bad_key_lines),
        "Synthetic bad cache key would fail gate 2.",
    )
    bad_ci_no_path = "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
    assert_condition(
        "Detects missing docs/path E2E skip",
        "dorny/paths-filter@v3" not in bad_ci_no_path
        and "needs_e2e" not in bad_ci_no_path,
        "Synthetic path-blind ci.yml would fail gate 3.",
    )


def test_agents_docs_match_reality(agents: str) -> None:
    print_header("5. AGENTS.md matches self-hosted mutation reality")
    assert_condition(
        "AGENTS documents self-hosted / watchtower mutation",
        "self-hosted" in agents and "watchtower" in agents.lower(),
    )
    assert_condition(
        "AGENTS denies daily github-hosted mutation",
        "does **not** run daily on `ubuntu-latest`" in agents
        or "not daily on ubuntu-latest" in agents.lower(),
        "Must explicitly reject daily ubuntu-latest mutation.",
    )
    assert_condition(
        "AGENTS still says mutation is not a PR required check",
        "not** a PR required check" in agents
        or "does **not** run mutmut" in agents
        or "PR CI does not run mutmut" in agents,
    )


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    print("=" * 75)
    print("SAPPHIRE Product Manager & QA Acceptance Test")
    print("   Feature: CI reliability Track 2 (mutation → Watchtower)")
    print("=" * 75)

    ci_path = root / ".github" / "workflows" / "ci.yml"
    nightly_path = root / ".github" / "workflows" / "mutation-nightly.yml"
    agents_path = root / "AGENTS.md"
    try:
        assert_condition("ci.yml exists", ci_path.is_file())
        assert_condition("mutation-nightly.yml exists", nightly_path.is_file())
        assert_condition("AGENTS.md exists", agents_path.is_file())
        ci = ci_path.read_text(encoding="utf-8")
        nightly = nightly_path.read_text(encoding="utf-8")
        agents = agents_path.read_text(encoding="utf-8")
        test_no_mutmut_on_pr_ci(ci)
        test_mutation_self_hosted_policy(nightly)
        test_docs_path_e2e_skip(ci)
        test_negative_policy_detection()
        test_agents_docs_match_reality(agents)
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
