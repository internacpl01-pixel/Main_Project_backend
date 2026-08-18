"""
Connection smoke test. Confirms DATABASE_URL works BEFORE we write SQL.
Run: python check_db.py
Expect: 'Connected.' line and a server version string.
"""
import asyncio
import sys

import asyncpg

import config


async def main():
    # Mask the password in the print, keep the host visible for debugging
    if "@" in config.DATABASE_URL:
        host_part = config.DATABASE_URL.split("@", 1)[1]
    else:
        host_part = config.DATABASE_URL
    print(f"Connecting to: ...@{host_part}")

    try:
        conn = await asyncpg.connect(config.DATABASE_URL)
    except Exception as e:
        print(f"\n[CONNECTION FAILED] {type(e).__name__}: {e}\n")
        print("Common causes:")
        print("  - Wrong host/port. Use Session pooler on port 5432, NOT 6543.")
        print("  - Password contains @, #, /, ?  -- must be percent-encoded.")
        print(" @ -> %40, # -> %23, / -> %2F, ? -> %3F, : -> %3A")
        print("  - Username must be postgres.YOUR_PROJECT_REF (dotted), not plain postgres.")
        print("  - Supabase project is paused in dashboard.")
        return 1

    version = await conn.fetchval("SELECT version()")
    await conn.close()

    print(f"\n[OK] Connected.")
    print(f"  Server: {version}")
    print(f"  Now safe to run migrations.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))