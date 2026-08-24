-- Seed idw_head_master (shown in the UI as "TCP Head") with DPL's standing
-- list, completing the set started in 010 and 011: a company created from here
-- on has all three head tables populated and can classify from the start.
--
-- Names verbatim, irregularities included, because these match the strings
-- DPL's books already use:
--   '?'                     a real head, not a placeholder — the RERA list has
--                           its own '? (Loan Repayment )'
--   'Const Const'           not a typo for 'Const Costs'
--   'Other- Selling Expenses'  hyphen spacing follows the RERA list's style
--   'Internal transfer'     lowercase 't', unlike 'Internal' in the other two
--
-- Same NOT EXISTS guard as 010/011. idw_head_master.name is UNIQUE on its own,
-- so ON CONFLICT would also work, but matching on lower(name) additionally
-- protects a company that already typed one of these in a different case.
INSERT INTO idw_head_master (name)
SELECT v.name
FROM (VALUES
    ('IDW Civil Works'),
    ('IDW Other'),
    ('EDW External Const'),
    ('EDW Road Work'),
    ('EDW Other'),
    ('Const Const'),
    ('Const Others'),
    ('Reversed'),
    ('Other- Selling Expenses'),
    ('Other- Land Cost'),
    ('Other- Administrative Expenses'),
    ('Other- Others'),
    ('AH Other Project'),
    ('Internal transfer'),
    ('Credit- no effect'),
    ('OTS'),
    ('?')
) AS v(name)
WHERE NOT EXISTS (
    SELECT 1 FROM idw_head_master i WHERE lower(i.name) = lower(v.name)
);
