#!/usr/bin/env python3
"""QA Verification Script: JARVIS (Watchtower shared inference) Integration.

Verifies the SAPPHIRE Vault interface to JARVIS
(default http://ai.watchtower.local/v1; on-node http://127.0.0.1:8080/v1):
  1. Live connectivity and model discovery against the JARVIS endpoint.
  2. Completion, PII stripping, and structured JSON extraction inside the Vault boundary.
  3. Strict zero-PII logging verification.
  4. Intentional negative failure cases (unreachable host, invalid model, empty payloads).
  5. Strict exception taxonomy (InferenceError -> PipelineError).

Fail-soft contract:
  - Endpoint **down**: skip live checks, still assert offline taxonomy → exit 0.
  - Endpoint **up** + any contract violation → exit non-zero.
  - Endpoint **up** + all checks pass → exit 0.

Usage:
    uv run python scripts/qa_watchtower_inference.py
"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.core.inference import VaultInferenceClient
from app.core.telemetry import JSONFormatter
from app.models.exceptions import (
    InferenceConnectionError,
    InferenceContractError,
    InferenceError,
    PipelineError,
)


class QAReport:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}" + (f": {detail}" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  [FAIL] {name}" + (f": {detail}" if detail else ""))

    def skip(self, name: str, detail: str = "") -> None:
        self.skipped.append(name)
        print(f"  [SKIP] {name}" + (f": {detail}" if detail else ""))

    def summary(self, *, endpoint_online: bool) -> bool:
        total = len(self.passed) + len(self.failed) + len(self.skipped)
        print(f"\n{'=' * 65}")
        print(
            f"QA Summary: {len(self.passed)} passed, "
            f"{len(self.failed)} failed, {len(self.skipped)} skipped "
            f"(of {total})"
        )
        if not endpoint_online:
            print(
                "Endpoint offline — fail-soft mode "
                "(live checks skipped; offline contracts still enforced)."
            )
        if self.failed:
            print(f"Failed checks ({len(self.failed)}):")
            for name in self.failed:
                print(f"  - {name}")
            return False
        if endpoint_online:
            print("All Watchtower inference checks passed!")
        else:
            print("Offline contract checks passed (fail-soft).")
        return True


def main() -> int:
    report = QAReport()

    print("=" * 65)
    print("  SAPPHIRE Vault: Watchtower Inference Engine QA Verification")
    print("=" * 65)

    client = VaultInferenceClient()
    print(f"\nTarget Endpoint: {client.base_url}")
    print(f"Default Model:   {client.model}")
    print(f"Default Timeout: {client.timeout}s\n")

    # ---------------------------------------------------------
    # Feature 1: Connectivity & Model Discovery
    # ---------------------------------------------------------
    print("Feature 1: Connectivity & Model Discovery (Happy Path)")
    print("-" * 65)

    is_avail = client.is_available()
    if is_avail:
        report.record(
            "Watchtower inference endpoint is reachable",
            True,
            f"url={client.base_url}",
        )
    else:
        report.skip(
            "Watchtower inference endpoint is reachable",
            f"offline url={client.base_url} (fail-soft)",
        )

    models: list[str] = []
    if is_avail:
        try:
            models = client.get_models()
            report.record(
                "Model discovery returned available models",
                len(models) > 0,
                f"discovered={models}",
            )
            has_qwen = any("qwen" in m.lower() for m in models)
            report.record(
                "Active model is present in model catalog",
                has_qwen,
                f"matches={models}",
            )
        except Exception as exc:  # noqa: BLE001
            report.record("Model discovery succeeded", False, str(exc))
    else:
        report.skip("Model discovery", "endpoint offline")

    # ---------------------------------------------------------
    # Feature 2: Vault Local Model Completion & Extraction
    # ---------------------------------------------------------
    print("\nFeature 2: Vault Inference Capabilities (Happy Path)")
    print("-" * 65)

    if is_avail:
        try:
            reply = client.complete(
                "Echo exactly: 'Vault inference online.'",
                max_tokens=256,
            )
            report.record(
                "Completion executed successfully",
                len(reply.strip()) > 0,
                f"reply={reply.strip()[:60]!r}",
            )
        except Exception as exc:  # noqa: BLE001
            report.record("Completion executed successfully", False, str(exc))

        try:
            structured = client.extract_structured(
                text="Account statement for ACME checking mask 4455 opening $100 closing $200",
                schema_description="JSON object with keys: institution (string), account_mask (string)",
            )
            report.record(
                "Structured JSON extraction parsed cleanly",
                isinstance(structured, dict) and "account_mask" in structured,
                f"structured={structured}",
            )
        except Exception as exc:  # noqa: BLE001
            report.record("Structured JSON extraction parsed cleanly", False, str(exc))
    else:
        report.skip("Completion", "endpoint offline")
        report.skip("Structured extraction", "endpoint offline")

    # ---------------------------------------------------------
    # Feature 3: Privacy & Zero-PII Logging Invariant
    # ---------------------------------------------------------
    print("\nFeature 3: Privacy & Zero-PII Logging Invariant")
    print("-" * 65)

    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setFormatter(JSONFormatter())
    inf_logger = logging.getLogger("app.core.inference")
    inf_logger.setLevel(logging.INFO)
    inf_logger.addHandler(handler)

    sensitive_account = "CONFIDENTIAL-ACCT-987654321"
    sensitive_ssn = "SSN-999-00-1111"
    try:
        if is_avail:
            client.complete(
                f"Sanitize check for {sensitive_account} and {sensitive_ssn}",
                max_tokens=10,
            )
        log_content = log_capture.getvalue()
        report.record(
            "Log records do NOT leak sensitive account tokens",
            sensitive_account not in log_content,
        )
        report.record(
            "Log records do NOT leak SSN tokens",
            sensitive_ssn not in log_content,
        )
        if is_avail:
            report.record(
                "Log records contain operational telemetry (model/status)",
                "model" in log_content,
            )
        else:
            report.skip(
                "Log telemetry while endpoint online",
                "endpoint offline (no live completion logged)",
            )
    finally:
        inf_logger.removeHandler(handler)

    # ---------------------------------------------------------
    # Feature 4: Fail-Loud Contract Enforcement (Negative Verification)
    # ---------------------------------------------------------
    print("\nFeature 4: Fail-Loud Contract Enforcement (Negative Verification)")
    print("-" * 65)

    # 4.1 Unreachable host / port (always — offline-safe)
    broken_client = VaultInferenceClient(
        base_url="http://192.168.1.92:9999/v1", timeout=1.0
    )
    try:
        broken_client.get_models()
        report.record("Unreachable endpoint rejected loud", False)
    except InferenceConnectionError:
        report.record(
            "Unreachable endpoint rejected loud (InferenceConnectionError)", True
        )
    except Exception as exc:  # noqa: BLE001
        report.record(
            "Unreachable endpoint rejected loud",
            False,
            f"wrong exception: {type(exc).__name__}",
        )

    # 4.2 Non-existent model
    if is_avail:
        try:
            client.chat_completion(
                [{"role": "user", "content": "test"}],
                model="nonexistent-model-definitely-missing-xyz",
                validate_model=True,
            )
            report.record("Nonexistent model rejected loud", False)
        except InferenceContractError:
            report.record(
                "Nonexistent model rejected loud (InferenceContractError)", True
            )
        except Exception as exc:  # noqa: BLE001
            report.record(
                "Nonexistent model rejected loud",
                False,
                f"wrong exception: {type(exc).__name__}",
            )
    else:
        report.skip("Nonexistent model rejection", "endpoint offline")

    # 4.3 Empty input payloads (always — offline-safe)
    try:
        client.chat_completion([])
        report.record("Empty messages rejected loud", False)
    except InferenceContractError:
        report.record("Empty messages rejected loud (InferenceContractError)", True)

    try:
        client.strip_pii("   ")
        report.record("Empty text to strip_pii rejected loud", False)
    except ValueError:
        report.record("Empty text to strip_pii rejected loud (ValueError)", True)

    try:
        client.extract_structured(text="valid", schema_description="")
        report.record("Empty schema_description rejected loud", False)
    except ValueError:
        report.record("Empty schema_description rejected loud (ValueError)", True)

    # ---------------------------------------------------------
    # Feature 5: Invariant Exception Hierarchy
    # ---------------------------------------------------------
    print("\nFeature 5: Exception Taxonomy & Invariant Contracts")
    print("-" * 65)

    report.record(
        "InferenceError is a subclass of PipelineError",
        issubclass(InferenceError, PipelineError),
    )
    report.record(
        "InferenceConnectionError is a subclass of InferenceError",
        issubclass(InferenceConnectionError, InferenceError),
    )
    report.record(
        "InferenceContractError is a subclass of InferenceError",
        issubclass(InferenceContractError, InferenceError),
    )

    return 0 if report.summary(endpoint_online=is_avail) else 1


if __name__ == "__main__":
    sys.exit(main())
