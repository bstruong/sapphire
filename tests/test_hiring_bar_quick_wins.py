"""Regression checks for hiring-bar Quick Wins docs/infra surfaces."""

from __future__ import annotations

from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def test_readme_is_single_entry_for_seed_health_demo_docker() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "scripts/seed_dashboard.py" in readme
    assert "/api/v1/health" in readme
    assert "docs/demo.md" in readme
    assert "docker compose" in readme
    assert "VaultInferenceClient" in readme
    assert "qa_watchtower_inference.py" in readme
    assert "fail-soft" in readme


def test_demo_md_exists_with_invariants() -> None:
    body = (ROOT / "docs" / "demo.md").read_text(encoding="utf-8")
    assert "integer cents" in body.lower()
    assert "fail-loud" in body.lower()
    assert "zero-pii" in body.lower() or "zero-PII" in body


def test_compose_healthchecks_api_health() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "healthcheck:" in compose
    assert "/api/v1/health" in compose
    assert (ROOT / "Dockerfile").is_file()


def test_watchtower_qa_fail_soft_when_offline(monkeypatch: Any) -> None:
    """Endpoint down → exit 0; offline contracts still recorded."""
    import scripts.qa_watchtower_inference as qa

    class OfflineClient:
        base_url = "http://example.invalid/v1"
        model = "test-model"
        timeout = 1.0

        def is_available(self) -> bool:
            return False

        def chat_completion(self, messages: list[Any], **kwargs: Any) -> Any:
            from app.models.exceptions import InferenceContractError

            if not messages:
                raise InferenceContractError("empty")
            raise AssertionError("unexpected live call")

        def strip_pii(self, text: str) -> str:
            if not text.strip():
                raise ValueError("empty")
            return text

        def extract_structured(
            self, text: str, schema_description: str
        ) -> dict[str, Any]:
            if not schema_description.strip():
                raise ValueError("empty schema")
            raise AssertionError("unexpected live call")

        def get_models(self) -> list[str]:
            raise AssertionError("unexpected live call")

        def complete(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("unexpected live call")

    class BrokenClient(OfflineClient):
        def get_models(self) -> list[str]:
            from app.models.exceptions import InferenceConnectionError

            raise InferenceConnectionError("unreachable")

    def fake_ctor(*args: Any, **kwargs: Any) -> OfflineClient:
        if kwargs.get("base_url") or (args and "9999" in str(args[0])):
            return BrokenClient()
        return OfflineClient()

    monkeypatch.setattr(qa, "VaultInferenceClient", fake_ctor)
    assert qa.main() == 0
