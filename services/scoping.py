"""
Project scoping — which projects a request is allowed to see.

Managers and staff see only the projects they have been assigned. Company
admins and super admins see everything in the company. That single rule is
resolved here once and reused by projects, transactions, staging and export, so
the four cannot disagree about what a person can see.

The unassigned case matters twice and is easy to get backwards:

  * a person with no assignments sees NOTHING, not everything. Access is
    granted, never assumed.
  * a ROW with no project (project_id IS NULL) is visible to everyone in the
    company. Staged rows arrive with no project — that is what classifying
    assigns — so hiding them would leave nobody able to file a fresh import,
    and any row that reached the ledger unclassified would be invisible forever.
"""
from fastapi import HTTPException, status

import permissions

# Returned by visible_project_ids to mean "no restriction at all".
UNRESTRICTED = None


async def visible_project_ids(conn, user: dict) -> list[int] | None:
    """
    Project ids this user may see, or UNRESTRICTED (None) for admins.

    An empty list is meaningful and different from None: it means the person is
    scoped and has been assigned nothing, so they see no project rows at all.

    `conn` must be a company_connection — project_members is read unqualified
    and resolves through search_path to the caller's own company.
    """
    if permissions.has_authority(user["level"], permissions.COMPANY_ADMIN):
        return UNRESTRICTED

    if user.get("id") is None:
        # Pre-upgrade tokens carry no uid, so there is no way to look up
        # assignments. Failing closed here would look like "all your data
        # vanished"; say what actually happened instead.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Your session predates the project-access update. Please sign in again.",
        )

    rows = await conn.fetch(
        "SELECT project_id FROM project_members WHERE user_id = $1", user["id"]
    )
    return [r["project_id"] for r in rows]


def project_filter(scope: list[int] | None, column: str, next_index: int,
                   include_unassigned: bool = True) -> tuple[str | None, list, int]:
    """
    Build the SQL fragment restricting `column` to the visible projects.

    Returns (clause, params, next_index). clause is None when nothing needs to
    be added — either the caller is unrestricted, or they are scoped to nothing
    and the caller should short-circuit before querying (see scope_is_empty).

        clause, params, idx = project_filter(scope, "t.project_id", idx)
        if clause:
            filters.append(clause)
            params.extend(params)

    include_unassigned=False drops the "OR ... IS NULL" arm, for callers that
    only ever look at rows already tied to a project.
    """
    if scope is UNRESTRICTED or not scope:
        return None, [], next_index

    clause = f"{column} = ANY(${next_index})"
    if include_unassigned:
        clause = f"({clause} OR {column} IS NULL)"
    return clause, [scope], next_index + 1


def scope_is_empty(scope: list[int] | None) -> bool:
    """True when the user is scoped and has no projects — they see project rows of nobody."""
    return scope is not UNRESTRICTED and not scope


def can_use_project(scope: list[int] | None, project_id: int | None) -> bool:
    """
    True if this user may file something against project_id.

    None passes: leaving a row unfiled is allowed for anyone, and is how staged
    rows arrive in the first place.
    """
    if scope is UNRESTRICTED or project_id is None:
        return True
    return project_id in scope
