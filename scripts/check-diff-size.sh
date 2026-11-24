#!/usr/bin/env bash
# Fail if branch vs main exceeds 150 LOC (insertions + deletions).
set -euo pipefail

MAX_LOC=150
BASE_REF="${1:-main}"

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  echo "ERROR: not inside a git repository" >&2
  exit 2
}
cd "$ROOT"

if git rev-parse --verify "$BASE_REF" >/dev/null 2>&1; then
  DIFF_BASE="$BASE_REF"
elif git rev-parse --verify "origin/$BASE_REF" >/dev/null 2>&1; then
  DIFF_BASE="origin/$BASE_REF"
else
  echo "ERROR: base ref '$BASE_REF' (and origin/$BASE_REF) not found" >&2
  exit 2
fi

if MERGE_BASE="$(git merge-base "$DIFF_BASE" HEAD 2>/dev/null)"; then
  STAT="$(git diff --shortstat "$MERGE_BASE"...HEAD)"
else
  STAT="$(git diff --shortstat "$DIFF_BASE"...HEAD)"
fi

INSERTIONS=0
DELETIONS=0
if [[ -n "${STAT// /}" ]]; then
  INSERTIONS="$(echo "$STAT" | grep -oE '[0-9]+ insertion' | grep -oE '[0-9]+' || true)"
  DELETIONS="$(echo "$STAT" | grep -oE '[0-9]+ deletion' | grep -oE '[0-9]+' || true)"
  INSERTIONS="${INSERTIONS:-0}"
  DELETIONS="${DELETIONS:-0}"
fi
TOTAL=$((INSERTIONS + DELETIONS))

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "Diff vs ${DIFF_BASE} (merge-base...HEAD): ${STAT:-0 files changed}"
echo "LOC (insertions + deletions): ${TOTAL} (limit ${MAX_LOC})"

if (( TOTAL > MAX_LOC )); then
  echo -e "${RED}LOC GATE FAILED${NC}: ${TOTAL} > ${MAX_LOC}"
  echo -e "${YELLOW}Alert:${NC} split this PR or accept manual-only review — automated AI approval is blocked."
  exit 1
fi

echo -e "${GREEN}LOC GATE PASSED${NC}: ${TOTAL} <= ${MAX_LOC}"
exit 0
