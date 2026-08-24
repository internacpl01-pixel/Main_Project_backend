-- Seed rera_head_master with DPL's standing RERA head list, for the same
-- reason as 010: a company with an empty head table cannot classify anything.
--
-- Names are verbatim. Several look irregular and are deliberately left alone,
-- because these are the strings DPL's existing books already use and a tidier
-- spelling here would simply fail to match them:
--   '? (Loan Repayment )'  leading '?', and a space before the ')'
--   'Dev- Apt' / 'Credit- no effect' / 'TDS - Cons'  inconsistent hyphen spacing
--   'HO - Advert/ Mkt'  spaced after the slash, where head_master has
--                       'HO - Advert/Mkt' without. Two tables, so nothing
--                       collides, but they are not the same string.
--
-- rera_head_master.name is UNIQUE on its own (unlike head_master, which is
-- unique on name+category), so ON CONFLICT would work here. NOT EXISTS on
-- lower(name) is used anyway to match 010 and to catch a company that already
-- typed one of these in a different case.
INSERT INTO rera_head_master (name)
SELECT v.name
FROM (VALUES
    ('? (Loan Repayment )'),
    ('Credit- no effect'),
    ('Cust Cancellation'),
    ('Customer Collection'),
    ('Dev- Apt'),
    ('Dev- Infra'),
    ('EDC/ IDC'),
    ('Free & IDW Loan'),
    ('HO - Admin'),
    ('HO - Advert/ Mkt'),
    ('Internal'),
    ('Land Payment'),
    ('Master 2 RERA'),
    ('Master to Free'),
    ('Opening Balance'),
    ('RERA 2 IDW'),
    ('Reversed'),
    ('TDS - Cons'),
    ('Check'),
    ('AH - CR'),
    ('Security Refundable'),
    ('Promoter Contribution')
) AS v(name)
WHERE NOT EXISTS (
    SELECT 1 FROM rera_head_master r WHERE lower(r.name) = lower(v.name)
);
