"""
Master data routes for bank_master, beneficiary_master, head_master,
rera_head_master, and idw_head_master tables.

All tables share the same shape, so a single generic CRUD router handles
all five. The `master_type` path param selects the table.
"""
import logging
import re

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)

import permissions
from database import company_connection
from routers.auth import get_current_schema, require_level
from services import beneficiary_import, tabular_import

logger = logging.getLogger(__name__)

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
        'fields': ['bank_name', 'account_number', 'ifsc_code', 'account_type',
                   'company'],
        'labels': {'bank_name': 'Bank Name', 'account_number': 'Account Number',
                   'ifsc_code': 'IFSC Code', 'account_type': 'Type',
                   'company': 'Company'},
        # Both chosen from this company's own master tables, not typed. See
        # company/015_bank_account_type.sql and 016_bank_company.sql on why the
        # name is stored rather than a reference to it.
        'options_from': {'account_type': 'account_type', 'company': 'company'},
        'unique': ['bank_name', 'account_number'],
        'required': ['bank_name'],
        'columns': ['id', 'bank_name', 'account_number', 'ifsc_code', 'account_type',
                    'company', 'is_active', 'created_at', 'updated_at'],
        'order_by': 'bank_name',
        'label_field': 'bank_name',
    },
    # Nine head columns, three from each head master — see
    # company/013_beneficiary_head_columns.sql. They record how this payee is
    # usually booked and are chosen from the company's own master tables rather
    # than typed. The ledger's own classification is still head_id /
    # rera_head_id / idw_head_id on transactions and is unaffected by these.
    #
    # 'idw_head*' columns carry 'TCP Head' labels for the same reason the
    # 'idw_head' master type does: the rename was to the label only.
    'beneficiary': {
        'label': 'Beneficiary',
        'table': 'beneficiary_master',
        # Turns on the Import button for this tab. A flag rather than the page
        # naming 'beneficiary' itself, so a second importable master type is a
        # line here and an endpoint, not an edit to the React page.
        'importable': True,
        # 'company' sits with the identifying fields rather than among the
        # heads: it says who this payee belongs to, not how they are booked.
        'fields': ['name', 'account_number', 'ifsc_code', 'bank_name', 'company',
                   'rera_head1', 'rera_head2', 'rera_head3',
                   'idw_head1', 'idw_head2', 'idw_head3',
                   'head1', 'head2', 'head3'],
        'labels': {
            'name': 'Name',
            'account_number': 'Account Number',
            'ifsc_code': 'IFSC Code',
            'bank_name': 'Bank Name',
            'company': 'Company',
            'rera_head1': 'RERA Head 1',
            'rera_head2': 'RERA Head 2',
            'rera_head3': 'RERA Head 3',
            'idw_head1': 'TCP Head 1',
            'idw_head2': 'TCP Head 2',
            'idw_head3': 'TCP Head 3',
            'head1': 'Internal Head 1',
            'head2': 'Internal Head 2',
            'head3': 'Internal Head 3',
        },
        # Which master type fills each dropdown. The Master Data page reads this
        # from GET /master/_schema and fetches that type's rows, so the options
        # are always this company's own heads — never a list written twice.
        'options_from': {
            'company': 'company',
            'rera_head1': 'rera_head', 'rera_head2': 'rera_head', 'rera_head3': 'rera_head',
            'idw_head1': 'idw_head', 'idw_head2': 'idw_head', 'idw_head3': 'idw_head',
            'head1': 'head', 'head2': 'head', 'head3': 'head',
        },
        # Which column of the source master supplies the stored VALUE, when it
        # is not that master's label_field. Company stores 'DPL' rather than
        # 'DWARKADHIS PROJECTS PRIVATE LIMITED': the full names are long enough
        # to swallow the row in a table showing fourteen columns, and the
        # abbreviation is what these are called in the books anyway.
        #
        # The dropdown still shows both, so picking one does not require
        # remembering which three letters belong to which company.
        'options_value': {'company': 'abbreviation'},
        # The three heads within a group must differ. Enforced in the database
        # too (013's CHECK constraints); repeated here so the message names the
        # field instead of quoting a constraint. Groups are separate because the
        # same name legitimately exists in two different head tables.
        'distinct_groups': [
            ('rera_head1', 'rera_head2', 'rera_head3'),
            ('idw_head1', 'idw_head2', 'idw_head3'),
            ('head1', 'head2', 'head3'),
        ],
        # Nothing unique: the same account can legitimately be recorded twice,
        # and the name never was unique either.
        'unique': [],
        'required': ['name'],
        'columns': ['id', 'name', 'account_number', 'ifsc_code', 'bank_name',
                    'company',
                    'rera_head1', 'rera_head2', 'rera_head3',
                    'idw_head1', 'idw_head2', 'idw_head3',
                    'head1', 'head2', 'head3',
                    'is_active', 'created_at', 'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
    # Same treatment as 'idw_head' below: the label is the only thing that
    # changed. The key stays 'head' and the table stays head_master, so every
    # URL, fieldmap row and head_id column already recorded keeps working.
    'head': {
        'label': 'Internal Head',
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
    # 'idw_head' / idw_head_master keep their original names. Only the label
    # changed, and a rename of the key would have to reach the master_type in
    # every URL, the column on two tables, and the fieldmap rows already
    # pointing at them — a lot of moving parts to change a word on screen.
    'idw_head': {
        'label': 'TCP Head',
        'table': 'idw_head_master',
        'fields': ['name'],
        'labels': {'name': 'Name'},
        'unique': ['name'],
        'required': ['name'],
        'columns': ['id', 'name', 'is_active', 'created_at', 'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
    # Group and associate companies referred to inside THIS company's books —
    # not admin.companies, which is the app's tenant registry. See
    # company/014_company_and_account_type_masters.sql on why the two are
    # separate despite overlapping in content.
    #
    # Appended rather than slotted in next to Bank because the Master Data page
    # opens on whichever type is listed first, and that is Bank today.
    'company': {
        'label': 'Company',
        'table': 'company_master',
        'fields': ['name', 'abbreviation'],
        'labels': {'name': 'Name', 'abbreviation': 'Abbreviation'},
        # Exactly three capital letters, matching the CHECK added in 017. The
        # rule lives in both places on purpose: the database is what makes it
        # true, this is what makes the refusal readable.
        #
        # 'upper' is why lowercase is not an error — "acp" is stored as "ACP"
        # rather than bounced, because nobody typing an abbreviation in lower
        # case meant a different value.
        'formats': {
            'abbreviation': {
                'regex': r'^[A-Z]{3}$',
                'message': 'Abbreviation must be exactly three letters, like ACP.',
                'upper': True,
                'maxlength': 3,
                'placeholder': 'ABC',
            },
        },
        # Both unique in the database. Only the name is required: an
        # abbreviation nobody has decided on yet should not block the row.
        'unique': ['name', 'abbreviation'],
        'required': ['name'],
        'columns': ['id', 'name', 'abbreviation', 'is_active', 'created_at',
                    'updated_at'],
        'order_by': 'name',
        'label_field': 'name',
    },
    'account_type': {
        'label': 'Type of Account',
        'table': 'account_type_master',
        'fields': ['name'],
        'labels': {'name': 'Name'},
        # Stored in capitals however it is typed. No regex: there is nothing to
        # refuse here, only a shape to impose — 'current' and 'Current' mean the
        # same account type and should not become two rows that look different
        # in the Bank tab's dropdown.
        'formats': {'name': {'upper': True}},
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


def _normalise(cfg: dict, field: str, raw) -> str | None:
    """Trim, upper-case where the field asks for it, and turn "" into NULL.

    The ""-to-NULL rule is why an optional UNIQUE column works at all: Postgres
    permits any number of NULLs but exactly one empty string, so two rows saved
    from a form with that box untouched would otherwise collide with each other.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if cfg.get('formats', {}).get(field, {}).get('upper'):
        value = value.upper()
    return value or None


def _check_formats(cfg: dict, values: dict) -> None:
    """Reject a value that does not match its field's shape.

    Only fields actually present are looked at, so a PATCH touching one column
    is not asked to justify the others. Blank is not checked — a field that may
    be empty is settled by cfg['required'], not here.

    'regex' is optional. A field can ask only to be upper-cased, which _normalise
    has already done by the time this runs and which nothing can then fail.
    """
    for field, rule in cfg.get('formats', {}).items():
        if field not in values or 'regex' not in rule:
            continue
        value = values[field]
        if value and not re.match(rule['regex'], value):
            raise HTTPException(400, rule['message'])


def _check_distinct_groups(cfg: dict, values: dict) -> None:
    """Reject a group of fields that are meant to differ but do not.

    `values` must describe the row as it will be AFTER the write, not merely
    what the request carried: a PATCH that sets only RERA Head 2 can still
    collide with the RERA Head 1 already stored, and checking the body alone
    would let that through to the database's CHECK constraint — which is a
    correct refusal wearing an unreadable message.

    Blank and missing are the same thing here and never collide, matching the
    ""-becomes-NULL rule the writes below apply.
    """
    for group in cfg.get('distinct_groups', []):
        seen: dict[str, str] = {}
        for f in group:
            value = str(values.get(f) or '').strip()
            if not value:
                continue
            if value in seen:
                first = cfg['labels'].get(seen[value], seen[value])
                this = cfg['labels'].get(f, f)
                raise HTTPException(
                    400,
                    f'{first} and {this} are both "{value}". '
                    f'The three must be different.'
                )
            seen[value] = f


# ── Schema ───────────────────────────────────────────────────────────
# Declared before /{master_type} on purpose: FastAPI matches in declaration
# order, so the other way round "_schema" would be read as a master type.

@router.delete("/beneficiary/all", dependencies=[Depends(require_manager)])
async def delete_all_beneficiaries(schema: str = Depends(get_current_schema)):
    """Empty the beneficiary table.

    Exists for the same reason the import does: a list loaded from a sheet is
    corrected by loading a better sheet, and doing that means clearing the last
    one. Deleting 150 rows one at a time is not a workflow.

    A real DELETE, not the archive that the single-row button does. Archiving
    would leave every account number in place, and the importer matches on
    account number -- so the next upload would call all of them duplicates and
    the corrected sheet would not land.

    One exception it cannot make: transactions.beneficiary_id is ON DELETE
    RESTRICT, so a beneficiary the ledger has booked against cannot be removed
    without rewriting history. Those are archived instead and counted
    separately, so the caller is told rather than the whole operation failing
    because of one row.

    temp_trans.beneficiary_id is ON DELETE SET NULL, so staged rows survive and
    simply lose the link. They are counted too -- someone will have to pick the
    beneficiary again on those rows, and that is worth knowing before pressing
    the button rather than after.
    """
    async with company_connection(schema) as conn:
        async with conn.transaction():
            total = await conn.fetchval("SELECT count(*) FROM beneficiary_master")

            protected = [
                r["beneficiary_id"] for r in await conn.fetch(
                    "SELECT DISTINCT beneficiary_id FROM transactions "
                    "WHERE beneficiary_id IS NOT NULL")
            ]
            unlinked = await conn.fetchval(
                "SELECT count(*) FROM temp_trans WHERE beneficiary_id IS NOT NULL "
                "AND NOT (beneficiary_id = ANY($1::bigint[]))", protected)

            archived = 0
            if protected:
                archived = await conn.fetchval(
                    "WITH archived AS ("
                    "  UPDATE beneficiary_master SET is_active = false "
                    "  WHERE id = ANY($1::bigint[]) AND is_active = true RETURNING 1"
                    ") SELECT count(*) FROM archived", protected)

            # ANY of an empty array is false for every row, so with nothing
            # protected this is a plain "delete everything".
            tag = await conn.execute(
                "DELETE FROM beneficiary_master "
                "WHERE NOT (id = ANY($1::bigint[]))", protected)
            deleted = int(tag.split()[-1])

    logger.info("[master] beneficiary clear-all: deleted=%d archived=%d unlinked=%d",
                deleted, archived, unlinked)
    return {
        "total": total,
        "deleted": deleted,
        "archived": archived,
        "unlinked_staged_rows": unlinked,
    }


@router.post("/beneficiary/import", dependencies=[Depends(require_manager)])
async def import_beneficiaries(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true writes"),
    on_duplicate: str = Form(
        "skip", description="skip | overwrite — rows whose account number AND "
                            "company already exist"),
    on_cross_company: str = Form(
        "add", description="add | skip — rows whose account number exists under "
                           "a different company. A payee may legitimately be "
                           "recorded once per group company, so the default "
                           "adds them"),
    schema: str = Depends(get_current_schema),
):
    """Bulk-load beneficiaries from an Excel or CSV sheet.

    Two calls, same as the statement importers. save=false reports what would
    happen and writes nothing; save=true performs it. The preview is not
    politeness — the duplicate choice cannot be made before knowing how many
    rows it covers, and a sheet with a mis-named head should be corrected in the
    sheet rather than half-imported.

    The sheet is re-read on the second call rather than the first call's result
    being held server-side. Holding it would mean a session, an expiry and a
    memory bound; re-reading costs a second and cannot go stale.
    """
    # .xls is deliberately absent: openpyxl reads .xlsx and .xlsm only, so
    # accepting it here would take the upload and then fail in the reader with a
    # vaguer message than "we do not read that format".
    name = (file.filename or "").lower()
    if name.endswith(".pdf"):
        reader = beneficiary_import.read_pdf_grid
    elif name.endswith(".xlsx"):
        reader = tabular_import.READERS["excel"][0]
    elif name.endswith(".csv"):
        reader = tabular_import.READERS["csv"][0]
    else:
        raise HTTPException(
            400, "Upload an .xlsx, .csv or .pdf file. An older .xls has to be "
                 "saved as .xlsx first.")

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "That file is empty.")

    try:
        grid = reader(file_bytes)
    except RuntimeError as e:
        # The PDF reader's own refusals — encrypted, or nothing table-shaped in
        # it — already say what to do about them.
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"That file could not be read: {e}")

    async with company_connection(schema) as conn:
        try:
            analysis = await beneficiary_import.analyse(conn, grid)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

        if not save:
            return {k: v for k, v in analysis.items() if not k.startswith("_")}

        try:
            result = await beneficiary_import.commit(
                conn, analysis, on_duplicate, on_cross_company)
        except RuntimeError as e:
            raise HTTPException(400, str(e))

    return {
        **{k: v for k, v in analysis.items() if not k.startswith("_")},
        "saved": True,
        **result,
    }


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
                    # Present only on fields that are chosen rather than typed.
                    # The value is another master type's key, so the page loads
                    # that type through the endpoint it already uses for its own
                    # tab — no second route and no options list stored here.
                    **(
                        {"options_from": cfg['options_from'][f]}
                        if f in cfg.get('options_from', {}) else {}
                    ),
                    # Which column of that master is stored. Absent means the
                    # master's own label_field, which is the usual case.
                    **(
                        {"options_value": cfg['options_value'][f]}
                        if f in cfg.get('options_value', {}) else {}
                    ),
                    # Shape hints for a typed field. The API and the database
                    # both refuse a bad value regardless; these exist so the box
                    # stops accepting one before the user presses Create.
                    **{
                        k: v
                        for k, v in cfg.get('formats', {}).get(f, {}).items()
                        if k in ('maxlength', 'placeholder', 'upper')
                    },
                }
                for f in cfg['fields']
            ],
            # Field groups whose members must differ from one another. The page
            # greys out a head already picked in its group rather than letting
            # it be chosen and refused on save.
            "distinct_groups": [list(g) for g in cfg.get('distinct_groups', [])],
            "importable": bool(cfg.get('importable')),
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

    # Normalised once, up front, so the checks below and the values written are
    # the same strings — an abbreviation is validated as "ACP", not as the "acp"
    # that was typed and would then have been stored in a third form again.
    #
    # "" is not a value: an untouched optional input posts an empty string, and
    # storing that makes a blank IFSC code sort and display differently from one
    # that was never entered.
    clean = {f: _normalise(cfg, f, body.get(f)) for f in fields}

    for f in cfg['required']:
        if not clean.get(f):
            raise HTTPException(400, f"{cfg['labels'].get(f, f)} is required.")

    _check_formats(cfg, clean)
    # A create carries the whole row, so this is already the after state.
    _check_distinct_groups(cfg, clean)

    placeholders = ', '.join(f'${i + 1}' for i in range(len(fields)))
    cols = ', '.join(fields)
    vals = [clean[f] for f in fields]

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
    # Same normalisation as create, over only the fields this patch carries.
    clean = {f: _normalise(cfg, f, body[f]) for f in cfg['fields'] if f in body}

    for f in cfg['required']:
        if f in body and not clean.get(f):
            raise HTTPException(400, f"{cfg['labels'].get(f, f)} cannot be blank.")

    _check_formats(cfg, clean)

    for f in cfg['fields']:
        if f in body:
            sets.append(f"{f} = ${idx}")
            # "" -> NULL, so clearing an optional field leaves it empty rather
            # than holding a zero-length string.
            params.append(clean[f])
            idx += 1
    if not sets:
        raise HTTPException(400, "No fields to update.")

    params.append(item_id)
    async with company_connection(schema) as conn:
        if cfg.get('distinct_groups'):
            # Read the row first and overlay the patch, so the check sees what
            # the row is about to become rather than the handful of fields this
            # particular request happened to send.
            current = await conn.fetchrow(
                f"SELECT {', '.join(cfg['fields'])} FROM {cfg['table']} WHERE id = $1",
                item_id,
            )
            if current is None:
                raise HTTPException(404, "Item not found.")
            _check_distinct_groups(cfg, {**dict(current), **clean})

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
