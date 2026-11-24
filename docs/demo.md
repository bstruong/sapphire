# SAPPHIRE — 5-minute interview demo

A copy-pasteable script for hiring-bar walkthroughs. Tone: **fail-loud**, **integer cents**, **zero-PII egress**. Entry point: [README.md](../README.md).

## Goal (30s)

SAPPHIRE turns ragged PDF statements into balanced ledger rows inside one **Vault** (Postgres + FastAPI + HTMX). Money is always `BIGINT` cents. Invalid input raises explicit exceptions—no silent coercion. Raw statements and amounts never leave the Vault trust boundary.

## Setup (1 min)

```bash
uv sync
export DATABASE_URL=postgresql://postgres:sapphire@localhost:54329/sapphire   # from .env.example
uv run python scripts/seed_dashboard.py
uv run uvicorn app.api.app:app --host 127.0.0.1 --port 8000
```

Docker alternative: `docker compose up --build` (see README).

## Script (≈3 min)

### 1. Health (happy path)

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
```

Expect `"status":"ok"` and `"database":"connected"`. Open `http://127.0.0.1:8000/dashboard` — synthetic seed data only (`SYNTHETIC …`, masks like `*0000`).

### 2. Integer cents (show, don't float)

Point at any amount on the dashboard or in seed output. Narrate: storage and arithmetic are **integer cents** (`int` / `BIGINT`). No `float` money paths.

### 3. Fail-loud (negative)

Upload a non-PDF (or empty) statement via the Statements UI, or hit an ingest path with a malformed payload. Expect a loud rejection (4xx / explicit error)—not a partial write.

### 4. Zero-PII egress (contract)

```bash
uv run python scripts/qa_watchtower_inference.py
```

- If JARVIS is **down**: script **fail-softs** (exit 0)—live calls skipped; offline taxonomy still asserted.
- If JARVIS is **up**: live completion/extraction must pass; contract violations exit **non-zero**. Logs must not contain prompts, account tokens, or amounts.

Client: [`app/core/inference.py`](../app/core/inference.py) (`VaultInferenceClient`). Env: `VAULT_INFERENCE_URL`, `VAULT_INFERENCE_MODEL`, `VAULT_INFERENCE_TIMEOUT` (aliases in README / `.env.example`).

## Close (30s)

One Vault deployable. Cloud agents may build code; **runtime** PII models stay on-Vault. Frontier (optional) sees sanitized aggregates only—never raw statements.

## Out of scope for this demo

API-key middleware, wiring JARVIS into ingest, k3s Watchtower ops, OTel dashboards, Carnelian/Alpha.
