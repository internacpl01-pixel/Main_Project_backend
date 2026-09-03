-- =============================================================================
-- 028_rule_table.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- The rule behind Check Rules, as data instead of Python.
--
-- Until now the rule lived in backend/services/rules.py as a literal: RERA
-- credits had to be "Master 2 RERA", RERA debits "RERA 2 IDW" or "Cust
-- Cancellation", and the three names were matched against this company's heads
-- by folding their spelling. Adding a rule for MASTER, IDW or FREE meant
-- editing that file and redeploying, and the Check Rules dialog said so out
-- loud: "No rules are written for MASTER accounts yet."
--
-- What a rule actually is, in this business: a head is a legitimate answer for
-- some account types and not others, and on those types only in one direction.
-- "Master 2 RERA" is money leaving the Master account and arriving in the RERA
-- account -- so it is a DEBIT on a MASTER account, a CREDIT on a RERA account,
-- and meaningless on IDW or FREE. One row here says exactly that much:
--
--     head_id -> Master 2 RERA,  account_type -> RERA,    direction -> CR
--     head_id -> Master 2 RERA,  account_type -> MASTER,  direction -> DR
--
-- and no row at all for IDW or FREE, which is how "blank" is stored. The Rules
-- screen renders these as the grid people think in -- one row per head, one
-- column per account type -- but the storage is one fact per row, so adding an
-- account type on the Master Data page adds a column with no migration and no
-- code change. That is the same reason bank_master stores account_type as the
-- name rather than as a reference; see company/015_bank_account_type.sql.
--
-- The rule table is now the ONLY thing that decides both questions Check Rules
-- asks: which heads a row may legitimately carry (so which rows come back red),
-- and which heads the Replace dropdown offers. One source for both, because a
-- dropdown that offers a head the check would still reject is worse than no
-- dropdown at all.
-- =============================================================================

CREATE TABLE IF NOT EXISTS rule (
    id           bigserial PRIMARY KEY,

    -- The heads come from rera_head_master because that is the master Check
    -- Rules writes into: the value it puts on a row is a rera_head_id, so a
    -- head offered from anywhere else could be shown but never saved. CASCADE
    -- rather than RESTRICT: deleting a head is a Master Data decision, and a
    -- rule row for a head that no longer exists is not a fact worth keeping.
    head_id      bigint NOT NULL
                 REFERENCES rera_head_master(id) ON DELETE CASCADE,

    -- The name, not an id, matching bank_master.account_type -- these two are
    -- compared to each other, so one holding a different form of the same value
    -- is the kind of mistake nobody spots by eye. Upper-cased on write for the
    -- same reason account_type_master upper-cases its own names.
    account_type text NOT NULL,

    -- BOTH is a real answer, not a convenience: a head like "Reversed" or
    -- "Internal" can legitimately land on either side of the same account.
    direction    text NOT NULL,

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rule_direction_check CHECK (direction IN ('CR', 'DR', 'BOTH')),
    CONSTRAINT rule_account_type_upper CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT rule_account_type_filled CHECK (btrim(account_type) <> ''),

    -- One answer per head per account type. Two rows saying the same head is
    -- both CR and DR on RERA would be BOTH written in a way neither screen
    -- could show, and the grid has one cell for it.
    CONSTRAINT rule_head_type_unique UNIQUE (head_id, account_type)
);

-- The lookup Check Rules makes on every run: everything allowed for one type.
CREATE INDEX IF NOT EXISTS rule_account_type_idx ON rule (account_type);

-- =============================================================================
-- Seed: exactly the rule that was hardcoded, and nothing more.
--
-- Three rows, so that the day this migration lands the Check Rules button
-- reports precisely what it reported the day before -- same conflicts, same
-- suggested fixes. Everything else on the grid starts blank and is the user's
-- to fill in, which is the whole point of the table existing.
--
-- Deliberately NOT seeding "Master 2 RERA -> MASTER -> DR", true though it is.
-- One row under MASTER would turn the rule on for MASTER accounts and flag
-- every debit that is not a transfer to RERA, which is most of them. A type
-- with no rows has no rule and is reported as such; that is the honest state
-- until somebody fills the column in.
--
-- Matched on the spelling folded the way services/rules.py folded it: lower
-- case, and every run of punctuation collapsed to one space, so 'Master-2-RERA'
-- and 'Master 2 RERA' agree. The join word is then matched as (2|to) rather
-- than rewritten, because a rewrite needs a backreference in the replacement
-- and \1 is read as the character U+0001 here, not as a capture -- which
-- silently folded 'Master 2 RERA' to 'master<0x01>to<0x02>rera' and matched
-- nothing. Verified against all three live companies: exactly these three
-- heads match, and no head matches two patterns.
--
-- A company whose rera_head_master lacks a head simply gets no row for it: a
-- rule naming a head nobody has is a rule that can never pass, and starting
-- empty is better than starting broken.
-- =============================================================================

INSERT INTO rule (head_id, account_type, direction)
SELECT h.id, 'RERA', 'CR'
  FROM rera_head_master h
 WHERE regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
       ~ '^ *master +(2|to) +rera *$'
ON CONFLICT (head_id, account_type) DO NOTHING;

INSERT INTO rule (head_id, account_type, direction)
SELECT h.id, 'RERA', 'DR'
  FROM rera_head_master h
 WHERE regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
       ~ '^ *rera +(2|to) +idw *$'
ON CONFLICT (head_id, account_type) DO NOTHING;

INSERT INTO rule (head_id, account_type, direction)
SELECT h.id, 'RERA', 'DR'
  FROM rera_head_master h
 WHERE regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
       ~ '^ *(cust|customer) +cancellation *$'
ON CONFLICT (head_id, account_type) DO NOTHING;
