"""
Authentication routes.

POST /auth/login       —  form-based login → JWT token
POST /auth/switch-company — super-admin switches to a company
GET  /auth/me           —  validates the current JWT

Also home to the dependencies every other router leans on:
get_current_user, get_current_schema, get_current_company_id and require_level.
They live here because they all decode the same token; the role hierarchy they
compare against is defined once in permissions.py.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt

import config
import permissions
from database import company_connection

router = APIRouter(prefix="/auth", tags=["auth"])

# OAuth2PasswordBearer tells Swagger "there's a login form."
# tokenUrl = where the form POSTs to get a token.
# Once you log in once, Swagger stores the token and sends it
# automatically with every request. No manual copy-paste.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    """
    Dependency: validates the JWT and returns the acting identity —
    {id, username, role, level, company_id, schema}.

    Use this when a route needs to know *who* is acting — imports record
    uploaded_by, for instance. When only the schema matters, depend on
    get_current_schema instead; it is a thin wrapper over this.

    level is derived from role rather than read from the token, so a role
    renamed in permissions.py cannot be contradicted by a token minted before
    the change.
    """
    try:
        payload = jwt.decode(
            token,
            config.JWT_SECRET,
            algorithms=[config.JWT_ALGORITHM],
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    username: str = payload.get("sub")
    schema: str = payload.get("schema")

    if not username or not schema:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing required fields.",
        )

    role = payload.get("role")
    return {
        "id": payload.get("uid"),
        "username": username,
        "role": role,
        "level": permissions.level_of(role),
        "company_id": payload.get("company_id"),
        "schema": schema,
    }


# A super admin administers companies; they do not work inside one. Everything
# under a company schema — its ledger, master data, fieldmap, imports, its own
# users — belongs to that company's people.
#
# This is enforced on the two dependencies below rather than route by route,
# because every company-scoped router in the app reaches its data through one of
# them. A new router cannot forget the rule: to read a company's tables it has to
# ask which company, and asking is what applies the check.
#
# It reads as an exception to the level hierarchy, and it is. Levels answer "does
# this person outrank that one"; super_admin outranks everybody. This answers a
# different question — whose data is this — and the answer for a super admin is
# "nobody's".
_SUPER_ADMIN_SCOPE = (
    "A super admin manages companies, not the data inside them. Create the "
    "company's admin from the Companies page and sign in as a company user to "
    "work in its ledger."
)


def _refuse_super_admin(user: dict):
    if user.get("role") == "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=_SUPER_ADMIN_SCOPE
        )


async def get_company_user(user: dict = Depends(get_current_user)) -> dict:
    """
    Dependency: the acting identity, for a route that works inside a company.

    get_current_user is the raw identity and stays unguarded — /auth/me and the
    whole companies router are legitimate super-admin ground. This is the one to
    depend on anywhere `user["schema"]` will be handed to company_connection.

    Reading the schema off the identity rather than through get_current_schema
    is how a route slips past the rule: the guard is on the dependency, so a
    route that never asks it never gets checked. Several already did that, and
    a super admin reached the ledger query before failing on `relation
    "fieldmap" does not exist` — the wrong error, from the wrong layer, about
    the wrong thing.
    """
    _refuse_super_admin(user)
    return user


async def get_current_schema(user: dict = Depends(get_current_user)) -> str:
    """
    Dependency: the company schema_name this request acts on.

    Used by any protected route:
        async def my_route(schema=Depends(get_current_schema)):
            ...
    """
    _refuse_super_admin(user)
    return user["schema"]


async def get_current_company_id(user: dict = Depends(get_current_user)) -> int:
    """
    Dependency: the admin.companies.id this request acts on.

    Company users carry theirs from login. A super admin has none and cannot
    acquire one — see above — so this refuses them by role rather than reporting
    a missing company, which would read as something they could go and fix.
    """
    _refuse_super_admin(user)
    company_id = user.get("company_id")
    if company_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This account is not attached to a company.",
        )
    return company_id


def require_level(required_level: int):
    """
    Dependency factory: rejects anyone ranking below required_level.

        @router.delete("/{id}")
        async def remove(user: dict = Depends(require_level(permissions.MANAGER))):
            ...

    reads as "manager or above". Levels run downward (0 = company admin), so the
    comparison goes through permissions.has_authority rather than being written
    out here — see permissions.py.
    """
    async def checker(user: dict = Depends(get_current_user)) -> dict:
        if not permissions.has_authority(user["level"], required_level):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Requires {permissions.label_of(permissions.role_of(required_level))} "
                    f"access or higher. You are signed in as "
                    f"{permissions.label_of(user['role'])}."
                ),
            )
        return user

    return checker


@router.post("/login")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    """
    Form-based login. Username and password come from a form POST
    (not JSON body). This is what Swagger's "Authorize" button uses.

    Returns a JWT token. After this, Swagger sends the token
    automatically with every request.
    """
    username = form_data.username
    password = form_data.password

    # Open a connection to find the user.
    # For super_admin (company_id = NULL), there's no schema — use 'admin'.
    async with company_connection("admin") as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.username, u.password_hash, u.role,
                   u.company_id, c.schema_name
            FROM admin.users u
            LEFT JOIN admin.companies c ON c.id = u.company_id
            WHERE u.username = $1 AND u.is_active = true
            """,
            username,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong username or password.",
        )

    # Verify the password against the bcrypt hash in the database.
    import bcrypt as _bcrypt

    if not _bcrypt.checkpw(password.encode("utf-8"), row["password_hash"].encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong username or password.",
        )

    # Build the JWT. schema_name = 'admin' for super_admin, or the
    # company's schema for company users.
    schema = row["schema_name"] if row["company_id"] is not None else "admin"

    payload = {
        "sub": row["username"],
        "uid": row["id"],
        "role": row["role"],
        "company_id": row["company_id"],
        "schema": schema,
        "exp": datetime.now(timezone.utc)
        .timestamp() + config.JWT_EXPIRE_MINUTES * 60,
    }

    token = jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)

    return {
        "access_token": token,
        "token_type": "bearer",
        "role": row["role"],
        "level": permissions.level_of(row["role"]),
        "schema": schema,
    }


@router.get("/me")
async def me(user: dict = Depends(get_current_user)):
    """
    Returns the current identity.

    The frontend reads level and assignable_roles from here to decide which
    nav items and buttons to render. Every one of those decisions is enforced
    again server-side — this only spares the user buttons that would 403.

    company_name is read from admin.companies rather than carried in the token,
    for the same reason `level` is derived from the role above: a token minted
    before a rename would otherwise keep showing the old name until it expired,
    and JWT_EXPIRE_MINUTES is long enough for that to be the name people see all
    day. One primary-key lookup, on a route the app calls once at load and again
    after a company switch.
    """
    company_name = None
    company_code = None
    if user["company_id"] is not None:
        async with company_connection("admin") as conn:
            row = await conn.fetchrow(
                "SELECT name, code FROM admin.companies WHERE id = $1", user["company_id"]
            )
        if row is not None:
            company_name = row["name"]
            company_code = row["code"]

    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "role_label": permissions.label_of(user["role"]),
        "level": user["level"],
        "company_id": user["company_id"],
        # Both None for a super admin, who belongs to no company.
        "company_name": company_name,
        # The username prefix for this company. The Users page needs it to show
        # what a new account will be called, and to explain the rule when a name
        # is refused. Null on a company registered before codes existed.
        "company_code": company_code,
        "schema": user["schema"],
        "assignable_roles": permissions.assignable_roles(user["level"]),
    }


# POST /auth/switch-company is gone.
#
# It minted a token binding a super admin to a company schema, which is exactly
# the access the rule above withdraws. Leaving it would have been a way to get a
# token the dependencies then refuse to honour — a door to a room with no floor.
# A super admin who needs work done inside a company creates that company's
# admin from the Companies page.


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout():
    """
    Logout endpoint.

    With JWT-based auth, the token is stateless, so logout is handled
    client-side by discarding the token. This endpoint exists as a
    convention for logging the event and as a hook if token
    blacklisting is added in the future.
    """
    return None

