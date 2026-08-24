-- =============================================================================
-- 020_seed_head_master_full.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- DPL's full internal head list: 93 names, replacing the 13 that 010 seeded as
-- a starting set.
--
-- ADDITIVE, not a replacement. Nothing already in head_master is renamed,
-- deactivated or removed, because by the time this ran the table was in use:
-- company_028 carries 145 beneficiaries booked against these heads, and both
-- temp_trans and transactions carry head_id references. Four heads survive that
-- are not in the new list -- 'HO - Advert/Mkt', 'Salary HO', 'Salary Site',
-- 'Statutory Dues', plus 'Brokerage', 'Site Expenses' and 'Customer Receipts'
-- in company_001 and company_009 -- and they stay, as asked.
--
-- CASE. Nine of these already exist in a different capitalisation: 'Vendor'
-- against 'VENDOR', 'Collection' against 'COLLECTION', and so on. The guard
-- below matches on lower(name), so those are recognised as already present and
-- keep their existing spelling rather than gaining a near-duplicate row.
--
-- That is deliberate and it is the conservative choice, not the tidy one. The
-- alternative -- renaming them to the capitals used here -- would leave 145
-- beneficiaries holding head1/2/3 text that no longer matches any master row,
-- because those columns store the NAME rather than a reference (see 013). The
-- table therefore ends up with mixed capitalisation, and normalising it is a
-- separate job that has to update beneficiary_master in the same transaction.
--
-- 'SALARY-HO' and 'SALARY-SITE' are added as NEW heads and the existing
-- 'Salary HO' and 'Salary Site' are left alone. They look like renames and are
-- not treated as such: 24 beneficiaries point at the old spellings, and turning
-- a guess about intent into an UPDATE across live data is not this file's job.
-- Merge them from the Master Data screen if they are meant to be one.
--
-- Names verbatim, including 'COLABREATION  SEC-23' with its two spaces and the
-- bare '?', which is a real head here exactly as it is in the RERA and TCP
-- lists.
-- =============================================================================

INSERT INTO head_master (name)
SELECT v.name
FROM (VALUES
    ('?'),
    ('AAKRITI'),
    ('ADISH JAIN'),
    ('AHRWA'),
    ('AMAN & CO'),
    ('AMBITION'),
    ('BANK CHARGES'),
    ('BONUS'),
    ('BOOKING'),
    ('BOUNCE'),
    ('BOUNCE RECOVER'),
    ('CANCELLATION'),
    ('CARD'),
    ('CASA DEV'),
    ('COLLECTION'),
    ('DIRECTOR REM'),
    ('EMI'),
    ('EXOTIC'),
    ('FDR'),
    ('FEES RATE & TAXES'),
    ('INTEREST'),
    ('INTERNAL'),
    ('LEGAL & PROFF.'),
    ('LOAN'),
    ('LOAN RECOVERY'),
    ('MBPL'),
    ('MEPL'),
    ('MISC'),
    ('NAVTECH'),
    ('OBOC'),
    ('OTHER COS'),
    ('PANDA'),
    ('PLP'),
    ('PROFESSIONAL'),
    ('RADHE'),
    ('RENTAL'),
    ('RTB'),
    ('SALARY'),
    ('SELF'),
    ('SKG BUILDCON'),
    ('TAX'),
    ('VENDOR'),
    ('STAMP PAPER'),
    ('VIEVEK SIR'),
    ('A.RENTAL'),
    ('EPF/ESI'),
    ('MLPL'),
    ('DPL'),
    ('VAT REFUND'),
    ('FULL & FINAL'),
    ('CAR 24'),
    ('RERA'),
    ('DD'),
    ('REIMBURSEMENT'),
    ('AUDIT FEE'),
    ('AXIS EMI'),
    ('SBI EMI'),
    ('ARRER SALARY'),
    ('CONTRACTOR'),
    ('COLABREATION  SEC-23'),
    ('REFUND'),
    ('PANTRY MATERIAL'),
    ('VECH.SALE'),
    ('IMPREST'),
    ('SHOP RENT RECEIVED'),
    ('INSURANCE'),
    ('WAGES'),
    ('LEI'),
    ('INVESTMENT'),
    ('MKT/ADVER'),
    ('EXOTIC BUILDWELL'),
    ('REPAIR & MAINT'),
    ('M TECH'),
    ('COMMISSION'),
    ('BG RENEWAL'),
    ('TENDER FEE'),
    ('DHBVN'),
    ('SUSPENSE'),
    ('OFFICE RENT'),
    ('IDW TO FREE LOAN'),
    ('FREE TO IDW LOAN'),
    ('SALARY-HO'),
    ('SALARY-SITE'),
    ('VENDOR - HO'),
    ('VENDOR -SITE'),
    ('REFUNDABLE SECURITY'),
    ('FOREIGN TRAVELLING EXP'),
    ('OFFICE EQUIPMENT'),
    ('ROC FEES'),
    ('PROFESSIONAL INCOME'),
    ('FREIGHT EXPENSES'),
    ('SALE'),
    ('STIPEND')
) AS v(name)
WHERE NOT EXISTS (
    SELECT 1 FROM head_master h WHERE lower(h.name) = lower(v.name)
);
