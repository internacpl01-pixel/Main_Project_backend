-- =============================================================================
-- 005_email_and_otp.sql
-- Adds an email address to admin.users (needed to match a Google account or
-- to know where to send a one-time login code -- neither existed before this,
-- login has only ever taken a username) and a table to hold outstanding OTPs.
-- Applied to the 'admin' schema by db.migrate.py.
-- =============================================================================

-- Nullable: every existing account predates this column, and an account with
-- no email simply cannot use Google sign-in or OTP login -- it keeps working
-- with username+password exactly as it does today. An admin fills it in later
-- from the Users page for whichever accounts want the new options.
ALTER TABLE admin.users ADD COLUMN email text;

-- Case-insensitive and partial (WHERE email IS NOT NULL): two blank emails
-- must not collide with each other the way two real, differently-cased
-- addresses for the same inbox must not both exist.
CREATE UNIQUE INDEX admin_users_email_unique
    ON admin.users (lower(email))
    WHERE email IS NOT NULL;

-- One row per code sent. Never the plain code -- code_hash only, same
-- reasoning as password_hash: a leaked row should not itself be a working
-- credential. A row is single-use (consumed_at) and time-limited
-- (expires_at), and the email is kept even though it is not a foreign key to
-- admin.users.email -- a code is sent to whatever address was typed, and
-- looking rows up by that address is what both the resend cooldown and the
-- verify step need, independent of whether the address still matches an
-- account by the time someone tries to use it.
CREATE TABLE admin.login_otps (
    id          bigserial   PRIMARY KEY,
    email       text        NOT NULL,
    code_hash   text        NOT NULL,
    expires_at  timestamptz NOT NULL,
    consumed_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Read on every request (cooldown check) and every verify (latest
-- unconsumed row for this email) -- both filter on lower(email) first.
CREATE INDEX admin_login_otps_email ON admin.login_otps (lower(email));
