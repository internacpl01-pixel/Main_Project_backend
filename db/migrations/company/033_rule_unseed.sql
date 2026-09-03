-- =============================================================================
-- 033_rule_unseed.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Take the three seeded rules back out. Every rule is the company's own now.
--
-- 028 created the `rule` grid and seeded it with exactly the three facts the
-- old hardcoded Python rule asserted:
--
--     RERA  CR  Master 2 RERA
--     RERA  DR  RERA 2 IDW
--     RERA  DR  Cust Cancellation
--
-- and 032 re-seeded them when it reversed 031. That was the right call at the
-- time: moving a rule from code into a table should not change what Check Rules
-- reports on the day it lands, so the seed made the move invisible.
--
-- The move is done, and the user has since written their own rules -- MASTER
-- and IDW columns the seed never knew about, and the first condition. So the
-- seed has stopped being a safety net and started being the last thing in the
-- system asserting an accounting fact nobody in the company entered. Asked on
-- 2026-09-03 for rules to come only from the table, and this is what was left.
--
-- WHAT THIS DELETES, precisely: a row is removed only when its account type,
-- its direction AND its head name all match what 028 inserted -- matched with
-- 028's own regexes, folded the same way, so 'Master 2 RERA' and 'Master to
-- RERA' are both caught. Anything else is untouched:
--
--   * a hand-written row on any other account type stays (company_028's
--     MASTER and IDW rows),
--   * a hand-written row on RERA against any other head stays,
--   * a seeded head whose direction the user has since CHANGED stays, because
--     the direction no longer matches -- that edit is theirs, not the seed's.
--
-- Nothing here touches rule_condition. Conditions were never seeded.
--
-- AFTERWARDS: a company whose whole grid was the seed has an empty grid, and
-- Check Rules will say "No rule is set for RERA accounts yet" until somebody
-- fills it in on the Rules page. That is the intended state -- an empty grid is
-- an honest "nobody has said yet", where the seed was this file speaking for
-- them. Re-adding any of the three is one dropdown on the Rules page.
--
-- New companies still run 028 and 032 first and are unseeded by this file a
-- moment later; 028 and 032 cannot be edited, because an applied migration is
-- checksummed and changing one hard-fails the whole run.
-- =============================================================================

DO $$
DECLARE
    credits int;
    debits  int;
BEGIN
    -- Money arriving in a RERA account from the Master account.
    DELETE FROM rule r
     USING rera_head_master h
     WHERE h.id = r.head_id
       AND r.account_type = 'RERA'
       AND r.direction = 'CR'
       AND regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
           ~ '^ *master +(2|to) +rera *$';
    GET DIAGNOSTICS credits = ROW_COUNT;

    -- Money leaving a RERA account: the two heads the old rule allowed.
    DELETE FROM rule r
     USING rera_head_master h
     WHERE h.id = r.head_id
       AND r.account_type = 'RERA'
       AND r.direction = 'DR'
       AND (regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
                ~ '^ *rera +(2|to) +idw *$'
         OR regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
                ~ '^ *(cust|customer) +cancellation *$');
    GET DIAGNOSTICS debits = ROW_COUNT;

    RAISE NOTICE 'unseeded % rule row(s); % left, written by the company',
        credits + debits, (SELECT count(*) FROM rule);
END $$;
