# Pitch: Account overview home

**Status:** Shipped  
**Builder:** Cloud agent (Cloud-Build Mode)  
**Bettor:** Human PM  
**Appetite:** Small Batch (2 weeks)  
**Product north star:** YNAB-useful personal Vault — accounts & totals first; transactions as drill-down  
**Depends on:** Statements in the browser — shipped ([#49](https://github.com/bstruong/sapphire/pull/49))

---

## Problem

The PM is building the parts of **You Need A Budget** that matter for a personal Vault. After statement ingest works, the home screen still fails that mental model:

- Pane 2 stays **empty on load** even when Postgres already holds accounts and statements.
- The primary post-upload surface is a **transaction ledger** — useful for audit, wrong as the default home.
- High-level account detail (balances, period, net change) is buried inside ledger chrome or behind frontier aggregate clicks with opaque IDs.
- There is no first-class path: **open dashboard → see my accounts at a glance → dig into a statement only when needed.**

## Appetite

**Small Batch (2 weeks).** Fixed time, variable scope.

**Good enough when time runs out:** `GET /dashboard` prepopulates Pane 2 from Vault when statements exist: one **overview row/card per account** (institution, mask, period, opening/closing/net integer cents, txn count). Empty DB shows a clear empty state. Clicking an account (or explicit control) reveals that statement’s **transaction table** as a secondary view. No budgeting envelopes, goals, or category planning in this appetite.

## Solution (breadboard)

```mermaid
flowchart LR
  load[GET_dashboard] --> query[list_accounts_or_recent_statements]
  query --> overview[Templated_account_overview]
  overview -->|select_account| txns[Templated_txn_drill_in]
  upload[Upload_still_works] --> query
```

1. **Overview on load** — Server-render or HTMX-load Pane 2 from Postgres on dashboard GET. Prefer one row per **account** (latest statement), not a dump of every historical statement.
2. **High-level fields only** — Institution, account type, mask (`*0000`), statement period, `opening_balance_cents`, `closing_balance_cents`, `net_change_cents`, transaction count. Optional: portfolio/holdings **total** cents when brokerage. No dollar floats; no generative HTML.
3. **Drill-in** — Secondary action loads existing ledger/transaction template for one statement. Overview remains the default after refresh.
4. **Empty & fail-loud** — No rows → empty state inviting upload. DB errors fail loud (no silent blank with fake zeros).
5. **Keep upload / frontier** — Pane 1 upload and frontier verify stay; this bet changes **what home means**, not ingest.

## Visuals (shaping)

- Baseline (shipped on `main` today): [`visuals/dashboard_baseline_wireframe.html`](visuals/dashboard_baseline_wireframe.html)
- Prior bet in flight (ledger after upload): [PR #49](https://github.com/bstruong/sapphire/pull/49) — treat upload + templated ledger as the surface this pitch replaces as **home**
- Proposed (overview-first home): [`visuals/account_overview_wireframe.html`](visuals/account_overview_wireframe.html)

## Rabbit holes

- Full YNAB: budgets, envelopes, goals, scheduled transactions, multi-month planning
- Category assignment UI / “give every dollar a job”
- Rebuilding all dashboard chrome / dark-theme redesign
- Cross-account “net worth over time” charts and history scrubbers
- Frontier/LLM-authored overview copy
- Supporting institutions beyond what ingest already persists

## No-gos

- Budget envelopes, goals, or category planning
- Floating-point money; logging PII or raw `amount_cents`
- Generative Pane 2 HTML
- Making transactions the default home again
- Watchtower / Grafana / Traefik
- New bank adapters (use existing Vault rows only)

## Done-when

- [x] `uv run python scripts/qa_account_overview.py` — happy: seeded DB → overview on dashboard load with integer-cent fields; empty DB → empty state; drill-in shows transactions; negative: fail-loud on bad request/id
- [x] Manual UI QA documented below (matches proposed wireframe)
- [x] Proposed wireframe linked
- [x] Core invariants: integer cents, fail-loud, no PII/amount logging
- [x] CI gates green; archived after merge (#50)

## Manual UI QA

1. `set -x DATABASE_URL postgresql://postgres:sapphire@localhost:54329/sapphire` (fish) — dogfood Vault DB (never `sapphire_test`)
2. `uv run uvicorn app.api.app:app --host 127.0.0.1 --port 8000`
3. Open `http://127.0.0.1:8000/dashboard`
4. Empty DB: Pane 2 shows **Account overview** + “No persisted statements yet”
5. Happy: upload Chase/Schwab synthetic PDFs (same as #49), then **refresh** the page → Pane 2 shows per-account Opening/Closing/Net as **USD** (e.g. `$1,295.00`) **without** a transaction table; Vault still stores BIGINT cents
6. Drill-in: click **Details** → **Vault Ledger** with USD-formatted amounts; **Back to overview** returns to the summary
7. Negative: unknown statement id → **Upload rejected** / `STATEMENT_NOT_FOUND`

## Circuit breaker

At 2 weeks: ship overview-on-load + one-account drill-in even if multi-statement history UI is thin. Unfinished budget/YNAB planning features stay on raw backlog. If statements-in-browser is not merged, park this bet until that baseline lands.
