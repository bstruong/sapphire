-- Nullable payee / counterparty on transactions (category spend step 2).
-- NULL when the source has no counterparty; never invent. description stays
-- the raw bank string (fail-loud audit trail). Empty string is rejected in
-- the CanonicalTransaction model (not via DB CHECK — NULL is allowed).

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS payee TEXT;
