-- Deterministic payee → spend-category rules (bulk suggest B1).
-- Exact match on normalized payee_pattern; UNIQUE prevents ambiguous multi-match.

CREATE TABLE IF NOT EXISTS spend_label_rules (
  rule_id             UUID PRIMARY KEY,
  payee_pattern       TEXT NOT NULL,
  spend_category_id   UUID NOT NULL REFERENCES spend_categories (spend_category_id),
  enabled             BOOLEAN NOT NULL DEFAULT TRUE,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (char_length(btrim(payee_pattern)) > 0),
  UNIQUE (payee_pattern)
);

CREATE INDEX IF NOT EXISTS idx_spend_label_rules_enabled
  ON spend_label_rules (enabled)
  WHERE enabled IS TRUE;
