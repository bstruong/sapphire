-- P3: statement-scoped bulk suggest. One staging table + nullable statement_id.

ALTER TABLE spend_label_suggestions
  ADD COLUMN IF NOT EXISTS statement_id UUID NULL
    REFERENCES statements (statement_id) ON DELETE CASCADE;

ALTER TABLE spend_label_suggestions
  ALTER COLUMN pie_year DROP NOT NULL,
  ALTER COLUMN pie_month DROP NOT NULL;

ALTER TABLE spend_label_suggestions
  DROP CONSTRAINT IF EXISTS spend_label_suggestions_scope_chk;

ALTER TABLE spend_label_suggestions
  ADD CONSTRAINT spend_label_suggestions_scope_chk CHECK (
    (statement_id IS NOT NULL)
    OR (pie_year IS NOT NULL AND pie_month IS NOT NULL)
  );

CREATE INDEX IF NOT EXISTS idx_spend_label_suggestions_statement
  ON spend_label_suggestions (statement_id, batch_id)
  WHERE statement_id IS NOT NULL;
