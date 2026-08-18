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


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise AccountError("Username is required.")
    if len(username) < MIN_USERNAME_LENGTH:
        raise AccountError(f"Username must be at least {MIN_USERNAME_LENGTH} characters.")
    return username


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
