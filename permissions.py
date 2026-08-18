"""
Role hierarchy — the single definition of who may do what.

LOWER NUMBER = MORE AUTHORITY. Level 0 is the company admin, and each step up
is one rung further down the org chart:

    -1  super_admin    register companies, switch between them, everything below
     0  company_admin  full access within one company, assigns projects to people
     1  manager        projects, master data, field mappings, deletes, staff accounts
     2  staff          read + data entry (import, classify, finalize)

Note this is the reverse of DPL_project's numbering, which had 0=Staff and
2=Admin. The direction was chosen deliberately for this project; the rest of the
model — the create/edit split below — is DPL's.

Because the ordering is inverted, never write `level >= X`. Use has_authority(),
which reads the same regardless of direction.

Two matrices govern account management, because "can act on" is not the same as
"outranks":

    LEVEL_CAN_CREATE — a company_admin may mint another company_admin, so a
                       company is never left with nobody able to administer it.
    LEVEL_CAN_EDIT   — but may NOT edit or delete that peer. Same-rank accounts
                       cannot lock each other out.
"""

SUPER_ADMIN = -1
COMPANY_ADMIN = 0
MANAGER = 1
STAFF = 2

ROLE_LEVELS = {
    "super_admin": SUPER_ADMIN,
    "company_admin": COMPANY_ADMIN,
    "manager": MANAGER,
    "staff": STAFF,
}

LEVEL_ROLES = {level: role for role, level in ROLE_LEVELS.items()}

ROLE_LABELS = {
    "super_admin": "Super Admin",
    "company_admin": "Company Admin",
    "manager": "Manager",
    "staff": "Staff",
}

# Which levels each level may create.
LEVEL_CAN_CREATE = {
    STAFF: [],
    MANAGER: [STAFF],
    COMPANY_ADMIN: [STAFF, MANAGER, COMPANY_ADMIN],
    SUPER_ADMIN: [STAFF, MANAGER, COMPANY_ADMIN, SUPER_ADMIN],
}

# Which levels each level may edit, delete, or re-role. Note the peer exclusion.
LEVEL_CAN_EDIT = {
    STAFF: [],
    MANAGER: [STAFF],
    COMPANY_ADMIN: [STAFF, MANAGER],
    SUPER_ADMIN: [STAFF, MANAGER, COMPANY_ADMIN],
}


def has_authority(user_level: int, required_level: int) -> bool:
    """
    True if user_level ranks at required_level or above.

    The one place the numbering direction is encoded. Every caller asks this
    question instead of comparing numbers, so flipping the scale again would be
    a one-line change here.
    """
    return user_level <= required_level


def level_of(role: str) -> int:
    """Level for a role name. Unknown roles fall to the least authority."""
    return ROLE_LEVELS.get(role, STAFF)


def role_of(level: int) -> str:
    """Role name for a level."""
    return LEVEL_ROLES.get(level, "staff")


def label_of(role: str) -> str:
    """Human-readable role name, for error messages and the UI."""
    return ROLE_LABELS.get(role, role)


def can_create(actor_level: int, target_level: int) -> bool:
    return target_level in LEVEL_CAN_CREATE.get(actor_level, [])


def can_edit(actor_level: int, target_level: int) -> bool:
    return target_level in LEVEL_CAN_EDIT.get(actor_level, [])


def assignable_roles(actor_level: int) -> list[str]:
    """Role names this level may hand out, most senior first. Drives the UI dropdown."""
    return [role_of(lvl) for lvl in sorted(LEVEL_CAN_CREATE.get(actor_level, []))]
