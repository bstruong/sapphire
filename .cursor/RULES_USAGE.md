# AI Code Review — Developer Workflow

Quick path using `.cursor/rules/code-review.mdc`.

## Steps

1. **Size gate** (required for automated approval):

   ```bash
   ./scripts/check-diff-size.sh
   ```

   Exit 0 / green `LOC GATE PASSED` ⇒ ≤ 150 LOC vs `main`. Over 150 blocks automated `APPROVED`.

2. **In Cursor Composer / Chat**, attach:

   - `@git diff main..HEAD` (or the PR diff)
   - `@code-review.mdc`
   - **One sentence** of PR intent / problem being solved

3. Expect findings only (`SEVERITY | file:line | checklist | defect`), or `APPROVED` + 1–2 bullets when clean and under the LOC gate.

## Notes

- Optional base: `./scripts/check-diff-size.sh <branch>` (default `main`).
- Rule globs: `*.py`, `*.html`, `*.jinja2`, `pyproject.toml`, `app/static/**/**`.
