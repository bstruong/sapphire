# Shape Up visuals

## When do mockups and wireframes happen?

**During shaping — before the betting table.** Not during the build cycle.

| Phase | Visual work |
|-------|-------------|
| **Raw backlog** | None. One-liners only. |
| **Shaping** | Fat-marker sketches, breadboards, and **low-fi wireframes** for any user-visible surface. These make the pitch reviewable. |
| **Betting** | PM reviews pitch **plus** linked wireframes. No visual → UI bet is not ready. |
| **Building** | Implement against the shaped wireframe. Do not invent a second design pass mid-cycle (rabbit hole). |
| **Cool-down** | Optional polish sketches for the *next* cycle’s pitches—not unscoped redesign of what just shipped. |

Shape Up intentionally avoids high-fidelity pixel comps as a gating artifact. Prefer:

1. **Breadboard** (elements + connections) in the pitch  
2. **Fat-marker / low-fi wireframe** (layout, hierarchy, primary actions) in this folder  
3. **Done-when** Manual UI QA that matches the wireframe  

## Conventions

- Path: `docs/specs/visuals/<pitch_or_surface>_wireframe.html` (open in a browser) or `.md` with ASCII/mermaid when HTML is overkill.
- Label the file and pitch: **baseline** (current shipped UI) vs **proposed** (shaped change).
- Synthetic copy only (`SYNTHETIC`, `*0000`). No real customer data.
- No hallucinated SRI hashes or asset UUIDs.

## Baseline artifacts

| File | Surface |
|------|---------|
| [dashboard_baseline_wireframe.html](dashboard_baseline_wireframe.html) | Early Vault dashboard (dual pane: ask + results / frontier verify) |
| [statements_in_browser_wireframe.html](statements_in_browser_wireframe.html) | **Baseline** — PDF upload + templated DB ledger (shipped) |
| [account_overview_wireframe.html](account_overview_wireframe.html) | **Baseline** — YNAB-useful account overview on load; transactions as drill-in (shipped) |
| [ynab_budgets_wireframe.html](ynab_budgets_wireframe.html) | **Proposed** — Ready to Assign + envelopes (YNAB budgets bet) |
| [dashboard_refresh_proposed.html](dashboard_refresh_proposed.html) | **Proposed** — Simple/modern dashboard shell: sidebar nav, KPI cards, dark + light themes, empty/error states ([pitch](../pitch_dashboard_refresh.md)) |
| [chase_credit_card_copy_note.md](chase_credit_card_copy_note.md) | **Proposed** — Honest supported-upload copy for Chase **card** (not checking) ([pitch](../pitch_chase_credit_card.md)) |
| [schwab_checking_copy_note.md](schwab_checking_copy_note.md) | **Proposed** — Supported-upload copy includes Schwab Bank **checking** ([pitch](../pitch_schwab_checking.md)) |
