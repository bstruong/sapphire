#!/usr/bin/env python3
"""QA Acceptance: Quick Wins (P3 / A3 / F2 / F1).

Verifies README is the single entry point for seed+health, local inference
contract docs, the 5-minute demo script, and Docker Compose + healthcheck.

Usage:
    uv run python scripts/qa_quick_wins.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pass(name: str, detail: str = "") -> None:
    print(f"  [PASS] {name}" + (f": {detail}" if detail else ""))


def _fail(name: str, detail: str = "") -> None:
    print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))
    raise AssertionError(f"{name}: {detail}")


def check_readme_quick_start(readme: str) -> None:
    print("\nP3 — README Quick Start (seed + health)")
    print("-" * 65)
    required = [
        "scripts/seed_dashboard.py",
        "DATABASE_URL=postgresql://postgres:sapphire@localhost:54329/sapphire",
        "uvicorn app.api.app:app",
        "/api/v1/health",
        "/dashboard",
    ]
    for needle in required:
        if needle not in readme:
            _fail("README Quick Start contains copy-pasteable cmd", needle)
        _pass("README includes", needle)


def check_inference_contract(readme: str) -> None:
    print("\nA3 — Local inference contract in README")
    print("-" * 65)
    for needle in (
        "VaultInferenceClient",
        "qa_watchtower_inference.py",
        "VAULT_INFERENCE_URL",
        "VAULT_INFERENCE_MODEL",
        "VAULT_INFERENCE_TIMEOUT",
        "fail-soft",
    ):
        if needle not in readme:
            _fail("README inference contract mentions", needle)
        _pass("README mentions", needle)

    qa = (ROOT / "scripts" / "qa_watchtower_inference.py").read_text(encoding="utf-8")
    if "fail-soft" not in qa.lower() and "Fail-soft" not in qa:
        _fail("qa_watchtower_inference.py documents fail-soft", "missing")
    if "report.skip" not in qa:
        _fail("qa_watchtower_inference.py skip path for offline", "no report.skip")
    _pass("qa_watchtower_inference.py has fail-soft skip path")


def check_demo(readme: str) -> None:
    print("\nF2 — docs/demo.md interview script")
    print("-" * 65)
    demo = ROOT / "docs" / "demo.md"
    if not demo.is_file():
        _fail("docs/demo.md exists", "missing")
    _pass("docs/demo.md exists")
    body = demo.read_text(encoding="utf-8")
    for needle in (
        "integer cents",
        "fail-loud",
        "zero-PII",
        "seed_dashboard",
        "/api/v1/health",
    ):
        if needle.lower() not in body.lower():
            _fail("docs/demo.md covers", needle)
        _pass("demo covers", needle)
    if "docs/demo.md" not in readme:
        _fail("README links docs/demo.md", "missing link")
    _pass("README links docs/demo.md")


def check_docker(readme: str) -> None:
    print("\nF1 — Dockerfile + compose.yaml")
    print("-" * 65)
    dockerfile = ROOT / "Dockerfile"
    compose = ROOT / "compose.yaml"
    if not dockerfile.is_file():
        _fail("Dockerfile exists", "missing")
    _pass("Dockerfile exists")
    if not compose.is_file():
        _fail("compose.yaml exists", "missing")
    _pass("compose.yaml exists")

    compose_text = compose.read_text(encoding="utf-8")
    if "/api/v1/health" not in compose_text:
        _fail("compose.yaml healthcheck path", "/api/v1/health missing")
    _pass("compose.yaml healthchecks /api/v1/health")
    if not re.search(r"healthcheck:", compose_text):
        _fail("compose.yaml defines healthcheck", "no healthcheck key")
    _pass("compose.yaml defines healthcheck")

    for needle in ("docker compose", "compose.yaml", "Dockerfile"):
        if needle not in readme:
            _fail("README documents docker path", needle)
        _pass("README mentions", needle)


def main() -> int:
    print("=" * 65)
    print("  SAPPHIRE Quick Wins QA")
    print("=" * 65)
    readme_path = ROOT / "README.md"
    if not readme_path.is_file():
        print("  [FAIL] README.md missing")
        return 1
    readme = readme_path.read_text(encoding="utf-8")
    try:
        check_readme_quick_start(readme)
        check_inference_contract(readme)
        check_demo(readme)
        check_docker(readme)
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}")
        return 1
    print(f"\n{'=' * 65}")
    print("All Quick Wins checks passed!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
