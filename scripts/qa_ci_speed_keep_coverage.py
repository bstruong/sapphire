#!/usr/bin/env python3
"""QA Acceptance: CI speed without compromising ≥90% coverage.

Verifies:
  - Always-green Test Coverage Gate (no Postgres on docs-only PRs)
  - Full suite still gated with --cov=app --cov-fail-under=90
  - xdist (-n 2, loadgroup) for wall-clock speed
  - Per-worker DB helpers exist (TRUNCATE-safe parallelism)

Usage:
    uv run python scripts/qa_ci_speed_keep_coverage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pass(name: str, detail: str = "") -> None:
    print(f"  [PASS] {name}" + (f": {detail}" if detail else ""))


def _fail(name: str, detail: str = "") -> None:
    print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))
    raise AssertionError(f"{name}: {detail}")


def main() -> int:
    print("=" * 65)
    print("  SAPPHIRE CI speed (keep ≥90% coverage) QA")
    print("=" * 65)
    ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    conf = ROOT / "tests" / "conftest.py"
    try:
        if not ci_path.is_file():
            _fail("ci.yml exists", "missing")
        ci = ci_path.read_text(encoding="utf-8")
        _pass("ci.yml exists")

        if "name: Test Coverage Gate" not in ci:
            _fail("required check name preserved", "Test Coverage Gate")
        _pass("required check name preserved", "Test Coverage Gate")

        if "if: always()" not in ci:
            _fail("always-green aggregator", "missing if: always()")
        _pass("always-green aggregator present")

        if "--cov-fail-under=90" not in ci or "--cov=app" not in ci:
            _fail("coverage gate retained", "missing --cov / --cov-fail-under=90")
        _pass("coverage gate retained", f"count={ci.count('--cov-fail-under=90')}")

        if "-n 2" not in ci or "--dist loadgroup" not in ci:
            _fail("xdist enabled on pytest job", "missing -n 2 / loadgroup")
        _pass("xdist enabled", "-n 2 --dist loadgroup")

        if "name: Pytest + Coverage" not in ci:
            _fail("pytest job separated from gate", "missing Pytest + Coverage")
        _pass("pytest job separated from gate")

        conf_text = conf.read_text(encoding="utf-8")
        if "_ensure_worker_database" not in conf_text:
            _fail("per-worker DB helper", "missing in conftest")
        _pass("per-worker DB helper in conftest")
        if "_session_schema" not in conf_text:
            _fail("session-scoped schema init", "missing")
        _pass("session-scoped schema init (not per-test init_db)")

        print(f"\n{'=' * 65}")
        print("All CI speed / coverage-retention checks passed!")
        return 0
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
