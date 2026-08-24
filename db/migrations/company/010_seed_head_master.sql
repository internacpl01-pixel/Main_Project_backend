-- Seed head_master (shown in the UI as "Internal Head") with DPL's standing
-- list of heads, so a new company starts usable instead of with an empty table
-- that blocks classification entirely — StagingPage refuses to classify a row
-- until at least one head exists.
--
-- category is left NULL: these arrived as a flat list, and the column is an
-- optional grouping. Fill it in from the Master Data screen if it is ever
-- wanted; nothing here depends on it.
--
-- Names are verbatim, including "Legal & Proff." next to "Professional" —
-- those are two different heads in DPL's books, not a typo to be tidied.
--
-- Guarded by NOT EXISTS rather than ON CONFLICT because the table's UNIQUE is
-- (name, category) and category is NULL here. In Postgres NULL is never equal
-- to NULL, so that constraint would NOT catch a repeat — ON CONFLICT would
-- insert a second copy of every row on a re-run. Matching on lower(name) also
-- means a company that already typed "vendor" by hand keeps its own row rather
-- than gaining a near-duplicate.
INSERT INTO head_master (name)
SELECT v.name
FROM (VALUES
    ('Vendor'),
    ('Salary Site'),
    ('Salary HO'),
    ('Imprest'),
    ('Contractor'),
    ('Professional'),
    ('Internal'),
    ('Legal & Proff.'),
    ('Statutory Dues'),
    ('Cancellation'),
    ('HO - Advert/Mkt'),
    ('Collection'),
    ('EPF/ESI')
) AS v(name)
WHERE NOT EXISTS (
    SELECT 1 FROM head_master h WHERE lower(h.name) = lower(v.name)
);
