"""Tests for app.core.inference (Vault Inference Client).

Verifies the interface to the Watchtower containerized inference engine.
Ensures zero PII leakage in logs, fail-loud contract enforcement,
and clean configuration handling.
"""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

from app.core.inference import STRIP_PII_SYSTEM_PROMPT, VaultInferenceClient
from app.core.telemetry import JSONFormatter
from app.models.exceptions import (
    InferenceConnectionError,
    InferenceContractError,
    InferenceError,
    PipelineError,
)


class TestVaultInferenceConfig:
    def test_default_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_INFERENCE_URL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_URL", raising=False)
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        monkeypatch.delenv("VAULT_INFERENCE_MODEL", raising=False)
        monkeypatch.delenv("LOCAL_LLM_MODEL", raising=False)
        monkeypatch.delenv("VAULT_INFERENCE_TIMEOUT", raising=False)

        client = VaultInferenceClient()
        assert client.base_url == "http://ai.watchtower.local/v1"
        assert client.model == "qwen36-coder-hybrid"
        assert client.timeout == 30.0

    def test_explicit_config(self) -> None:
        client = VaultInferenceClient(
            base_url="http://ai.watchtower.local/v1/",
            model="custom-coder",
            timeout=15.0,
        )
        # Normalizes trailing slash
        assert client.base_url == "http://ai.watchtower.local/v1"
        assert client.model == "custom-coder"
        assert client.timeout == 15.0

    def test_env_var_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VAULT_INFERENCE_URL", "http://10.0.0.5:8080/v1/")
        monkeypatch.setenv("VAULT_INFERENCE_MODEL", "qwen-custom")
        monkeypatch.setenv("VAULT_INFERENCE_TIMEOUT", "45.5")

        client = VaultInferenceClient()
        assert client.base_url == "http://10.0.0.5:8080/v1"
        assert client.model == "qwen-custom"
        assert client.timeout == 45.5

    def test_fallback_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VAULT_INFERENCE_URL", raising=False)
        monkeypatch.setenv("OPENAI_BASE_URL", "http://cluster.local:8080/v1")
        monkeypatch.setenv("LOCAL_LLM_MODEL", "fallback-model")

        client = VaultInferenceClient()
        assert client.base_url == "http://cluster.local:8080/v1"
        assert client.model == "fallback-model"

    def test_invalid_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="base_url must not be empty"):
            VaultInferenceClient(base_url="")

    def test_invalid_model_raises(self) -> None:
        with pytest.raises(ValueError, match="model must not be empty"):
            VaultInferenceClient(model="")

    def test_invalid_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="timeout must be positive"):
            VaultInferenceClient(timeout=-1.0)

    def test_env_var_whitespace_url_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VAULT_INFERENCE_URL", "   ")
        with pytest.raises(ValueError, match="base_url must not be empty"):
            VaultInferenceClient()

    def test_env_var_whitespace_model_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VAULT_INFERENCE_URL", raising=False)
        monkeypatch.setenv("VAULT_INFERENCE_MODEL", "   ")
        with pytest.raises(ValueError, match="model must not be empty"):
            VaultInferenceClient()


class TestVaultInferenceModels:
    @patch("app.core.inference.urlopen")
    def test_get_models_success(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "object": "list",
                "data": [
                    {
                        "id": "qwen36-coder",
                        "aliases": ["qwen36-coder", "qwen36-coder-hybrid"],
                    }
                ],
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        models = client.get_models()
        assert "qwen36-coder" in models
        assert "qwen36-coder-hybrid" in models

    @patch("app.core.inference.urlopen")
    def test_get_models_http_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = HTTPError(
            url="http://ai.watchtower.local/v1/models",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=io.BytesIO(b'{"error": "Internal error"}'),
        )
        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="500"):
            client.get_models()

    @patch("app.core.inference.urlopen")
    def test_get_models_connection_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = URLError("Connection refused")
        client = VaultInferenceClient()
        with pytest.raises(InferenceConnectionError, match="Connection refused"):
            client.get_models()

    @patch("app.core.inference.urlopen")
    def test_get_models_malformed_json_raises(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"Not valid JSON"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="Malformed JSON"):
            client.get_models()

    @patch("app.core.inference.urlopen")
    def test_is_available(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"data": []}'
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        assert client.is_available() is True

        mock_urlopen.side_effect = URLError("Host down")
        assert client.is_available() is False


class TestVaultInferenceChat:
    @patch("app.core.inference.urlopen")
    def test_chat_completion_success(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        resp_data = {
            "id": "chatcmpl-test123",
            "model": "qwen36-coder",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Statement reconciled cleanly.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 5,
                "total_tokens": 17,
            },
            "timings": {
                "predicted_per_second": 52.1,
            },
        }
        mock_response.read.return_value = json.dumps(resp_data).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        result = client.chat_completion([{"role": "user", "content": "Hello"}])

        assert result["id"] == "chatcmpl-test123"
        assert (
            result["choices"][0]["message"]["content"]
            == "Statement reconciled cleanly."
        )

    @patch("app.core.inference.urlopen")
    def test_complete_helper(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Clean text",
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        reply = client.complete(
            "Extract details",
            system_prompt="You are a local Vault extractor.",
        )
        assert reply == "Clean text"

    def test_empty_messages_raises(self) -> None:
        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="messages must not be empty"):
            client.chat_completion([])

    @patch("app.core.inference.urlopen")
    def test_chat_http_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = HTTPError(
            url="http://ai.watchtower.local/v1/chat/completions",
            code=404,
            msg="Not Found",
            hdrs=MagicMock(),
            fp=io.BytesIO(b'{"error": {"message": "Model not found"}}'),
        )
        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="404"):
            client.chat_completion([{"role": "user", "content": "hi"}])

    @patch("app.core.inference.urlopen")
    def test_chat_connection_timeout(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = TimeoutError("Request timed out")
        client = VaultInferenceClient()
        with pytest.raises(InferenceConnectionError, match="timed out"):
            client.chat_completion([{"role": "user", "content": "hi"}])

    @patch("app.core.inference.urlopen")
    def test_chat_malformed_json_raises(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b"Not JSON"
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="Malformed JSON"):
            client.chat_completion([{"role": "user", "content": "hi"}])

    @patch("app.core.inference.urlopen")
    def test_complete_no_choices_raises(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"choices": []}'
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="returned no choices"):
            client.complete("hello")

    @patch("app.core.inference.urlopen")
    def test_complete_no_content_raises(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"choices": [{"message": {}}]}'
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        with pytest.raises(InferenceContractError, match="missing message content"):
            client.complete("hello")


class TestVaultInferencePII:
    def test_strip_pii_prompt_keep_deny_contract(self) -> None:
        """Policy kernel: keep Zelle/payee; redact SSN/account/check#; holder once."""
        prompt = STRIP_PII_SYSTEM_PROMPT
        # KEEP signal
        assert "Zelle" in prompt
        assert "payee" in prompt.lower()
        assert "activity-line" in prompt.lower() or "activity line" in prompt.lower()
        # DENY secrets
        assert "[REDACTED_SSN]" in prompt
        assert "[REDACTED_ACCOUNT]" in prompt
        assert "[REDACTED_CHECK]" in prompt
        assert "check numbers" in prompt.lower() or "check number" in prompt.lower()
        assert "[REDACTED_ADDRESS]" in prompt
        assert "[REDACTED_PHONE]" in prompt
        assert "[REDACTED_EMAIL]" in prompt
        # Holder name once — not blanket personal-name erasure
        assert "ONCE" in prompt
        assert "Account Holder" in prompt
        assert "personal names" not in prompt.lower()
        # Prefer keep when unsure
        assert "prefer KEEP" in prompt or "prefer keep" in prompt.lower()

    @patch("app.core.inference.urlopen")
    def test_strip_pii_sends_keep_deny_system_prompt(
        self, mock_urlopen: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "Account Holder: Jane Doe once\n"
                                "Zelle to Alex Rivera -50.00 "
                                "acct [REDACTED_ACCOUNT] SSN [REDACTED_SSN] "
                                "Check [REDACTED_CHECK]"
                            ),
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        synthetic = (
            "Account Holder: Jane Doe\n"
            "Prepared for: Jane Doe\n"
            "Zelle payment to Alex Rivera 50.00 "
            "SSN 123-45-6789 account 1234567890123456 Check #4521"
        )
        stripped = client.strip_pii(synthetic)
        assert "Zelle" in stripped or "Alex" in stripped
        assert "[REDACTED_ACCOUNT]" in stripped
        assert "[REDACTED_SSN]" in stripped
        assert "[REDACTED_CHECK]" in stripped

        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        system_msgs = [
            m["content"] for m in body["messages"] if m.get("role") == "system"
        ]
        assert system_msgs == [STRIP_PII_SYSTEM_PROMPT]
        assert "Zelle" in system_msgs[0]
        assert "[REDACTED_CHECK]" in system_msgs[0]

    @patch("app.core.inference.urlopen")
    def test_strip_pii_happy_path(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ACCOUNT [REDACTED_ACCOUNT] STATEMENT 2026-01-01",
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        stripped = client.strip_pii(
            "ACCOUNT 1234-5678-9012 JOHN DOE STATEMENT 2026-01-01"
        )
        assert "[REDACTED_ACCOUNT]" in stripped

    def test_strip_pii_empty_raises(self) -> None:
        client = VaultInferenceClient()
        with pytest.raises(ValueError, match="text must not be empty"):
            client.strip_pii("")

    def test_strip_pii_whitespace_only_raises(self) -> None:
        client = VaultInferenceClient()
        with pytest.raises(ValueError, match="text must not be empty"):
            client.strip_pii("   \n\t  ")

    def test_ingest_does_not_call_strip_pii_before_parse(self) -> None:
        """No full-LLM strip before adapter parse (locked Q7)."""
        from pathlib import Path

        ingest_src = (
            Path(__file__).resolve().parents[2]
            / "app"
            / "pipeline"
            / "ingest_service.py"
        ).read_text(encoding="utf-8")
        assert "strip_pii" not in ingest_src
        assert "parse_canonical" in ingest_src

    @patch("app.core.inference.urlopen")
    def test_extract_structured_happy_path(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"institution": "Chase", "account_mask": "1234"}',
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        data = client.extract_structured(
            text="Raw statement text",
            schema_description="Extract institution and account_mask as JSON",
        )
        assert data["institution"] == "Chase"
        assert data["account_mask"] == "1234"

    @patch("app.core.inference.urlopen")
    def test_extract_structured_invalid_json_raises(
        self, mock_urlopen: MagicMock
    ) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Not valid JSON output from model",
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        with pytest.raises(
            InferenceContractError, match="Failed to parse structured JSON"
        ):
            client.extract_structured(
                text="Raw statement text",
                schema_description="Extract JSON",
            )

    def test_extract_structured_empty_text_raises(self) -> None:
        client = VaultInferenceClient()
        with pytest.raises(ValueError, match="text must not be empty"):
            client.extract_structured(text="", schema_description="Extract JSON")

    def test_extract_structured_empty_schema_raises(self) -> None:
        client = VaultInferenceClient()
        with pytest.raises(ValueError, match="schema_description must not be empty"):
            client.extract_structured(text="Raw text", schema_description="")

    @patch("app.core.inference.urlopen")
    def test_extract_structured_markdown_fence(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '```json\n{"status": "ok", "count": 42}\n```',
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        data = client.extract_structured(
            text="Raw text",
            schema_description="Extract status and count",
        )
        assert data == {"status": "ok", "count": 42}

    @patch("app.core.inference.urlopen")
    def test_extract_structured_list_json_raises(self, mock_urlopen: MagicMock) -> None:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "[1, 2, 3]",
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        client = VaultInferenceClient()
        with pytest.raises(
            InferenceContractError, match="Extracted JSON root must be an object"
        ):
            client.extract_structured(
                text="Raw text",
                schema_description="Extract list",
            )


class TestVaultInferenceObservabilityAndPrivacy:
    @patch("app.core.inference.urlopen")
    def test_zero_pii_in_logs(self, mock_urlopen: MagicMock) -> None:
        sensitive_text = (
            "SSN: 000-12-3456 Account: 987654321 Name: Brian Amount: $5,432.10"
        )
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [{"message": {"role": "assistant", "content": "Done"}}],
                "usage": {"total_tokens": 15},
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        log_capture = io.StringIO()
        handler = logging.StreamHandler(log_capture)
        handler.setFormatter(JSONFormatter())

        logger = logging.getLogger("app.core.inference")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

        try:
            client = VaultInferenceClient()
            client.complete(sensitive_text)

            log_output = log_capture.getvalue()
            # Assert sensitive prompt is NOT leaked in logs
            assert "000-12-3456" not in log_output
            assert "987654321" not in log_output
            assert "$5,432.10" not in log_output
            # Operational metrics ARE logged
            assert "model" in log_output
            assert "qwen36-coder-hybrid" in log_output
        finally:
            logger.removeHandler(handler)


class TestExceptionSubclasses:
    def test_inheritance(self) -> None:
        assert issubclass(InferenceError, PipelineError)
        assert issubclass(InferenceConnectionError, InferenceError)
        assert issubclass(InferenceContractError, InferenceError)
