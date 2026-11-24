# Payee signal inventory (category spend step 1)

**Status:** Built (docs + fixture QA)  
**Pitch:** [`pitch_category_spend_pie.md`](pitch_category_spend_pie.md)  
**Upstream:** Metrotower dogfood analysis (ACRA-16) — Schwab checking PDFs store opaque activity lines; no wipe/re-seed.

---

## Policy (locked for this bet)

1. **Never invent a payee.** If the source row has no counterparty text, `payee` stays `NULL` (or description stays the opaque bank type string). Opaque rows may still receive a **manual** spend label later; they must not get hallucinated merchants.
2. **Pipeline `transaction_category` is not a payee.** `withdrawal` / `deposit` / … stay ledger tags.
3. **PDF statement adapters are the only ingest path today.** No OFX / QFX / CSV export adapter is shipped. Prefer adding an export path in a later PR if PDFs stay opaque — do not fake payees from amount rhythm.
4. **Dogfood:** inventory and later ingest changes must not wipe Metrotower personal data. Re-parse of stored PDFs is opt-in and called out in Manual UI QA when payee ingest ships.

---

## Source matrix

| Source | Adapter | Counterparty signal today | Payee for pie? | Notes |
|--------|---------|---------------------------|----------------|-------|
| Schwab Bank Investor Checking **PDF** | `schwab_checking` | Almost none — activity lines are bank type strings (`Electronic Withdrawal`, `Electronic Deposit`, `Interest Paid`, occasional Zelle/ACH/Wire/Check chrome) | **No** until richer export or statement layout with merchant | Redacted fixture: 15 txns → 3 unique descriptions (`Electronic Withdrawal` ×13). Dogfood (ACRA-16): 141 txns → 14 unique; `Electronic Withdrawal` ×100 |
| Schwab brokerage **PDF** | `schwab` | Activity descriptions are transfer/dividend/fee prose, not merchant spend | Weak / N/A for spend pie | Cash & portfolio bridge; not grocery/rent taxonomy |
| Chase **credit card** PDF | `chase_credit_card` | **Yes** — merchant-ish `description` (`TING* …`, `SELLIT.COM …`, payment thank-you lines) | **Yes** (use description as payee candidate; do not invent beyond printed text) | Probe flag `has_merchant_txn_header: true`. Fixture: 3 unique merchant/payment strings |
| Generic depository / credit fixtures | `depository` / `credit_card` | Synthetic descriptions often include merchant-ish text (`Grocery Store Purchase`) | Synthetic only | Good for QA; not production institutions |
| Chase checking (JSON fixture only) | depository path | `SYNTHETIC Payroll Deposit` etc. | Synthetic | No production Chase checking adapter bet in this series |
| OFX / QFX / CSV export | — | Not implemented | Future | Best candidate if Schwab PDF stays opaque |

---

## Opaque-row policy

| Situation | Product behavior |
|-----------|------------------|
| Description is a bank type only (`Electronic Withdrawal`) | Payee = absent. Spend label = optional manual only. No Vault LLM invent. |
| Description already includes merchant/payee text (Chase card) | Payee candidate = that printed text (trimmed). Fail loud if empty string after trim. |
| Ambiguous multi-field row (type + payee) | Prefer nullable `payee` column (plan option A2) so raw `description` stays audit trail; formatting `Type — Payee` into description (A1) is acceptable only if adapters never drop the raw type. |

---

## Implications for next PRs

1. **Payee ingest (step 2):** nullable `transactions.payee` + `CanonicalTransaction.payee: str | None` via migration `0003_transactions_payee.sql` and `app.adapters.payee.payee_from_description`. Chase fills payee from printed description; Schwab checking leaves `NULL` for opaque types.
2. **Manual labels (step 3):** allowed on opaque rows (PM memory / amount context) — labels do not require payee.
3. **Rules / Vault suggest (steps 4 / 6):** require non-null payee; skip opaque rows loudly (no silent invent).
4. **Pie (step 5):** unlabeled spend → **Uncategorized** bucket regardless of payee presence.

---

## Executable QA

```bash
uv run python scripts/qa_payee_signal_inventory.py
```

Asserts redacted Chase vs Schwab PDF description entropy and the opaque-row policy against fixtures (no DB wipe).

## Manual UI QA

not applicable (no user-visible UI). Open this doc + run the QA script.
