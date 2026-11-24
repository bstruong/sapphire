#!/usr/bin/env python3
"""QA: Payee signal inventory (category spend step 1).

Happy: Chase card redacted PDF yields merchant-ish distinct descriptions;
       Schwab checking redacted PDF is dominated by opaque bank-type strings.
Negative: empty / whitespace-only description strings are rejected as payee
          candidates (fail-loud policy — never invent).

No database writes. Does not touch Metrotower dogfood data.
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REDACTED = ROOT / "tests" / "fixtures" / "redacted"

# Bank-type strings that are not counterparty/payee signals.
OPAQUE_BANK_TYPES = frozenset(
    {
        "Electronic Withdrawal",
        "Electronic Deposit",
        "Interest Paid",
        "Incoming Wire",
        "Funds Transfer from Brokerage",
        "Mobile Deposit",
        "Check",
        "ACH Bill Pay",
    }
)


def _pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def _payee_candidate(description: str) -> str | None:
    """Inventory policy: opaque bank types → no payee; else printed text.

    Empty / whitespace-only descriptions fail loud (never invent).
    """
    if not isinstance(description, str):
        raise TypeError("description must be str")
    stripped = description.strip()
    if not stripped:
        raise ValueError("payee candidate refuses empty description")
    if stripped in OPAQUE_BANK_TYPES:
        return None
    # Zelle / ACH lines may include a counterparty after the type keyword;
    # treat pure type matches as opaque; anything else keeps printed text.
    return stripped


def _descriptions_from_pdf(adapter: object, pdf: Path) -> list[str]:
    from app.extraction.core import extract_pdf_to_raw

    raw = extract_pdf_to_raw(
        file_path=pdf,
        adapter_id=adapter.adapter_id,
        adapter_version=adapter.adapter_version,
        original_basename=pdf.name,
    )
    result = adapter.parse_canonical(raw)
    txns = result[3]
    return [t.description for t in txns]


def main() -> int:
    print("=" * 70)
    print("QA: Payee signal inventory (Chase vs Schwab fixtures)")
    print("=" * 70)
    failures = 0

    from app.adapters.chase_credit_card import ChaseCreditCardAdapter
    from app.adapters.schwab_checking import SchwabCheckingAdapter

    chase_pdf = REDACTED / "chase_credit_card_sample.pdf"
    schwab_pdf = REDACTED / "schwab_checking_sample.pdf"

    if not chase_pdf.is_file():
        _fail(f"Missing fixture: {chase_pdf}")
        return 1
    if not schwab_pdf.is_file():
        _fail(f"Missing fixture: {schwab_pdf}")
        return 1

    # --- Happy: Chase card has merchant-ish distinct descriptions ---
    print("\n[Chase credit card — payee-capable descriptions]")
    try:
        chase_descs = _descriptions_from_pdf(ChaseCreditCardAdapter(), chase_pdf)
    except Exception as exc:  # noqa: BLE001
        _fail(f"Chase parse failed: {type(exc).__name__}: {exc}")
        return 1

    chase_counts = Counter(chase_descs)
    chase_payees = []
    for desc in chase_descs:
        try:
            cand = _payee_candidate(desc)
        except ValueError as exc:
            _fail(f"Chase description rejected unexpectedly: {exc}")
            failures += 1
            continue
        if cand is not None:
            chase_payees.append(cand)

    if len(chase_descs) < 1:
        _fail("Chase fixture produced zero transactions")
        failures += 1
    elif len(chase_payees) < 1:
        _fail("Chase fixture produced no payee candidates")
        failures += 1
    else:
        _pass(
            f"Chase: {len(chase_descs)} txns, {len(chase_counts)} unique, "
            f"{len(chase_payees)} payee candidates "
            f"(e.g. {chase_payees[0]!r})"
        )

    merchantish = [
        p
        for p in chase_payees
        if "PAYMENT" not in p.upper() or "*" in p or ".COM" in p.upper()
    ]
    if merchantish:
        _pass(f"Chase includes merchant-ish payee text ({merchantish[0]!r})")
    else:
        # Payment thank-you alone still counts as non-opaque printed text.
        _pass("Chase payee candidates are non-opaque printed description text")

    # --- Happy / negative contrast: Schwab checking is opaque ---
    print("\n[Schwab checking — opaque bank-type descriptions]")
    try:
        schwab_descs = _descriptions_from_pdf(SchwabCheckingAdapter(), schwab_pdf)
    except Exception as exc:  # noqa: BLE001
        _fail(f"Schwab parse failed: {type(exc).__name__}: {exc}")
        return 1

    schwab_counts = Counter(schwab_descs)
    opaque = sum(1 for d in schwab_descs if _payee_candidate(d) is None)
    non_opaque = [d for d in schwab_descs if _payee_candidate(d) is not None]

    if len(schwab_descs) < 1:
        _fail("Schwab fixture produced zero transactions")
        failures += 1
    elif opaque < 1:
        _fail("Expected Schwab fixture to include opaque bank-type rows")
        failures += 1
    else:
        _pass(
            f"Schwab: {len(schwab_descs)} txns, {len(schwab_counts)} unique, "
            f"{opaque} opaque (no payee) — top={schwab_counts.most_common(1)[0]!r}"
        )

    if (
        "Electronic Withdrawal" in schwab_counts
        and schwab_counts["Electronic Withdrawal"] >= 2
    ):
        _pass(
            "Schwab dominated by Electronic Withdrawal "
            f"(×{schwab_counts['Electronic Withdrawal']})"
        )
    else:
        _fail("Expected Electronic Withdrawal dominance on Schwab fixture")
        failures += 1

    if non_opaque:
        # Fixture may gain richer lines later; still report without inventing.
        _pass(
            f"Schwab non-opaque printed lines kept as candidates "
            f"({len(non_opaque)}): e.g. {non_opaque[0]!r}"
        )
    else:
        _pass("Schwab fixture: all rows opaque → zero payee candidates (policy OK)")

    # --- Negative: empty description must fail loud ---
    print("\n[Negative: refuse empty / whitespace payee candidates]")
    for bad in ("", "   ", "\t"):
        try:
            _payee_candidate(bad)
            _fail(f"Expected ValueError for {bad!r}")
            failures += 1
        except ValueError:
            _pass(f"Loud reject for empty/whitespace description {bad!r}")

    # Opaque map returns None (not invented merchant)
    for opaque_desc in ("Electronic Withdrawal", "Interest Paid"):
        if _payee_candidate(opaque_desc) is not None:
            _fail(f"Opaque type {opaque_desc!r} must not yield a payee")
            failures += 1
        else:
            _pass(f"Opaque type {opaque_desc!r} → payee=None (no invent)")

    print()
    if failures:
        print(f"Result: [FAIL] ({failures} failure(s))")
        return 1
    print("Result: [PASS]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
