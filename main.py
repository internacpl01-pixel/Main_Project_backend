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

# apiClient.js always builds an absolute baseURL, so every browser request is
# cross-origin even in development and CORS is not optional.
#
# Every origin is allowed. This previously matched localhost only, which meant
# the frontend worked from `npm run dev` and was blocked the moment it was
# deployed anywhere — and the failure surfaces as "no data" in the browser
# rather than as an error the server ever sees.
#
# "*" and allow_credentials=True look contradictory, because a literal
# `Access-Control-Allow-Origin: *` is rejected by browsers on any credentialed
# request. Starlette resolves it: with credentials enabled it echoes the
# caller's own Origin back instead of "*" (cors.py — `if self.allow_all_origins
# and self.allow_credentials: self.allow_explicit_origin(...)`), on both the
# preflight and the real response. So this is spec-correct, not a wildcard the
# browser has to forgive. It does mean the behaviour depends on that Starlette
# version — pinning starlette below 1.x would silently send "*" again and every
# credentialed request would start failing.
#
# What this gives up: any website a signed-in user visits can call this API
# from their browser. The saving grace is that auth here is a Bearer token read
# from localStorage, which is origin-scoped and never attached automatically —
# so a third-party page cannot borrow a session the way it could with cookie
# auth. Narrow this to the deployed frontend's origin when it has a fixed one.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
