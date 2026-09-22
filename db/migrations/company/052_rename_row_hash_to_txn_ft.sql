-- Renames temp_trans.row_hash to txn_ft, per the user's naming preference.
-- Same column, same values, same purpose (the content-fingerprint dedup
-- signal from 001/002) -- only the name changes, and every reference to it
-- in Python (import_helpers.py, routers/imports.py, services/custom_fields.py)
-- was updated in the same change.
--
-- Postgres folds an unquoted identifier to lowercase, so "txn_FT" as typed
-- becomes txn_ft here and in every query against it -- deliberately not
-- quoted to force mixed case, since every other column in this schema is
-- plain lowercase snake_case and a quoted exception would need quoting on
-- every single reference to it forever after.
ALTER TABLE temp_trans RENAME COLUMN row_hash TO txn_ft;

-- Cosmetic, but kept in step: an index still named after the column's old
-- name would be a small, permanent "why doesn't this match" left for whoever
-- next reads \d temp_trans.
ALTER INDEX idx_temp_trans_row_hash RENAME TO idx_temp_trans_txn_ft;
