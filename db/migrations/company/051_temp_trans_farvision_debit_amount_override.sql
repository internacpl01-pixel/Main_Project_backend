-- Backs the Farvision Verify page's TDS Rate reverse-calculation: picking a
-- rate grosses up the row's Debit Amount (currently the net amount actually
-- paid, after TDS) into the gross amount, and Adjustment Amount is set to
-- that same gross figure -- Credit Amount is never touched. Confirmed with
-- the user with a worked example (98,000 net at 2% -> 100,000 gross for
-- both fields). Same NUMERIC(18,2) precision as the field_num_* columns this
-- overrides (see services/custom_fields.py's own numeric field type).
ALTER TABLE temp_trans
    ADD COLUMN IF NOT EXISTS farvision_debit_amount_override numeric(18,2);
