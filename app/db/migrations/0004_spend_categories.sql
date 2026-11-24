-- Spend-category taxonomy for category spend pie (distinct from pipeline
-- TransactionCategory and from envelopes). Labels never carry money.

CREATE TABLE IF NOT EXISTS spend_categories (
  spend_category_id  UUID PRIMARY KEY,
  slug               TEXT NOT NULL,
  display_name       TEXT NOT NULL,
  is_system          BOOLEAN NOT NULL DEFAULT FALSE,
  CHECK (char_length(btrim(slug)) > 0),
  CHECK (char_length(btrim(display_name)) > 0),
  UNIQUE (slug)
);

CREATE TABLE IF NOT EXISTS transaction_spend_labels (
  transaction_id      UUID PRIMARY KEY REFERENCES transactions (transaction_id) ON DELETE CASCADE,
  spend_category_id   UUID NOT NULL REFERENCES spend_categories (spend_category_id),
  source              TEXT NOT NULL CHECK (source IN ('manual', 'rule', 'vault_suggest_accepted')),
  labeled_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_txn_spend_labels_category
  ON transaction_spend_labels (spend_category_id);

-- Closed starter system set (wireframe legend). Uncategorized is absence of a label.
INSERT INTO spend_categories (spend_category_id, slug, display_name, is_system)
VALUES
  ('a1000000-0000-4000-8000-000000000001', 'rent', 'Rent', TRUE),
  ('a1000000-0000-4000-8000-000000000002', 'groceries', 'Groceries', TRUE),
  ('a1000000-0000-4000-8000-000000000003', 'dining', 'Dining', TRUE),
  ('a1000000-0000-4000-8000-000000000004', 'utilities', 'Utilities', TRUE),
  ('a1000000-0000-4000-8000-000000000005', 'transport', 'Transport', TRUE),
  ('a1000000-0000-4000-8000-000000000006', 'transfer', 'Transfer', TRUE),
  ('a1000000-0000-4000-8000-000000000007', 'other', 'Other', TRUE)
ON CONFLICT (slug) DO NOTHING;
