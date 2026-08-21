"""
Master data routes for bank_master, beneficiary_master, head_master,
rera_head_master, and idw_head_master tables.

All tables share the same shape, so a single generic CRUD router handles
all five. The `master_type` path param selects the table.
"""
from fastapi import APIRouter, Depends, HTTPException, Query

import permissions
from database import company_connection
from routers.auth import get_current_schema, require_level

router = APIRouter(prefix="/master", tags=["master"])

# Anyone may read master data — it fills every dropdown in the app.
# Adding, renaming or deactivating an entry is manager+.
require_manager = require_level(permissions.MANAGER)

# The one description of every master table. 'label' and 'labels' are here
# rather than in the React page so adding a master type, or a column to one,
# is a single edit that the UI picks up through GET /master/_schema instead of
# a matching edit to a second copy of this table in the frontend.
#
# 'label_field' names the column that identifies a row to a human. It is what
# every dropdown in the app shows, and it is not always 'name' — bank_master
# calls it bank_name.
_TABLES = {
    'bank': {
        'label': 'Bank',
        'table': 'bank_master',
        'fields': ['bank_name', 'account_number', 'ifsc_code'],
        'labels': {'bank_name': 'Bank Name', 'account_number': 'Account Number', 'ifsc_code': 'IFSC Code'},
        'unique': ['bank_name', 'account_number'],
        'required': ['bank_name'],
        'columns': ['id', 'bank_name', 'account_number', 'ifsc_code', 'is_active', 'created_at', 'updated_at'],
        'order_by': 'bank_name',
        'label_field': 'bank_name',
    },
    # head1/2/3 are free text, not references to the three head tables they are
    # named after — see company/009_beneficiary_fields.sql. They record how this
    # payee is usually booked; the ledger's own classification still comes from
    # head_master / rera_head_master / idw_head_master and is unaffected.
    'beneficiary': {
        'label': 'Beneficiary',
        'table': 'beneficiary_master',
        'fields': ['name', 'account_number', 'ifsc_code', 'bank_name',
                   'head1', 'head2', 'head3'],
        'labels': {
            'name': 'Name',
            'account_number': 'Account Number',
            'ifsc_code': 'IFSC Code',
            'bank_name': 'Bank Name',
            'head1': 'Head 1',
            'head2': 'Head 2',
            'head3': 'Head 3',
        },
        # Nothing unique: the same account can legitimately be recorded twice,
        # and the name never was unique either.
        'unique': [],
        'required': ['name'],
        'columns': ['id', 'name', 'account_number', 'ifsc_code', 'bank_name',
                    'head1', 'head2', 'head3',
                    'is_active', 'created_at', 'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
    'head': {
        'label': 'Head',
        'table': 'head_master',
        'fields': ['name', 'category'],
        'labels': {'name': 'Name', 'category': 'Category'},
        'unique': ['name', 'category'],
        'required': ['name'],
        'columns': ['id', 'name', 'category', 'is_active', 'created_at', 'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
    'rera_head': {
        'label': 'RERA Head',
        'table': 'rera_head_master',
        'fields': ['name'],
        'labels': {'name': 'Name'},
        'unique': ['name'],
        'required': ['name'],
        'columns': ['id', 'name', 'is_active', 'created_at', 'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
    'idw_head': {
        'label': 'IDW Head',
        'table': 'idw_head_master',
        'fields': ['name'],
        'labels': {'name': 'Name'},
        'unique': ['name'],
        'required': ['name'],
        'columns': ['id', 'name', 'is_active', 'created_at', 'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
}


# Table name -> human label, for code outside this router that has to name a
# master table. The clone preview lists what a copy would bring across, and the
# words it uses are these ones rather than a second set written somewhere else —
# renaming 'RERA Head' above renames it there too.
TABLE_LABELS = {cfg['table']: cfg['label'] for cfg in _TABLES.values()}


def _get_config(master_type: str):
    cfg = _TABLES.get(master_type)
    if not cfg:
        raise HTTPException(404, f"Unknown master type: {master_type}")
    return cfg


# ── Schema ───────────────────────────────────────────────────────────
# Declared before /{master_type} on purpose: FastAPI matches in declaration
# order, so the other way round "_schema" would be read as a master type.

@router.get("/_schema")
async def master_schema():
    """
    What master tables exist, and what each one's columns are called.

    The Master Data page builds its tabs, its table headers and its add/edit
    form from this. Nothing about the five tables is written twice.
    """
    return [
        {
            "key": key,
            "label": cfg['label'],
            "label_field": cfg['label_field'],
            "fields": [
                {
                    "key": f,
                    "label": cfg['labels'].get(f, f.replace('_', ' ').title()),
                    "required": f in cfg['required'],
                }
                for f in cfg['fields']
            ],
        }
        for key, cfg in _TABLES.items()
    ]


# ── List ─────────────────────────────────────────────────────────────

@router.get("/{master_type}")
async def list_master(
    master_type: str,
    include_inactive: bool = Query(False),
    schema: str = Depends(get_current_schema),
):
    cfg = _get_config(master_type)
    where = "" if include_inactive else "WHERE is_active = true"
    # Sort by each table's own display column. This used to be a hardcoded
    # ORDER BY name, which 500s on bank_master — its display column is
    # bank_name — so the Bank tab, the one Master Data opens on, never loaded.
    async with company_connection(schema) as conn:
        rows = await conn.fetch(
            f"SELECT {', '.join(cfg['columns'])} FROM {cfg['table']} {where} "
            f"ORDER BY {cfg['order_by']}"
        )
    return [dict(r) for r in rows]


# ── Create ───────────────────────────────────────────────────────────

@router.post("/{master_type}", status_code=201, dependencies=[Depends(require_manager)])
async def create_master(
    master_type: str,
    body: dict,
    schema: str = Depends(get_current_schema),
):
    cfg = _get_config(master_type)
    fields = cfg['fields']

    for f in cfg['required']:
        if not str(body.get(f) or '').strip():
            raise HTTPException(400, f"{cfg['labels'].get(f, f)} is required.")

    placeholders = ', '.join(f'${i + 1}' for i in range(len(fields)))
    cols = ', '.join(fields)
    # "" is not a value — an untouched optional input posts an empty string, and
    # storing that makes a blank IFSC code sort and display differently from one
    # that was never entered.
    vals = [(str(body.get(f)).strip() or None) if body.get(f) is not None else None
            for f in fields]

    async with company_connection(schema) as conn:
        try:
            row = await conn.fetchrow(
                f"INSERT INTO {cfg['table']} ({cols}) VALUES ({placeholders}) RETURNING {', '.join(cfg['columns'])}",
                *vals,
            )
        except Exception as e:
            raise HTTPException(400, str(e))
    return dict(row)


# ── Update ───────────────────────────────────────────────────────────

@router.patch("/{master_type}/{item_id}", dependencies=[Depends(require_manager)])
async def update_master(
    master_type: str,
    item_id: int,
    body: dict,
    schema: str = Depends(get_current_schema),
):
    cfg = _get_config(master_type)
    sets = []
    params = []
    idx = 1
    # The column name is interpolated, the value is bound. Postgres has no
    # placeholder for an identifier, so the previous "SET $1 = $2" was a syntax
    # error on every call — editing any master row failed. Interpolating is safe
    # here only because `f` comes from cfg['fields'], a server-side list, never
    # from the request body.
    for f in cfg['required']:
        if f in body and not str(body[f] or '').strip():
            raise HTTPException(400, f"{cfg['labels'].get(f, f)} cannot be blank.")

    for f in cfg['fields']:
        if f in body:
            sets.append(f"{f} = ${idx}")
            # Same "" -> NULL rule as create, so clearing an optional field
            # leaves it empty rather than holding a zero-length string.
            params.append(str(body[f]).strip() or None if body[f] is not None else None)
            idx += 1
    if not sets:
        raise HTTPException(400, "No fields to update.")

    params.append(item_id)
    async with company_connection(schema) as conn:
        try:
            row = await conn.fetchrow(
                f"UPDATE {cfg['table']} SET {', '.join(sets)} WHERE id = ${idx} "
                f"RETURNING {', '.join(cfg['columns'])}",
                *params,
            )
        except Exception as e:
            # Most likely a UNIQUE violation on cfg['unique'].
            raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(404, "Item not found.")
    return dict(row)


# ── Delete (archive) ─────────────────────────────────────────────────

@router.delete("/{master_type}/{item_id}", dependencies=[Depends(require_manager)])
async def delete_master(
    master_type: str,
    item_id: int,
    permanent: bool = Query(False),
    schema: str = Depends(get_current_schema),
):
    cfg = _get_config(master_type)
    if not permanent:
        async with company_connection(schema) as conn:
            row = await conn.fetchrow(
                f"UPDATE {cfg['table']} SET is_active = false WHERE id = $1 AND is_active = true RETURNING id",
                item_id,
            )
        if row is None:
            raise HTTPException(400, "Already archived or not found.")
        return {"status": "archived"}

    async with company_connection(schema) as conn:
        await conn.execute(f"DELETE FROM {cfg['table']} WHERE id = $1", item_id)
    return {"status": "deleted"}
