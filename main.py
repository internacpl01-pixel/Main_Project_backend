"""
Company Ledger API — FastAPI entry point.

Mounts every router and owns the asyncpg pool's lifespan. Nothing else belongs
here: routes live in routers/, business logic in services/, SQL in the layer
below that.

Restored 2026-08-18 after main.py was overwritten with a "DPL Data Bank"
skeleton that mounted only /health and a duplicate staging router. The router
list below is the one recovered in main.py.previous-routes.txt.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import close_pool, init_pool
from routers import (auth, companies, custom_fields, export, fieldmap, imports,
                     master, projects, transactions, users)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the connection pool before the first request, close it after the last.

    The pool is a module-level singleton in database.py, so this runs once per
    process. company_connection() raises a clear error if it is called before
    this has run, rather than failing with a None dereference.
    """
    await init_pool()
    yield
    await close_pool()


app = FastAPI(
    title="Company Ledger API",
    description="Schema-per-tenant accounting API for construction companies.",
    version="0.1.0",
    lifespan=lifespan,
)

# apiClient.js always builds an absolute baseURL (http://localhost:8000), so
# every browser request is cross-origin even in development and CORS is not
# optional. A regex rather than a fixed list because Vite falls back to 3001,
# 3002... when 3000 is already taken, and a preflight from the fallback port
# would otherwise fail with no obvious cause.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    max_age=600,
)

# Order is cosmetic (it sets the grouping in /docs) except that auth comes
# first because every other router depends on it.
app.include_router(auth.router)
app.include_router(companies.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(master.router)
app.include_router(fieldmap.router)
app.include_router(custom_fields.router)
app.include_router(imports.router)
app.include_router(transactions.router)
app.include_router(export.router)


@app.get("/", tags=["meta"])
async def root():
    """Liveness probe and a pointer to the docs."""
    return {"status": "ok", "service": "Company Ledger API", "docs": "/docs"}


@app.get("/health", tags=["meta"])
async def health():
    """Kept from the overwritten skeleton — deployment probes may point at it."""
    return {"status": "ok"}
