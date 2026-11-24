# PII redaction contract (Vault sanitize)

**Status:** Locked with Brian (plan Q1–Q12); implemented in `STRIP_PII_SYSTEM_PROMPT` (`app/core/inference.py`).  
**Scope:** Sanitize-role / optional assist / egress policy. **Not** a full-LLM strip before `adapter.parse_canonical`.

## Keep (Vault ledger, Tier-1 MCP, rules/suggest)

- Payee / counterparty (merchant, **Zelle / person payee**, bill-pay payee as printed)
- Activity-line description / memo (after account/SSN/check scrub)
- Integer `amount_cents`, dates, balances, table structure, institution names
- Account mask (`*XXXX`)
- Account-holder legal name **once** in account chrome

## Deny (tokenize; never as usable secrets)

| Identifier | Token |
|------------|--------|
| SSN / ITIN | `[REDACTED_SSN]` |
| Full account / routing / PAN | `[REDACTED_ACCOUNT]` |
| Check # / confirmation ID | `[REDACTED_CHECK]` |
| Street address | `[REDACTED_ADDRESS]` |
| Phone | `[REDACTED_PHONE]` |
| Email | `[REDACTED_EMAIL]` |
| Repeated holder-name spam | `[REDACTED_NAME]` |

## Hard rules

- **No LLM `strip_pii` before adapter parse.** Deterministic deny-list only if needed pre-parse.
- If unsure header identity vs payee: **prefer KEEP on activity lines.**
- Frontier / cloud: aggregates only — no row payee egress.
- Logs: never raw payee, description, SSN, full account, or `amount_cents`.
- Tier-1 MCP (Vault-local / self-hosted LLMs): payee OK. Tier-2: aggregates only.
