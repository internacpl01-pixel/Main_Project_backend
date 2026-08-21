"""
Account service — create, edit, delete and re-role rows in admin.users.

Everything here is company-scoped by an explicit company_id argument rather
than by search_path. admin.users is a single cross-company table, so a missing
WHERE company_id = $n is the difference between a manager editing their own
staff and a manager editing another company's. The scope is a required
parameter on every function for that reason, never a default.

Callers do the hierarchy checks (permissions.can_create / can_edit) before
calling in; this layer only enforces what the database itself guarantees.
"""
import bcrypt

# Project policy: minimum 4 characters for both username and password.
MIN_USERNAME_LENGTH = 3
MIN_PASSWORD_LENGTH = 4


class AccountError(Exception):
    """Invalid account input. Carries a message fit to show a user."""


def hash_password(password: str) -> str:
    """bcrypt hash, cost 12 — same cost migrate.py seeds the super-admin with."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Does this password match the stored hash?

    bcrypt refuses a password over 72 bytes rather than truncating it, and a
    stored hash can be malformed if it was ever written by hand — both raise,
    and neither is a reason to return a 500 to someone typing in a login box.
    Either way the answer to "is this the right password" is no.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise AccountError("Username is required.")
    if len(username) < MIN_USERNAME_LENGTH:
        raise AccountError(f"Username must be at least {MIN_USERNAME_LENGTH} characters.")
    return username


async def company_code_of(conn, company_id: int) -> str | None:
    """The three-letter code every username in this company must start with.

    None for a company registered before codes existed. Callers treat that as
    "no rule to apply" rather than as a failure — those companies' accounts were
    named before there was a convention, and breaking their logins to enforce
    one retroactively helps nobody.
    """
    return await conn.fetchval("SELECT code FROM admin.companies WHERE id = $1", company_id)


async def enforce_code_prefix(conn, username: str, company_id: int) -> str:
    """Require username to read `code-something`, when the company has a code.

    The prefix is what makes a username say which company it belongs to. Login
    resolves an account from the username alone, before any company is known, so
    this is the only place that fact is visible at the moment it is needed.

    Checked when an account is created or renamed, never at login: accounts that
    predate their company's code keep working exactly as they are.
    """
    code = await company_code_of(conn, company_id)
    if not code:
        return username

    prefix = f"{code}-"
    if username.lower().startswith(prefix):
        # Fold the prefix to its canonical lowercase form so 'DPL-ravi' and
        # 'dpl-ravi' cannot both exist. The rest of the name is left as typed.
        rest = username[len(prefix):]
        if not rest.strip():
            raise AccountError(
                f"'{username}' is only the company prefix. Add a name after it, "
                f"like {prefix}ravi."
            )
        return prefix + rest

    raise AccountError(
        f"Usernames in this company must start with '{prefix}'. "
        f"Try '{prefix}{username.lower()}'."
    )


def validate_password(password: str) -> str:
    password = password or ""
    if not password:
        raise AccountError("Password is required.")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return password


async def list_accounts(conn, company_id: int) -> list[dict]:
    """Every account belonging to one company, most privileged first."""
    rows = await conn.fetch(
        """
        SELECT id, username, role, is_active, created_at, updated_at
        FROM admin.users
        WHERE company_id = $1
        ORDER BY
            CASE role
                WHEN 'company_admin' THEN 0
                WHEN 'manager'       THEN 1
                ELSE 2
            END,
            username
        """,
        company_id,
    )
    return [dict(r) for r in rows]


async def get_account(conn, user_id: int, company_id: int) -> dict | None:
    """
    One account, but only if it belongs to company_id.

    Scoping the read is what stops /users/{id} from being an oracle for other
    companies' usernames — an out-of-company id is indistinguishable from a
    missing one.
    """
    row = await conn.fetchrow(
        """
        SELECT id, username, role, is_active, company_id, created_at, updated_at
        FROM admin.users
        WHERE id = $1 AND company_id = $2
        """,
        user_id,
        company_id,
    )
    return dict(row) if row else None


async def create_account(conn, *, username: str, password: str, role: str, company_id: int) -> dict:
    """
    Create one company account. Raises AccountError on bad input or a taken name.

    Usernames are unique across the whole install, not per company — login
    resolves a user from the username alone, before any company is known.
    """
    username = validate_username(username)
    username = await enforce_code_prefix(conn, username, company_id)
    password = validate_password(password)

    taken = await conn.fetchval("SELECT 1 FROM admin.users WHERE lower(username) = lower($1)", username)
    if taken:
        raise AccountError(f"Username '{username}' is already taken.")

    row = await conn.fetchrow(
        """
        INSERT INTO admin.users (username, password_hash, role, company_id)
        VALUES ($1, $2, $3, $4)
        RETURNING id, username, role, is_active, created_at, updated_at
        """,
        username,
        hash_password(password),
        role,
        company_id,
    )
    return dict(row)


async def update_account(
    conn, user_id: int, company_id: int, *, username: str | None = None, password: str | None = None
) -> dict | None:
    """
    Change a username, a password, or both. Fields left None are untouched.
    Returns None if no such account in this company.
    """
    sets = []
    params = []

    if username is not None:
        username = validate_username(username)
        # Renaming has to obey the same rule as creating, or the prefix is a
        # convention you can opt out of one edit after joining.
        username = await enforce_code_prefix(conn, username, company_id)
        taken = await conn.fetchval(
            "SELECT 1 FROM admin.users WHERE lower(username) = lower($1) AND id <> $2",
            username,
            user_id,
        )
        if taken:
            raise AccountError(f"Username '{username}' is already taken.")
        sets.append(f"username = ${len(params) + 1}")
        params.append(username)

    if password is not None:
        password = validate_password(password)
        sets.append(f"password_hash = ${len(params) + 1}")
        params.append(hash_password(password))

    if not sets:
        raise AccountError("Provide a username or a password to update.")

    params.extend([user_id, company_id])
    row = await conn.fetchrow(
        f"""
        UPDATE admin.users
        SET {", ".join(sets)}
        WHERE id = ${len(params) - 1} AND company_id = ${len(params)}
        RETURNING id, username, role, is_active, created_at, updated_at
        """,
        *params,
    )
    return dict(row) if row else None


async def set_own_password(conn, user_id: int, password: str) -> str | None:
    """Replace one account's password, keyed by id alone. Returns the username.

    Every other write in this module takes a company_id, because admin.users is
    one cross-company table and a missing scope is the difference between
    editing your own staff and editing someone else's. This one does not, and
    deliberately: it is only ever called with the id out of the caller's own
    token, so the identity IS the scope, and there is nothing wider to leak into.

    It also has to be that way for a super admin. Their row has company_id NULL,
    so `AND company_id = $n` can never match it — update_account would report
    "no such account" for the one account that is definitely signed in.
    """
    password = validate_password(password)
    return await conn.fetchval(
        """
        UPDATE admin.users
        SET password_hash = $1
        WHERE id = $2 AND is_active = true
        RETURNING username
        """,
        hash_password(password),
        user_id,
    )


async def set_account_role(conn, user_id: int, company_id: int, role: str) -> dict | None:
    """Move an account to a different rung. Returns None if not in this company."""
    row = await conn.fetchrow(
        """
        UPDATE admin.users
        SET role = $1
        WHERE id = $2 AND company_id = $3
        RETURNING id, username, role, is_active, created_at, updated_at
        """,
        role,
        user_id,
        company_id,
    )
    return dict(row) if row else None


async def delete_account(conn, user_id: int, company_id: int) -> str | None:
    """Delete an account. Returns the deleted username, or None if not found."""
    return await conn.fetchval(
        "DELETE FROM admin.users WHERE id = $1 AND company_id = $2 RETURNING username",
        user_id,
        company_id,
    )
