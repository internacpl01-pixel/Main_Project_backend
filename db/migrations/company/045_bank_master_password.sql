-- The PDF password this bank account's own statements are protected with --
-- typed and edited by hand on the Master Data page's Bank tab, not derived
-- from anything. Plain text: this is a bank statement's own unlock code, not
-- a login credential, and the point of storing it is to read it back later
-- (an import feature can offer it automatically instead of asking every
-- time it sees a protected file from this account) -- confirmed with the
-- user. Nullable: most accounts' statements are not password-protected at
-- all, and NULL says that plainly rather than an empty string standing in
-- for "not set".
ALTER TABLE bank_master
    ADD COLUMN IF NOT EXISTS password text;
