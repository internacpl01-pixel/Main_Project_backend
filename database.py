"""
Runtime database access layer.

- Pool of asyncpg connections (Phase 2 onwards).
- Per-request schema switching via SET LOCAL inside a transaction.
- This is where the Step 0 fix #1 lives. Without it, the SET lands on one
  pool connection and the query runs on another, silently reading the wrong
  company.
"""
from contextlib import asynccontextmanager

import asyncpg

import config

_pool: asyncpg.Pool | None = None


async def init_pool():
    """Create the connection pool. Call once at app startup (Phase 2)."""
    global _pool
    _pool = await asyncpg.create_pool(
        config.DATABASE_URL,
        min_size=config.DB_POOL_MIN,
        max_size=config.DB_POOL_MAX,
    )


async def close_pool():
    """Close the pool. Call once at app shutdown (Phase 2)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def company_connection(schema: str):
    """
    Borrow a connection from the pool, set search_path to the given company
    schema, yield it for the duration of one logical request, and release it.

    All queries inside the `async with` block read/write ONLY that company's
    tables. The `admin` schema is also in search_path so admin.set_updated_at()
    triggers still resolve.

    Usage (Phase 2):
        async with company_connection("company_001") as conn:
            rows = await conn.fetch("SELECT * FROM projects")
    """
    if _pool is None:
        raise RuntimeError(
            "Pool not initialized. Call init_pool() at app startup "
            "before using company_connection()."
        )

    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f'SET LOCAL search_path TO "{schema}", admin')
            yield conn


@asynccontextmanager
async def raw_connection():
    """
    Borrow a pool connection with no schema switch and no wrapping transaction.

    company_connection() pins search_path inside a transaction, which is exactly
    wrong for provisioning a new company: CREATE SCHEMA plus the migration files
    need to open their own transaction and set search_path per file. Anything
    using this must schema-qualify its table names (admin.companies, not
    companies).
    """
    if _pool is None:
        raise RuntimeError(
            "Pool not initialized. Call init_pool() at app startup "
            "before using raw_connection()."
        )

    async with _pool.acquire() as conn:
        yield conn