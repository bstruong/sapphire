"""Unit tests for xdist-safe test DB helpers (no coverage compromise)."""

from __future__ import annotations

import pytest

from tests.conftest import _ensure_worker_database, _replace_database_name


def test_replace_database_name_keeps_credentials() -> None:
    url = "postgresql://postgres:sapphire@localhost:54329/sapphire_test"
    out = _replace_database_name(url, "sapphire_test_gw0")
    assert out == "postgresql://postgres:sapphire@localhost:54329/sapphire_test_gw0"


def test_replace_database_name_rejects_unsafe() -> None:
    with pytest.raises(ValueError, match="Unsafe"):
        _replace_database_name(
            "postgresql://postgres:sapphire@localhost:54329/sapphire_test",
            "sapphire-test;drop",
        )


def test_master_worker_reuses_base_url() -> None:
    base = "postgresql://postgres:sapphire@localhost:54329/sapphire_test"
    assert _ensure_worker_database(base, "master") == base
