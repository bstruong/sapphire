# SAPPHIRE Agent Configuration & Repository Conventions

This document serves as the authoritative source of conventions and instructions for all AI agents and contributors operating within the SAPPHIRE repository.

## Environment & Runtime Commands

Agents must verify their work using the following commands:
- **Build/Dependencies:** `uv sync`
- **Lint:** `uv run ruff check .`
- **Format Check:** `uv run ruff format --check .` (Format fix: `uv run ruff format .`)
- **Test with Coverage Gate (>90%):** `uv run pytest --cov=app --cov-fail-under=90 -v`
- **Mutation Testing (local / Watchtower):** Default CI is path-filtered **diff** (or `--critical-only`); rare full audit: `uv run python scripts/run_mutation_on_diff.py --full-allowlist && uv run python scripts/check_mutation_score.py --min-score 55.0 --max-no-tests 0` (PR CI does **not** run mutmut; CI mutation runs on `[self-hosted, watchtower]` — see `.github/workflows/mutation-nightly.yml`)
- **Agent Invariant Review:** `uv run python scripts/agent_review.py`

### Database isolation (dogfood vs pytest)

- **Dogfood / Vault (`uvicorn`, `seed_dashboard`, Manual UI QA):** `DATABASE_URL=…/sapphire` (default in `app/db/connection.py`).
- **Pytest / mutmut only:** `TEST_DATABASE_URL=…/sapphire_test`. Pytest **never** falls back to `DATABASE_URL` and refuses DB names that do not end in `_test` / `_pytest`.
- Do **not** point uvicorn at `sapphire_test`. Do **not** run `seed_dashboard` against the pytest DB.

## Core Invariants

Any changes made to the codebase must strictly adhere to these invariants:

1. **Strict Contract Enforcement:**
   - Services must validate inputs and fail loudly on invalid payloads.
   - Do not implement silent coercion, skipping of malformed rows, or "best effort" parsing. If it's invalid, it fails with an explicit exception from `app.models.exceptions`.

2. **Financial Precision:**
   - All monetary values must be stored strictly as integer cents (`BIGINT` in database, `int` in Python).
   - No floating-point arithmetic (`float`, `Double`, etc.) is permitted for any monetary operations.

3. **Code Clarity:**
   - No dynamic metaprogramming or deep reflection.
   - Code must be highly readable and support static AST analysis. Explicit is always better than implicit.

4. **Observability:**
   - Structured JSON logging is required on all external I/O and processing boundaries.
   - Logs must provide sufficient context to reconstruct the state leading up to an error without manual debugging.
   - Never log raw PII (account numbers, SSNs, personal identities) or unmasked financial amounts (`amount_cents`, `amount`).

5. **Frontend Asset Integrity:**
   - Agents are strictly forbidden from hallucinating cryptographic hashes (SRI, SHA, MD5) or UUIDs for frontend assets.
   - You must either serve assets locally from `app/static/`, compute hashes programmatically, or omit them.

---


6. **Shape Up planning + Cloud-Build Mode (temporary):**
   - Active product work is shaped as **pitches** under `docs/specs/`. The live Shape Up **board** (betting table / in-bet / shipped / raw) is the private GitHub Project **SAPPHIRE Shape Up** — see pointer + migrate script in `ARCH_PLAN.md`. Do not turn raw backlog into undated Phase N checklists in `docs/architecture/`.
   - **Cloud-Build Mode (active):** Cloud agents **may** shape pitches **and** implement chosen bets (application code, tests, QA scripts, PRs). Local Tier-1 / `pi-up.sh` is **not** required while this mode is active.
   - When Cloud-Build Mode is explicitly revoked, restore the prior rule: cloud agents plan/review/CI only; Tier-1 local harness implements.
   - TDD, coverage ≥90%, financial precision, fail-loud contracts, and zero PII/amount logging still gate every bet—Cloud-Build does not relax those invariants.

## Test-Driven Development (TDD) Mandate

All feature development and adapter implementations must strictly adhere to Test-Driven Development (TDD):

1. **Phase 1: Red (Test First):**
   - Before writing any implementation code or schema parsing logic, write exhaustive unit and integration tests based on the feature specification, canonical contracts, and known negative invariant boundaries.
   - Tests must include both happy path assertions and explicit negative tests asserting that malformed tokens, schema drift, or equation violations fail loudly.
   - Verify tests fail before writing implementation code.

2. **Phase 2: Green (Make It Pass):**
   - Write the minimum contract-compliant code necessary to pass the test suite.
   - Do not introduce extraneous features, speculative abstractions, or bypassed validation checks.

3. **Phase 3: Refactor (Clean & Type-Safe):**
   - Refactor code for readability, type hints, and performance while ensuring AST clarity.
   - Run `uv run ruff check .` and `uv run ruff format --check .` to ensure compliance.

4. **Strict Test Coverage Standard (>90%):**
   - All modules in `app/` must maintain at least **90% line and branch test coverage**.
   - Any pull request dropping coverage below 90% is strictly blocked by CI (`uv run pytest --cov=app --cov-fail-under=90`).
   - Trivial tests that simply mock away validation without asserting invariant enforcement are unacceptable.

5. **Mutation Testing Mandate (Killing Injected Defects):**
   - High code coverage alone is meaningless if tests merely execute lines of code without validating outcomes. Tests must be robust enough to **kill mutations**.
   - Injected mutations (such as operator inversions `<` vs `<=`, altered arithmetic signs, or deleted statements) must cause test failures.
   - Core financial validators (such as `app/pipeline/validator.py`) enforce a **100% mutation kill rate (zero surviving mutants)**.
   - **PR CI does not run mutmut** (avoids long/brittle PR blockers). Mutation is **not** a PR required check. On `main` it runs on the Watchtower self-hosted runner (`.github/workflows/mutation-nightly.yml`: path-filtered **diff** on push; `workflow_dispatch` defaults to **critical-only** / opt-in `full_allowlist=true` for rare full audits — **no weekly cron**), gated by `scripts/check_mutation_score.py --min-score 55.0 --max-no-tests 0`. It does **not** run daily on `ubuntu-latest`.
   - Agents changing financial modules should still run mutation locally when practical (`--critical-only` or diff); use `--full-allowlist` only for rare full audits.
   - Score is classic `killed/(killed+survived)`. `no_tests` fails separately. Only financial allowlisted modules are mutated; API/dashboard/MCP are out of scope. Validator (`CRITICAL_MODULES`) must stay at **100% kill** when mutated.

---

## Product Manager & QA Acceptance Mandate

The human operator acts in the role of Quality Assurance (QA) and Product Manager (PM). They must be able to verify product functionality, reliability, and correctness **without reading code or diffs**.

Every pull request and feature deliverable **MUST** satisfy the following:

1. **Executable QA Script:**
   - Every PR must include an executable script (e.g., `scripts/qa_<feature_name>.py`) or explicit, copy-pasteable terminal instructions.
   - The script must be runnable via `uv run python scripts/qa_<feature_name>.py`.
   - The QA script must clearly print:
     - What feature or capability is being verified.
     - Successful processing of valid inputs (happy path).
     - Intentional, loud rejection of malformed or fraudulent inputs (negative verification).
     - Clear terminal indicators (`[PASS]` / `[FAIL]`) summarizing verification results.

2. **Structured Architecture & QA Artifact:**
   - Every PR must provide a Markdown artifact (or detailed PR description section) summarizing:
     - Architectural decisions made.
     - Core invariants verified (financial precision, contract enforcement, observability).
     - Test coverage report.
     - Exact instructions for the PM to execute the verification script.

3. **Manual UI QA (when the feature is user-visible):**
   - If the change touches a screen a human can open (HTMX dashboard, forms, uploads, routes rendered in a browser), the PR description **must** include a **Manual UI QA** section the PM can follow without reading code.
   - The section must contain:
     - How to start the app (for the dashboard: `uv run uvicorn app.api.app:app --host 127.0.0.1 --port 8000`, then open `http://127.0.0.1:8000/dashboard`).
     - Click-by-click steps (what to open, type, upload, or submit).
     - What success looks like on screen (happy path).
     - What a loud failure looks like on screen when the feature has a negative case (malformed upload, empty state, rejected input).
   - If the change has **no** browser surface, write one line: `Manual UI QA: not applicable (no user-visible UI).`
   - **Synthetic test data:** When the UI cannot be exercised on an empty database, the agent must generate synthetic data and tell the PM how to load it. Prefer a committed script (`uv run python scripts/seed_<feature>.py`) or a fixture under `tests/fixtures/`. Data must be obviously fake (masks like `*0000`, descriptions such as `SYNTHETIC Payroll`), integer cents only, and must not use real customer PDFs, account numbers, SSNs, or names. Do not invent SRI hashes or UUIDs for frontend assets; compute them or omit them.

---

## Agent-to-Agent Review Protocol

To ensure autonomous code quality and invariant compliance, every pull request must undergo automated Agent-to-Agent review before human acceptance.

### 1. Dual Agent Architecture
- **Implementation Agent:** Responsible for authoring contracts, writing failing tests (TDD), implementing features, authoring QA scripts/artifacts, and opening the PR.
- **Reviewer Agent:** An autonomous peer agent responsible for rigorous static analysis, invariant verification, and validation of QA deliverables.

### 2. Reviewer Checklist
Before a pull request can be merged, the Reviewer Agent verifies:
- [ ] **Financial Precision:** AST analysis passes; zero floating-point types used for monetary values.
- [ ] **Strict Contract Enforcement:** No silent error suppression (`except Exception: pass`), no best-effort coercion, explicit taxonomy exceptions raised.
- [ ] **Observability & Data Privacy:** Structured logging is configured at boundaries; no PII or financial amounts leaked.
- [ ] **TDD & Coverage Gate:** `uv run pytest --cov=app --cov-fail-under=90` passes with >90% coverage.
- [ ] **Mutation Testing (Watchtower on main):** Not a PR merge blocker. Confirm financial changes remain killable; path-diff (and critical-only dispatch) gates on `main` (self-hosted) must stay green when they run (`classic >= 55%`, `no_tests == 0`, zero survivors in `app/pipeline/validator.py`). Full-allowlist is opt-in / rare, not the default gate.
- [ ] **Code Hygiene:** `uv run ruff check .` and `uv run ruff format --check .` pass with zero warnings.
- [ ] **QA Deliverables:** Executable QA script (`scripts/qa_*.py`) exists, runs successfully, and demonstrates both positive and negative cases.
- [ ] **Manual UI QA:** If the PR changes a browser-visible surface, the description has start command, click path, expected screen result, and (when needed) a synthetic seed the PM can load. Otherwise it states UI QA is not applicable.

### 3. Automated Review Gates
- **GitHub Actions CI (`.github/workflows/ci.yml`):** Automatically executes linting, formatting check, and the 90% coverage test gate on all PRs and pushes to `main` (no mutmut on PRs).
- **Mutation Nightly (`.github/workflows/mutation-nightly.yml`):** Runs on `[self-hosted, watchtower]`. Path-filtered **diff** on `main` pushes; `workflow_dispatch` default **critical-only**, opt-in `full_allowlist=true` for rare full audits (**no weekly cron**); score gate. Not a PR required check.
- **Agent Review Workflow (`.github/workflows/agent-review.yml`):** Automatically executes static invariant scans via `scripts/agent_review.py` and provides automated critique.

---

## Shape Up + Cloud-Build Workflow (current)

Planning follows Shape Up: raw backlog → shape pitch → PM bets → cloud agent builds within appetite → ship or circuit-break → cool-down. Live board = private GitHub Project (see `ARCH_PLAN.md`); pitches/wireframes = `docs/specs/`.

**Visuals:** For any user-visible surface, produce fat-marker / low-fi wireframes under `docs/specs/visuals/` during **shaping** (before betting). Do not defer first product visuals to mid-build.

**Local models — two lanes:**
- **Dev (Cloud-Build):** no local coding LLM / `pi-up.sh` required to implement bets.
- **Runtime (Vault):** local models **are** required for PII-touching parse/strip so raw statements never go to cloud. They live inside the Vault trust boundary—not a separate “AI Brain” product deploy.

### Cloud agent (Shaper / Builder) — Cloud-Build Mode
- **Role:** Primary author and implementation agent while Cloud-Build Mode is active.
- **Responsibilities:**
  - Keeps the GitHub Project board and `docs/specs/` pitches current; does not invent Phase N roadmaps in architecture docs.
  - Authors TDD suites (Red), implements bet scope (Green), refactors for AST clarity (Refactor).
  - Authors executable QA verification scripts (`scripts/qa_<feature_name>.py`).
  - For browser-visible features, writes Manual UI QA steps in the PR (and a synthetic seed script when the screen needs data).
  - Executes pre-flight checks (`uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest --cov=app --cov-fail-under=90 -v`, `uv run python scripts/agent_review.py`).
  - Commits changes and opens the Pull Request on GitHub.

### Dual-Tier / Tier 1 Local Generation (suspended)
The prior Dual-Tier path (cloud orchestrator plans only; local `qwen36-coder` / `pi-up.sh` implements) remains documented in runbooks below for when Cloud-Build Mode is revoked. Do not require local harness execution for bets while Cloud-Build Mode is active.

### Process Execution & Hang Prevention (CRITICAL)
1. **No Dangling Background Processes:** Never run commands in the background using `&` unless absolutely necessary. If you must start a server or long-running process, you must kill it explicitly before ending your turn.
2. **Explicit Thread Cleanup:** All test scripts and execution wrappers must have explicit cleanup logic (e.g., `sys.exit(0)` in Python or `os.Exit(0)` in Go) to forcefully terminate all lingering background threads.
3. **Pipe Detachment:** If you absolutely must spawn a persistent background process, you must redirect its output to detach it from the execution pipe: `command > /dev/null 2>&1 &`.
4. **Local inference cleanup:** If this session started `llama-server` (via `llama-up.sh` / `pi-up.sh`), end the turn with `killall llama-server` so `mlock`'d weights (~20GB) are released.
5. **Host-visible artifacts only:** Never tell agents to write reports to `/tmp/...` inside the harness container; write under `/home/brian/Projects/sapphire/` (or the active workspace root).

## Troubleshooting & CI/CD Runbooks (For Future Agents)

### Mutation Testing (`mutmut`) Anomalies
* **PR vs Watchtower mutation:** Mutation is **not** a PR required check. Failures belong to `Mutation Nightly` on `main` (`[self-hosted, watchtower]`). Do not re-add a blocking `mutation:` job to `ci.yml` or move it back to daily `ubuntu-latest` without an explicit product decision.
* **Mutation Postgres circuit breaker:** Do **not** restore `jobs.mutation.services` Postgres on Watchtower. DinD fails extracting `postgres:15` (overlayfs whiteout EPERM). Mutation uses **host** Postgres on the Watchtower node (`podman` container `sapphire-mutation-pg` → `:54329` / `sapphire_test`). See `homelab-k3s` `manifests/watchtower/github-runner/README.md`.
* **`no_tests` (🫥) is a hard fail:** Untested / dead code on allowlisted modules fails `--max-no-tests 0`. Delete dead helpers or add assertion-heavy tests. Do **not** "fix" this by mutating presentation code or faking stats.
* **Wrong score denominator:** Never use `killed/total`. `total` includes `no_tests` and skips. Use classic `killed/(killed+survived)` via `scripts/check_mutation_score.py`.
* **Module Import Crashes (Exit Code 4):** `mutmut` isolates files in `mutants/`. Always keep `source_paths = ["app"]` and inject `only_mutate` via `scripts/run_mutation_on_diff.py`. Never point `source_paths` at a single file.
* **Out-of-scope diffs:** Presentation/API/MCP/orchestrator are out of mutation scope. Default CI is diff-scoped (or `--critical-only` on dispatch); `--full-allowlist` is rare/manual only. Diff mode writes `skipped: true` when no allowlisted files changed.
* **Slow Mutations / E2E:** E2E tests are ignored in `[tool.mutmut]` and renamed `.bak` during runs. Playwright belongs on the **coverage** job only, not the mutation workflow.
* **Meta-tests under mutmut:** `tests/test_mutation_gates.py` must stay in mutmut `--ignore` (loads `scripts/` which is not copied into `mutants/`). Workflow-policy tests (`tests/test_ci_pr_slim.py`, `tests/test_ci_reliability.py`) must also stay ignored — they assert on `.github/workflows/*.yml` / `AGENTS.md`, which mutmut does not copy into `mutants/`.
* **Single config:** Do not reintroduce `setup.cfg` for mutmut; `[tool.mutmut]` in `pyproject.toml` is authoritative.

### Local Coding Harness (`pi-up.sh` / `llama-up.sh`)
* **Failure mode — RAM:** `llama-server --load-mode mlock` pins ~20GB. Leaving it running after an agent session starves the host. `pi-up.sh` does **not** tear it down.
* **Fix:**
  ```bash
  pgrep -a llama-server || true
  killall llama-server
  pgrep -a llama-server || echo "llama-server stopped"
  ```
* **Failure mode — missing reports:** Paths under the container's `/tmp` are not on the host. Instruct agents to write findings into the mounted workspace (e.g. `/home/brian/Projects/sapphire/investigation.md`), never `/tmp/report.md`.

