"""Unit tests for CI reliability Track 2 policy (mutation self-hosted)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
MUTATION_YML = ROOT / ".github" / "workflows" / "mutation-nightly.yml"
AGENTS_MD = ROOT / "AGENTS.md"


@pytest.fixture(scope="module")
def ci_text() -> str:
    assert CI_YML.is_file(), "ci.yml must exist"
    return CI_YML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mutation_text() -> str:
    assert MUTATION_YML.is_file(), "mutation-nightly.yml must exist"
    return MUTATION_YML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def agents_text() -> str:
    assert AGENTS_MD.is_file(), "AGENTS.md must exist"
    return AGENTS_MD.read_text(encoding="utf-8")


def test_pr_ci_does_not_run_mutmut(ci_text: str) -> None:
    assert "check_mutation_score.py" not in ci_text
    assert "run_mutation_on_diff.py" not in ci_text
    assert "mutation:" not in ci_text
    assert "runs-on: ubuntu-latest" in ci_text
    assert "self-hosted" not in ci_text


def test_mutation_runs_on_self_hosted_watchtower(mutation_text: str) -> None:
    assert "runs-on: [self-hosted, watchtower]" in mutation_text
    assert "runs-on: ubuntu-latest" not in mutation_text


def test_mutation_triggers_weekly_path_push_dispatch(mutation_text: str) -> None:
    assert 'cron: "0 8 * * 0"' in mutation_text
    assert "workflow_dispatch:" in mutation_text
    assert "app/pipeline/**" in mutation_text
    assert "# Temporarily disabled automatic runs" not in mutation_text


def test_mutation_push_diff_vs_full_allowlist(mutation_text: str) -> None:
    assert "--full-allowlist" in mutation_text
    assert 'github.event_name }}" = "push"' in mutation_text
    assert "check_mutation_score.py" in mutation_text
    assert "--min-score 55.0" in mutation_text
    assert "--max-no-tests 0" in mutation_text


def test_mutmut_cache_key_omits_github_sha(mutation_text: str) -> None:
    key_lines = [
        line for line in mutation_text.splitlines() if line.strip().startswith("key:")
    ]
    assert any(
        "mutmut-nightly-${{ steps.mutmut_meta.outputs.mutmut_ver }}" in line
        and "github.sha" not in line
        for line in key_lines
    )


def test_mutation_uses_host_postgres_circuit_breaker(mutation_text: str) -> None:
    """DinD postgres:15 whiteout on Watchtower — no Actions service containers."""
    assert re.search(r"(?m)^\s+services:\s*$", mutation_text) is None
    assert "image: postgres:" not in mutation_text
    assert "54329/sapphire_test" in mutation_text
    assert "WATCHTOWER_HOST_IP" in mutation_text


def test_docs_path_e2e_skip_present_in_ci(ci_text: str) -> None:
    assert "dorny/paths-filter@v3" in ci_text
    assert "needs_e2e:" in ci_text
    assert "--ignore=tests/test_e2e_dashboard.py" in ci_text


def test_agents_documents_self_hosted_mutation(agents_text: str) -> None:
    assert "self-hosted" in agents_text
    assert "watchtower" in agents_text.lower()
    assert "does **not** run daily on `ubuntu-latest`" in agents_text
