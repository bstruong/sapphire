#!/usr/bin/env python3
"""QA: PII redaction contract (P1) — strip_pii keep/deny prompt.

Happy: system prompt keeps Zelle/payee; redacts SSN/account/check#; holder once.
Happy: mocked strip_pii response preserves Zelle payee while tokenizing secrets.
Negative: empty / whitespace-only strip_pii fails loud.
Negative: ingest_service source must not call strip_pii before parse.

Offline-safe (mocks JARVIS). No dogfood wipe.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def main() -> int:
    print("=" * 70)
    print("QA: PII redaction contract (strip_pii keep/deny)")
    print("=" * 70)
    failures = 0

    from app.core.inference import STRIP_PII_SYSTEM_PROMPT, VaultInferenceClient

    prompt = STRIP_PII_SYSTEM_PROMPT
    prompt_l = prompt.lower()
    keep_ok = (
        "Zelle" in prompt
        and "payee" in prompt_l
        and "activity-line" in prompt_l
        and "ONCE" in prompt
        and "Account Holder" in prompt
    )
    deny_needles = (
        "[REDACTED_SSN]",
        "[REDACTED_ACCOUNT]",
        "[REDACTED_CHECK]",
        "[REDACTED_ADDRESS]",
        "[REDACTED_PHONE]",
        "[REDACTED_EMAIL]",
    )
    missing_deny = [n for n in deny_needles if n not in prompt]
    if not keep_ok:
        _fail("prompt missing keep (Zelle/payee/activity-line/ONCE/Account Holder)")
        failures += 1
    elif missing_deny:
        _fail(f"prompt missing deny tokens: {missing_deny}")
        failures += 1
    elif "personal names" in prompt_l:
        _fail("prompt still uses blanket 'personal names' hammer")
        failures += 1
    else:
        _pass("system prompt keep/deny contract (Zelle/payee keep; secrets deny)")

    client = VaultInferenceClient()
    try:
        client.strip_pii("")
        _fail("empty strip_pii did not fail loud")
        failures += 1
    except ValueError:
        _pass("empty strip_pii fails loud")

    try:
        client.strip_pii("   ")
        _fail("whitespace strip_pii did not fail loud")
        failures += 1
    except ValueError:
        _pass("whitespace strip_pii fails loud")

    synthetic_in = (
        "Account Holder: Jane Doe\n"
        "Zelle payment to Alex Rivera 50.00 "
        "SSN 123-45-6789 account 1234567890123456 Check #4521"
    )
    synthetic_out = (
        "Account Holder: Jane Doe\n"
        "Zelle payment to Alex Rivera 50.00 "
        "SSN [REDACTED_SSN] account [REDACTED_ACCOUNT] Check [REDACTED_CHECK]"
    )
    with patch("app.core.inference.urlopen") as mock_urlopen:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": synthetic_out,
                        }
                    }
                ]
            }
        ).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response
        out = client.strip_pii(synthetic_in)
        request = mock_urlopen.call_args[0][0]
        body = json.loads(request.data.decode("utf-8"))
        system = next(m["content"] for m in body["messages"] if m["role"] == "system")

    if system != STRIP_PII_SYSTEM_PROMPT:
        _fail("strip_pii did not send STRIP_PII_SYSTEM_PROMPT")
        failures += 1
    else:
        _pass("strip_pii sends keep/deny system prompt")

    if "Alex Rivera" not in out or "Zelle" not in out:
        _fail("Zelle/person payee not preserved in sanitized output")
        failures += 1
    elif "[REDACTED_SSN]" not in out or "[REDACTED_ACCOUNT]" not in out:
        _fail("SSN/account not redacted in sanitized output")
        failures += 1
    elif "[REDACTED_CHECK]" not in out:
        _fail("check number not redacted in sanitized output")
        failures += 1
    else:
        _pass("mocked sanitize keeps Zelle payee; redacts SSN/account/check#")

    ingest_src = (ROOT / "app" / "pipeline" / "ingest_service.py").read_text(
        encoding="utf-8"
    )
    if "strip_pii" in ingest_src:
        _fail("ingest_service calls strip_pii (forbidden before parse)")
        failures += 1
    elif "parse_canonical" not in ingest_src:
        _fail("ingest_service missing parse_canonical")
        failures += 1
    else:
        _pass("no LLM strip_pii before adapter parse (ingest)")

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
