"""Vault local inference client for SAPPHIRE.

Communicates with **JARVIS**, the shared Watchtower inference engine (k3s
namespace/deployment `jarvis`/`jarvis`) via its OpenAI-compatible HTTP API.

Default URL is the Traefik ingress (`ai.watchtower.local`). On the Watchtower
node use `http://127.0.0.1:8080/v1`; in-cluster use
`http://jarvis.jarvis.svc.cluster.local:8080/v1`. Do not use LAN `:8080`
(hostPort is loopback-only). Exclusive GPU work: see
`homelab-k3s/scripts/jarvis-lease.sh`.

Guarantees:
- Single Vault trust boundary: raw statements and PII remain inside the Vault.
- Zero local compute on developer workstations (all inference executes on Watchtower).
- Zero PII egress and zero PII logging: prompts, messages, statement text, and
  financial amounts are never written to logs.
- Fail-loud contract enforcement: broken endpoints, HTTP errors, and malformed
  payloads raise explicit InferenceError subclasses.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.models.exceptions import (
    InferenceConnectionError,
    InferenceContractError,
    InferenceError,
)

logger = logging.getLogger("app.core.inference")

_DEFAULT_VAULT_URL = "http://ai.watchtower.local/v1"
_DEFAULT_MODEL = "qwen36-coder-hybrid"
_DEFAULT_TIMEOUT_SECONDS = 30.0

_JSON_FENCE_PATTERN = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```")

# Sanitize-role policy kernel (P1). Used for egress / optional assist only —
# never as a full-LLM strip before adapter parse_canonical.
STRIP_PII_SYSTEM_PROMPT = (
    "You are a local financial redaction model operating inside a strict Vault boundary. "
    "Sanitize the text by replacing DENY-list identifiers with tokens; KEEP transaction signal. "
    "DENY — replace with tokens: "
    "SSN/ITIN → [REDACTED_SSN]; "
    "full account/routing/card PAN digits → [REDACTED_ACCOUNT]; "
    "street addresses → [REDACTED_ADDRESS]; "
    "telephone numbers → [REDACTED_PHONE]; "
    "emails → [REDACTED_EMAIL]; "
    "check numbers and confirmation IDs → [REDACTED_CHECK]; "
    "account-holder legal name in header chrome may appear ONCE "
    "(Account Holder / Prepared for); any repeated holder-name spam → [REDACTED_NAME]. "
    "KEEP unaltered: dates, amounts, balances, table structure, institution names, "
    "activity-line descriptions, and payee/merchant/memo text including Zelle and "
    "person-shaped payees printed on activity lines. "
    "If unsure whether a name is header identity vs payee: prefer KEEP on activity lines; "
    "redact only clearly labeled Account Holder / Prepared for / Address chrome "
    "(except the single show-once holder name). "
    "Do not invent merchants. Do not erase payees to be helpful. "
    "Return only the sanitized text."
)


class VaultInferenceClient:
    """Client for JARVIS (Watchtower shared inference) within the Vault trust boundary."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        if base_url is not None:
            if not base_url.strip():
                raise ValueError("base_url must not be empty.")
            raw_url = base_url
        else:
            raw_url = (
                os.environ.get("VAULT_INFERENCE_URL")
                or os.environ.get("LOCAL_LLM_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or _DEFAULT_VAULT_URL
            )
        if not raw_url.strip():
            raise ValueError("base_url must not be empty.")
        self.base_url = raw_url.strip().rstrip("/")

        if model is not None:
            if not model.strip():
                raise ValueError("model must not be empty.")
            raw_model = model
        else:
            raw_model = (
                os.environ.get("VAULT_INFERENCE_MODEL")
                or os.environ.get("LOCAL_LLM_MODEL")
                or _DEFAULT_MODEL
            )
        if not raw_model.strip():
            raise ValueError("model must not be empty.")
        self.model = raw_model.strip()

        if timeout is not None:
            self.timeout = float(timeout)
        else:
            env_timeout = os.environ.get("VAULT_INFERENCE_TIMEOUT")
            self.timeout = (
                float(env_timeout) if env_timeout else _DEFAULT_TIMEOUT_SECONDS
            )

        if self.timeout <= 0:
            raise ValueError("timeout must be positive.")

    def is_available(self) -> bool:
        """Check whether the Watchtower inference engine is reachable."""
        try:
            self.get_models()
            return True
        except (InferenceError, OSError):
            return False

    def get_models(self) -> list[str]:
        """Query available models from the Watchtower inference engine.

        Returns:
            List of model identifiers and aliases.

        Raises:
            InferenceConnectionError: If network connectivity or timeout fails.
            InferenceContractError: If the server returns an error or invalid payload.
        """
        url = f"{self.base_url}/models"
        req = Request(url, headers={"Accept": "application/json"})

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            with contextlib.suppress(Exception):
                exc.close()
            logger.warning(
                "Watchtower models endpoint returned HTTP error",
                extra={"status_code": exc.code, "endpoint": url},
            )
            raise InferenceContractError(
                f"Watchtower models query returned HTTP {exc.code}: {exc.reason}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            logger.warning(
                "Watchtower models endpoint connection failed",
                extra={"error_type": type(exc).__name__, "endpoint": url},
            )
            raise InferenceConnectionError(
                f"Failed to connect to Watchtower inference at {url}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise InferenceContractError(
                f"Malformed JSON from Watchtower models endpoint: {exc}"
            ) from exc

        models: list[str] = []
        # Support both OpenAI standard "data" and llama.cpp "models" keys
        items = data.get("data") or data.get("models") or []
        for item in items:
            if isinstance(item, dict):
                model_id = item.get("id") or item.get("name")
                if model_id and model_id not in models:
                    models.append(str(model_id))
                for alias in item.get("aliases", []):
                    if alias and str(alias) not in models:
                        models.append(str(alias))
        return models

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
        validate_model: bool = False,
    ) -> dict[str, Any]:
        """Execute a chat completion on the Watchtower inference engine.

        Guarantees zero PII logging: prompt and response contents are never
        logged. Only operational telemetry (model, duration, token count) is recorded.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            model: Optional model override. Defaults to client model.
            temperature: Sampling temperature (default 0.1 for deterministic extraction).
            max_tokens: Max completion tokens.
            validate_model: If True, asserts the model is present in server model catalog.

        Returns:
            OpenAI-compatible chat completion response dictionary.

        Raises:
            InferenceContractError: On invalid input or server-side HTTP/API error.
            InferenceConnectionError: On timeout or network failure.
        """
        if not messages:
            raise InferenceContractError("messages must not be empty.")

        target_model = model or self.model
        if validate_model:
            available_models = self.get_models()
            if target_model not in available_models:
                raise InferenceContractError(
                    f"Model {target_model!r} not found on Watchtower inference engine. Available: {available_models}"
                )

        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        start_time = time.monotonic()
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                status_code = resp.status
        except HTTPError as exc:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.warning(
                "Watchtower chat completion returned HTTP error",
                extra={
                    "status_code": exc.code,
                    "model": target_model,
                    "duration_ms": duration_ms,
                },
            )
            error_body = ""
            with contextlib.suppress(Exception):
                error_body = exc.read().decode("utf-8")
            with contextlib.suppress(Exception):
                exc.close()
            raise InferenceContractError(
                f"Watchtower inference HTTP {exc.code}: {exc.reason}. Detail: {error_body}"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            duration_ms = round((time.monotonic() - start_time) * 1000, 2)
            logger.warning(
                "Watchtower chat completion connection failed",
                extra={
                    "error_type": type(exc).__name__,
                    "model": target_model,
                    "duration_ms": duration_ms,
                },
            )
            raise InferenceConnectionError(
                f"Connection to Watchtower inference at {url} failed: {exc}"
            ) from exc

        duration_ms = round((time.monotonic() - start_time) * 1000, 2)
        try:
            result = json.loads(resp_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise InferenceContractError(
                f"Malformed JSON from Watchtower chat completion: {exc}"
            ) from exc

        # Observability: log telemetry without leaking PII or prompt text
        usage = result.get("usage", {})
        logger.info(
            "Watchtower chat completion executed successfully",
            extra={
                "model": target_model,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            },
        )
        return result

    def complete(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> str:
        """High-level completion helper returning the assistant message string.

        Args:
            prompt: User prompt.
            system_prompt: Optional system prompt.
            model: Optional model override.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens to generate.

        Returns:
            The text response generated by the model.
        """
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = self.chat_completion(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        choices = response.get("choices", [])
        if not choices:
            raise InferenceContractError("Inference response returned no choices.")
        message = choices[0].get("message", {})
        content = message.get("content")
        if content is None:
            raise InferenceContractError("Inference choice missing message content.")
        if not str(content).strip() and message.get("reasoning_content"):
            return str(message.get("reasoning_content"))
        return str(content)

    def strip_pii(self, text: str) -> str:
        """Sanitize text inside the Vault (sanitize role / optional assist).

        Redacts identity secrets (SSN, full account, check #, address, phone,
        email; holder name once in chrome) while keeping payee/Zelle/activity
        signal. Not a pre-parse gate — adapters parse raw Vault PDFs without
        a full-LLM strip first.

        Args:
            text: Statement or page text to sanitize.

        Returns:
            Text with DENY-list identifiers tokenized; KEEP-list signal intact.
        """
        stripped = text.strip() if text else ""
        if not stripped:
            raise ValueError("text must not be empty.")

        return self.complete(stripped, system_prompt=STRIP_PII_SYSTEM_PROMPT)

    def extract_structured(
        self,
        text: str,
        schema_description: str,
    ) -> dict[str, Any]:
        """Extract structured JSON data from text within the Vault trust boundary.

        Args:
            text: Input document or table text.
            schema_description: Description of the expected JSON fields and types.

        Returns:
            Parsed dictionary matching the requested schema.

        Raises:
            InferenceContractError: If the model output cannot be parsed as valid JSON.
        """
        stripped_text = text.strip() if text else ""
        if not stripped_text:
            raise ValueError("text must not be empty.")

        stripped_schema = schema_description.strip() if schema_description else ""
        if not stripped_schema:
            raise ValueError("schema_description must not be empty.")

        system_prompt = (
            "You are a financial extraction engine operating inside the SAPPHIRE Vault. "
            f"Extract the requested data strictly following this schema:\n{stripped_schema}\n"
            "Return ONLY a valid JSON object. Do not wrap in conversational text or commentary."
        )
        raw_output = self.complete(stripped_text, system_prompt=system_prompt)

        # Attempt direct JSON parse or extract from markdown fence
        candidate = raw_output.strip()
        fence_match = _JSON_FENCE_PATTERN.search(candidate)
        if fence_match:
            candidate = fence_match.group(1).strip()

        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                raise TypeError("Extracted JSON root must be an object.")
            return parsed
        except (json.JSONDecodeError, TypeError) as exc:
            raise InferenceContractError(
                f"Failed to parse structured JSON from model output: {exc}. Output was: {raw_output[:200]}"
            ) from exc
