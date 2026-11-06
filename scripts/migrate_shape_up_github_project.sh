#!/usr/bin/env bash
# Migrate ARCH_PLAN Shape Up board → private GitHub Project (Projects v2).
#
# Run on your workstation as the project owner (needs `project` scope):
#   gh auth refresh -s project,read:project
#   bash scripts/migrate_shape_up_github_project.sh
#
# Idempotent-ish: if a project titled "SAPPHIRE Shape Up" already exists for
# OWNER, reuses it and skips create. Always (re)links the sapphire repo.
set -euo pipefail

OWNER="${SHAPE_UP_OWNER:-@me}"
TITLE="${SHAPE_UP_TITLE:-SAPPHIRE Shape Up}"
REPO="${SHAPE_UP_REPO:-bstruong/sapphire}"
CYCLE="${SHAPE_UP_CYCLE:-cycle-2026-w42}"

need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need gh
need jq

echo "==> Ensuring GitHub auth can manage projects"
if ! gh api graphql -f query='query { viewer { login } }' --jq .data.viewer.login >/dev/null 2>&1; then
  echo "gh auth failed. Run: gh auth login && gh auth refresh -s project,read:project" >&2
  exit 1
fi

LOGIN="$(gh api graphql -f query='query { viewer { login } }' --jq .data.viewer.login)"
if [[ "$OWNER" == "@me" ]]; then
  OWNER_LOGIN="$LOGIN"
else
  OWNER_LOGIN="$OWNER"
fi
OWNER="$OWNER_LOGIN"
echo "    owner login: $OWNER_LOGIN"

echo "==> Locate or create project: $TITLE"
EXISTING_NUM="$(
  gh project list --owner "$OWNER" --limit 100 --format json \
    | jq -r --arg t "$TITLE" '.projects[] | select(.title == $t) | .number' \
    | head -n1
)"

if [[ -n "$EXISTING_NUM" ]]; then
  NUMBER="$EXISTING_NUM"
  echo "    reusing project #$NUMBER"
else
  CREATE_JSON="$(gh project create --owner "$OWNER" --title "$TITLE" --format json)"
  NUMBER="$(echo "$CREATE_JSON" | jq -r '.number')"
  echo "    created project #$NUMBER"
fi

echo "==> Force visibility PRIVATE"
gh project edit "$NUMBER" --owner "$OWNER" --visibility PRIVATE \
  --description "SAPPHIRE Shape Up control plane (betting table, in-bet, shipped archive, cool-down)." \
  --readme "$(cat <<'EOF'
# SAPPHIRE Shape Up

**Source of truth** for active bets and the betting table.

- **Lane:** Raw | Shaped | In-bet | Shipped | Circuit-broken | Cool-down | Deferred
- Pitches still live in `docs/specs/pitch_*.md`; wireframes in `docs/specs/visuals/`.
- Repo `ARCH_PLAN.md` holds process rules and a pointer here — not the live board.
EOF
)" >/dev/null

PROJECT_JSON="$(gh project view "$NUMBER" --owner "$OWNER" --format json)"
PROJECT_URL="$(echo "$PROJECT_JSON" | jq -r '.url')"
PROJECT_ID="$(echo "$PROJECT_JSON" | jq -r '.id')"
echo "    url: $PROJECT_URL"

echo "==> Link repository $REPO"
gh project link "$NUMBER" --owner "$OWNER" --repo "$REPO" >/dev/null || true

echo "==> Ensure custom fields"
fields_json() { gh project field-list "$NUMBER" --owner "$OWNER" --format json; }

ensure_single_select() {
  local name="$1"
  shift
  local options_csv
  options_csv="$(IFS=,; echo "$*")"
  if fields_json | jq -e --arg n "$name" '.fields[] | select(.name == $n)' >/dev/null; then
    echo "    field exists: $name"
  else
    echo "    creating field: $name ($options_csv)"
    gh project field-create "$NUMBER" --owner "$OWNER" \
      --name "$name" --data-type SINGLE_SELECT \
      --single-select-options "$options_csv" >/dev/null
  fi
}

ensure_text() {
  local name="$1"
  if fields_json | jq -e --arg n "$name" '.fields[] | select(.name == $n)' >/dev/null; then
    echo "    field exists: $name"
  else
    echo "    creating field: $name (TEXT)"
    gh project field-create "$NUMBER" --owner "$OWNER" \
      --name "$name" --data-type TEXT >/dev/null
  fi
}

ensure_single_select "Lane" \
  "Raw" "Shaped" "In-bet" "Shipped" "Circuit-broken" "Cool-down" "Deferred"
ensure_single_select "Appetite" "Small Batch" "Big Batch" "n/a"
ensure_text "Cycle"
ensure_text "Pitch path"

# Refresh fields after possible creates
FIELDS="$(fields_json)"
field_id() { echo "$FIELDS" | jq -r --arg n "$1" '.fields[] | select(.name == $n) | .id'; }
option_id() {
  local field="$1" opt="$2"
  echo "$FIELDS" | jq -r --arg f "$field" --arg o "$opt" \
    '.fields[] | select(.name == $f) | .options[]? | select(.name == $o) | .id'
}

LANE_FIELD="$(field_id Lane)"
APPETITE_FIELD="$(field_id Appetite)"
CYCLE_FIELD="$(field_id Cycle)"
PITCH_FIELD="$(field_id "Pitch path")"

# Built-in Status field (Todo / In Progress / Done) — map loosely
STATUS_FIELD="$(echo "$FIELDS" | jq -r '.fields[] | select(.name == "Status") | .id')"

set_select() {
  local item_id="$1" field_id="$2" option_id="$3"
  [[ -n "$field_id" && -n "$option_id" && "$option_id" != "null" ]] || return 0
  gh project item-edit --id "$item_id" --project-id "$PROJECT_ID" \
    --field-id "$field_id" --single-select-option-id "$option_id" >/dev/null
}

set_text() {
  local item_id="$1" field_id="$2" text="$3"
  [[ -n "$field_id" && "$field_id" != "null" ]] || return 0
  [[ -n "$text" ]] || return 0
  gh project item-edit --id "$item_id" --project-id "$PROJECT_ID" \
    --field-id "$field_id" --text "$text" >/dev/null
}

echo "==> Fetch existing items"
EXISTING_ITEMS="$(gh project item-list "$NUMBER" --owner "$OWNER" --limit 100 --format json 2>/dev/null || echo '{"items":[]}')"

add_item() {
  local title="$1" body="$2" lane="$3" appetite="$4" pitch="$5"
  echo "    + $title [$lane]"
  local item_id
  item_id="$(echo "$EXISTING_ITEMS" | jq -r --arg t "$title" '.items[]? | select(.title == $t) | .id' | head -n1)"
  if [[ -z "$item_id" || "$item_id" == "null" ]]; then
    local item_json
    item_json="$(gh project item-create "$NUMBER" --owner "$OWNER" \
      --title "$title" --body "$body" --format json)"
    item_id="$(echo "$item_json" | jq -r '.id')"
  fi
  set_select "$item_id" "$LANE_FIELD" "$(option_id Lane "$lane")"
  set_select "$item_id" "$APPETITE_FIELD" "$(option_id Appetite "$appetite")"
  set_text "$item_id" "$CYCLE_FIELD" "$CYCLE"
  set_text "$item_id" "$PITCH_FIELD" "$pitch"
}

echo "==> Seed board items (draft issues)"
# Current cycle note
add_item \
  "Cycle $CYCLE — Cloud-Build" \
  "Mode: Cloud-Build. Appetite budget: Small Batch default. In-bet: none (awaiting PM bet)." \
  "Cool-down" "n/a" ""

# Shaped / deferred
add_item \
  "Watchtower telemetry path" \
  "Verifiable OTel → collector smoke path for Watchtower; no SaaS APM. Deferred — not on critical path to personal Vault home." \
  "Deferred" "Small Batch" "docs/specs/pitch_watchtower_telemetry.md"

# New shaped pitch (YNAB) — seeded by this migration when pitch lands in repo
add_item \
  "YNAB budgets / envelopes" \
  "Personal Vault: assign dollars to envelope categories (integer cents). Depends on account overview (shipped). Category assignment may be in-scope or a thin prerequisite slice." \
  "Shaped" "Small Batch" "docs/specs/pitch_ynab_budgets.md"

# Shipped archive
add_item "Pipeline steps 1–9 (baseline)" "Contracts → extract → adapters → Postgres → ingest." "Shipped" "n/a" ""
add_item "Phase 2 HTMX dashboard + tiered MCP + frontier audit" "Shipped baseline." "Shipped" "n/a" ""
add_item "Local HTMX asset + SRI ban + Playwright E2E" "Shipped." "Shipped" "n/a" ""
add_item "Holdings + Vanguard + portfolio bridge" "Shipped." "Shipped" "n/a" ""
add_item "App-side telemetry helpers + QA" "Full Watchtower remains separate pitch." "Shipped" "n/a" ""
add_item "Spec & design truthfulness" "Shipped." "Shipped" "n/a" ""
add_item "Single Vault + shaping wireframes" "Shipped." "Shipped" "n/a" ""
add_item "Statements in the browser (Chase + Schwab)" "Shipped — PR #49." "Shipped" "Small Batch" "docs/specs/pitch_statements_in_browser.md"
add_item "Account overview home (YNAB-useful)" "Shipped — PR #50. USD overview on load + Details drill-in." "Shipped" "Small Batch" "docs/specs/pitch_account_overview.md"
add_item "Dashboard refresh (Modern UI/UX)" "Shipped — stacked under PR #61 / dashboard refresh branch." "Shipped" "Small Batch" "docs/specs/pitch_dashboard_refresh.md"

# Raw backlog
add_item "Chase credit card adapter" "Shipped — PR #61. Redacted PDF fixture + exclusive chase_credit_card matcher." "Shipped" "Small Batch" "docs/specs/pitch_chase_credit_card.md"
add_item "Schwab checking (Investor Checking) adapter" "In-bet. Exclusive schwab_checking vs brokerage + dogfood DB isolation + liquid/RTA + money-debug. Mark Shipped on merge." "In-bet" "Small Batch" "docs/specs/pitch_schwab_checking.md"
add_item "Additional institution adapters" "Beyond locked stack. SFFCU + Schwab savings/multi-product still raw." "Raw" "n/a" ""
add_item "Synthetic PDF fixtures for layout-hard statements" "Beyond JSON token fixtures." "Raw" "n/a" ""
add_item "Grafana Tempo / Prometheus / Loki dashboards" "Depends on Watchtower pitch." "Raw" "n/a" ""
add_item "Traefik ingress hardening notes" "Vault deployment." "Raw" "n/a" ""
add_item "Category assignment UI" "Personal budgeting labels (may fold into YNAB budgets pitch)." "Raw" "n/a" ""
add_item "Multi-month net-worth history charts" "Beyond account overview snapshot." "Raw" "n/a" ""

# Cool-down
add_item "CI nits (no product surface)" "Cool-down chore." "Cool-down" "n/a" ""
add_item "Mutation nightly / coverage hygiene" "Cool-down chore." "Cool-down" "n/a" ""
add_item "Architecture docs polish copy-edit" "Optional cool-down." "Cool-down" "n/a" ""

echo
echo "DONE. Private Shape Up board:"
echo "  $PROJECT_URL"
echo
echo "Paste that URL into ARCH_PLAN.md (GitHub Project pointer) if not already set."
echo "$PROJECT_URL" > /tmp/sapphire_shape_up_project_url.txt
