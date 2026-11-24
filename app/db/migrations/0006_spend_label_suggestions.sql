-- Bulk suggest staging (B2). Suggest-only: Accept writes transaction_spend_labels.

CREATE TABLE IF NOT EXISTS spend_label_suggestions (
  suggestion_id       UUID PRIMARY KEY,
  batch_id            UUID NOT NULL,
  transaction_id      UUID NOT NULL REFERENCES transactions (transaction_id) ON DELETE CASCADE,
  payee_display       TEXT NOT NULL,
  payee_normalized    TEXT NOT NULL,
  proposed_slug       TEXT NOT NULL,
  origin              TEXT NOT NULL CHECK (origin IN ('rule', 'vault_suggest')),
  evidence            TEXT NOT NULL,
  pie_year            INTEGER NOT NULL,
  pie_month           INTEGER NOT NULL,
  abs_amount_cents    BIGINT NOT NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (char_length(btrim(payee_display)) > 0),
  CHECK (char_length(btrim(payee_normalized)) > 0),
  CHECK (char_length(btrim(proposed_slug)) > 0),
  CHECK (char_length(btrim(evidence)) > 0),
  CHECK (pie_year >= 1970 AND pie_year <= 2100),
  CHECK (pie_month >= 1 AND pie_month <= 12),
  CHECK (abs_amount_cents > 0),
  UNIQUE (batch_id, transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_spend_label_suggestions_batch
  ON spend_label_suggestions (batch_id);

CREATE INDEX IF NOT EXISTS idx_spend_label_suggestions_month
  ON spend_label_suggestions (pie_year, pie_month, batch_id);
