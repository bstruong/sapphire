# SAPPHIRE

**SAPPHIRE** is a mathematically rigorous, fail-loud financial document extraction and reconciliation pipeline.

It enforces strict financial invariants, closed canonical schemas, and zero-PII egress to external models. Raw, ragged PDF statement data becomes balanced, integer-cents ledger records inside a single **Vault** deployable (Postgres + FastAPI + HTMX).

## Architecture

```mermaid
flowchart TD
  user[User] --> vault[Vault_app_Postgres]
  localModel[Local_model_on_Vault] -->|PII_touching_parse_strip| vault
  vault -->|sanitized_aggregates_only| frontier[Optional_Frontier_Model]
  frontier -->|insights_HTMX| vault
```

Two different “local model” questions — keep them separate:

| Lane | Local models? | Why |
|------|---------------|-----|
| **Development (Cloud-Build)** | **No** — cloud agents shape/build code. No `pi-up.sh` / local coding LLM required. | Delivery process only. |
| **Runtime (Vault / PII pipeline)** | **Yes** — models that see raw statements run **on the Vault** (or same trust boundary). | PII stripping / extraction must not send raw docs to cloud. |

- **Vault:** Owns PDFs, PII, ledgers, dashboard, MCP, and any local inference used for parse/strip.
- **Frontier (optional):** Sees anonymous aggregates only—never raw statements or amounts in logs.
- **Not required:** a separate “AI Brain” workstation as a second product box. Local models are Vault-side runtime, not a parallel deploy to operate.

## Engineering Rigor

- **Financial Precision (`BIGINT`):** All money is integer cents. No floats for currency.
- **Fail-loud contracts:** Schema drift, missing markers, and recon breaks raise explicit exceptions.
- **TDD & coverage:** `uv run pytest --cov=app --cov-fail-under=90 -v` (≥90%).
- **Mutation (Watchtower on main):** Path-filtered diff / critical-only via `.github/workflows/mutation-nightly.yml`—not a PR blocker; full allowlist is rare/opt-in.

Planning uses Shape Up (`ARCH_PLAN.md`, `docs/specs/`). User-visible pitches must include shaping-phase wireframes under `docs/specs/visuals/`. Board sync: owner runs `gh auth refresh -s project,read:project` then `bash scripts/migrate_shape_up_github_project.sh` (no dedicated agent PAT).

**Interview / hiring demo:** [docs/demo.md](docs/demo.md) — a 5-minute script (fail-loud, integer cents, zero-PII egress).

## Quick Start & Verification

**1. Install Dependencies**
```bash
uv sync
```

**2. Run the Test Suite**
```bash
uv run pytest --cov=app --cov-fail-under=90 -v
```

**3. Run Linter & Formatter**
```bash
uv run ruff check .
uv run ruff format .
```

**4. Run the dashboard (seed + health)**

Copy env defaults from [`.env.example`](.env.example) (dogfood DB is `…/sapphire`, never `sapphire_test`):

```bash
export DATABASE_URL=postgresql://postgres:sapphire@localhost:54329/sapphire
uv run python scripts/seed_dashboard.py
uv run uvicorn app.api.app:app --host 127.0.0.1 --port 8000
```

In another terminal:

```bash
curl -sS http://127.0.0.1:8000/api/v1/health
```

Open `http://127.0.0.1:8000/dashboard`. Baseline wireframe: `docs/specs/visuals/dashboard_baseline_wireframe.html`.

**5. Docker Compose (optional)**

Postgres + Vault with a healthcheck on `/api/v1/health`:

```bash
docker compose up --build
curl -sS http://127.0.0.1:8000/api/v1/health
```

See [`Dockerfile`](Dockerfile) and [`compose.yaml`](compose.yaml). Compose sets `DATABASE_URL` to the `db` service; host-mapped Postgres is `localhost:54329` to match `.env.example`.

## Local inference contract (Vault → JARVIS)

PII-touching parse/strip stays inside the Vault trust boundary via [`VaultInferenceClient`](app/core/inference.py). Live verification:

```bash
uv run python scripts/qa_watchtower_inference.py
```

| Behavior | Exit |
|----------|------|
| Endpoint **down** | **fail-soft**: skips live checks, still asserts offline contract taxonomy → exit **0** |
| Endpoint **up** + contract violation | Exit **non-zero** |
| Endpoint **up** + all checks pass | Exit **0** |

Environment (see [`.env.example`](.env.example)):

| Variable | Role | Default |
|----------|------|---------|
| `VAULT_INFERENCE_URL` | OpenAI-compatible base URL (`…/v1`) | `http://ai.watchtower.local/v1` |
| `OPENAI_BASE_URL` / `LOCAL_LLM_URL` | Accepted aliases for the URL | — |
| `VAULT_INFERENCE_MODEL` | Default completion model | `qwen36-coder-hybrid` |
| `LOCAL_LLM_MODEL` | Alias for the model | — |
| `VAULT_INFERENCE_TIMEOUT` | Seconds | `30.0` |

Prefer Traefik ingress or loopback hostPort on the Watchtower node—not a LAN `:8080` hostPort. Exclusive GPU work uses `homelab-k3s/scripts/jarvis-lease.sh`. Do **not** wire JARVIS into ingest in this Quick Start; the client is the contract surface only.

## Troubleshooting & Agent Runbooks

### CI & Mutation Testing (`mutmut`)
* **PR CI (required):** lint, format, coverage ≥90%. **Mutation is not a PR blocker.**
* **Main (Watchtower):** `.github/workflows/mutation-nightly.yml` runs path-filtered diff on push; `workflow_dispatch` defaults to critical-only; set `full_allowlist=true` for a rare full audit.
* **Classic score:** `killed / (killed + survived)`. `no_tests` is a separate hard fail.
* See `AGENTS.md` for full mutation and process rules.
