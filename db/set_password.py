"""
Rotate a user's password.

    python -m db.set_password admin

Prompts for the new password without echoing it, bcrypt-hashes it, and updates
admin.users. Nothing is written to disk, no shell history entry, and the
plaintext never leaves this process.

This exists because migrate.py's `init` only seeds a super admin when the users
table is empty -- correct, since re-running it must not silently reset a live
password -- which left no way to change one afterwards. Use this instead of
editing SUPER_ADMIN_PASSWORD in .env and re-seeding.
"""
import asyncio
import getpass
import sys

import asyncpg
import bcrypt

import config

MIN_LENGTH = 4


async def set_password(username: str, password: str) -> None:
    conn = await asyncpg.connect(config.DATABASE_URL)
    try:
        pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))
        updated = await conn.fetchval(
            """
            UPDATE admin.users
            SET password_hash = $1
            WHERE username = $2
            RETURNING username
            """,
            pw_hash.decode("utf-8"),
            username,
        )
    finally:
        await conn.close()

    if updated is None:
        print(f"[ERROR] No user named {username!r} in admin.users.", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Password updated for {updated}.")
    print("     Existing JWTs stay valid until they expire -- rotate JWT_SECRET")
    print("     too if you need to invalidate current sessions immediately.")


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python -m db.set_password <username>", file=sys.stderr)
        sys.exit(1)

    username = sys.argv[1]
    first = getpass.getpass(f"New password for {username}: ")
    if len(first) < MIN_LENGTH:
        print(f"[ERROR] Too short -- use at least {MIN_LENGTH} characters.", file=sys.stderr)
        sys.exit(1)
    if first != getpass.getpass("Confirm: "):
        print("[ERROR] Passwords do not match.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(set_password(username, first))


if __name__ == "__main__":
    main()
