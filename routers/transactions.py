"""
Transaction routes.

GET    /transactions                  — list finalized transactions (paged)
GET    /transactions/summary          — totals by head for a date range
GET    /temp-trans                    — list raw staged rows (paged)
DELETE /temp-trans                    — clear the whole staging table
DELETE /temp-trans/{row_id}           — remove one staged row
POST   /temp-trans/{row_id}/classify  — tag a raw row with a head
POST   /temp-trans/{row_id}/finalize  — move a row into the ledger

Both list endpoints are paged and searchable, and both return
{columns, rows, total, page, limit}. They used to return every row in the table
on every render, which was fine at a few hundred and is not at a few hundred
thousand — one statement import is several hundred rows on its own.
"""
import logging
import re

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

import permissions
from database import company_connection
from routers.auth import get_company_user, get_current_schema, require_level
from services import custom_fields, rules, scoping, staging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/transactions", tags=["transactions"])

# Clearing staging throws away everyone's un-posted work at once, so it is
# manager and above — the same bar as discarding a single batch.
require_manager = require_level(permissions.MANAGER)

# Which master table backs each classification id. Nothing in this file names a
# head, a beneficiary or a project literally — a row is only classifiable
# against rows that exist in this company's own master tables right now, and
# every company keeps its own copies in its own schema.
_MASTER_LOOKUPS = {
    "head_id": ("head_master", "head"),
    "rera_head_id": ("rera_head_master", "RERA head"),
    # The label is what an error message calls it; the table and the column keep
    # their original names.
    "idw_head_id": ("idw_head_master", "TCP head"),
    "beneficiary_id": ("beneficiary_master", "beneficiary"),
    "project_id": ("projects", "project"),
}

# Which master table backs each value of fieldmap.mirrors — see
# company/019_fieldmap_mirrors.sql and 025_fieldmap_mirrors_project.sql. Keyed
# by the mirrors value, not the id column, because that is what the fieldmap row
# stores.
#
# All five Classify pickers are here. Which of them a company actually mirrors
# is the fieldmap's answer: company_028 mirrors four (BUSINESS UNIT is its
# Project column), companies with no custom fields mirror none, and nothing is
# written for a classification no column claims.
_MIRROR_TABLES = {
    "head": "head_master",
    "rera_head": "rera_head_master",
    "idw_head": "idw_head_master",
    "project": "projects",
    "beneficiary": "beneficiary_master",
}

# A fieldmap row names a physical column, and that name is interpolated into the
# UPDATE below — Postgres has no placeholder for an identifier. The fieldmap is
# server-side data rather than request input, but "not user input today" is a
# weaker guarantee than a pattern that cannot express anything but a custom
# field, so the name is matched against one before it is used.
_CUSTOM_FIELD_RE = re.compile(r"^field_(text|num|date)_\d+$")


async def _mirror_values(conn, chosen: dict[str, int | None]) -> dict[str, str]:
    """Map custom column -> master name, for classifications being set.

    Returns {} when the company has no mirroring columns, which is the normal
    case for a company that never added custom fields — the caller then writes
    only the _id columns, exactly as before this existed.
    """
    wanted = {key: value for key, value in chosen.items() if value is not None}
    if not wanted:
        return {}

    rows = await conn.fetch(
        "SELECT fieldname, mirrors FROM fieldmap "
        "WHERE mirrors = ANY($1::text[]) AND is_active = true",
        list(wanted),
    )

    out: dict[str, str] = {}
    for row in rows:
        column, target = row["fieldname"], row["mirrors"]
        if not _CUSTOM_FIELD_RE.match(column or ""):
            logger.warning(
                "[classify] fieldmap row mirrors %s but names %r, which is not a "
                "custom field column — ignored", target, column,
            )
            continue
        name = await conn.fetchval(
            f"SELECT name FROM {_MIRROR_TABLES[target]} WHERE id = $1", wanted[target]
        )
        if name is not None:
            out[column] = name
    return out


# Page size ceiling. Export is the route for "give me everything" — it streams
# instead of building one JSON array in memory, which is the actual reason a
# list endpoint should not be asked for 200,000 rows.
MAX_PAGE_SIZE = 500


# Enough words for any real query, and a bound on how much work one search box
# can ask a sequential scan to do.
MAX_SEARCH_TERMS = 8


def _search_terms(term: str) -> list[str]:
    """Split what was typed into the words every row has to match.

    A search box people type two words into is expected to find rows carrying
    both, wherever each one sits — "salary 5000" means the salary row for 5000,
    not a narration containing the literal string "salary 5000". So whitespace
    separates words and they are AND-ed.

    That would take phrase search away, so a quoted run is kept whole:
    "cash deposit" stays one term and matches only where those two words appear
    together.
    """
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    for ch in term:
        if quote:
            if ch == quote:
                quote = ""
            else:
                buf.append(ch)
        elif ch in "\"'":
            quote = ch
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out[:MAX_SEARCH_TERMS]


def _like_patterns(token: str) -> list[str]:
    """The ILIKE patterns one term should be tried against.

    Wildcards are escaped: a term containing % or _ searches for that character
    instead of silently matching everything, which is what "50%" used to do.

    A term with digit grouping gets a second pattern with the grouping removed.
    The table prints 1,50,000.00 and the column holds 150000.00, so a number
    copied off the screen finds nothing otherwise — the one place where what is
    searched is not what is displayed. Only added when the term actually has
    separators, so an ordinary word still costs one comparison.
    """
    esc = token.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    patterns = [f"%{esc}%"]
    bare = esc.replace(",", "")
    if bare and bare != esc:
        patterns.append(f"%{bare}%")
    return patterns


def highlight_terms(term: str) -> list[str]:
    """The strings the browser should mark up, in the same order it should try.

    Returned to the client rather than re-derived there: what matched is decided
    here, and a second implementation in JavaScript would drift from it the
    first time either side changed.
    """
    out: list[str] = []
    for token in _search_terms(term):
        out.append(token)
        bare = token.replace(",", "")
        if bare and bare != token:
            out.append(bare)
    return out


def _search_filter(term: str, columns: list[dict], extra_exprs: tuple[str, ...],
                   idx: int) -> tuple[str, list, int]:
    """A WHERE fragment matching *term* against everything visible on the row.

    Every data column plus the joined master names, so what the search matches
    is what the table draws — searching for "SALARY" or a UTR finds the row
    whether that text sits in the narration or in the beneficiary it was filed
    against. It is matched against the whole table, not the page on screen: the
    browser holds fifty rows and the answer is usually not among them.

    Non-text columns are cast rather than skipped, which is what makes a date
    findable as "2026-08" and an amount as "1500". DPL restricted its search to
    the id column to avoid "345" matching a narration ending in 345; that was
    the right call for a lookup-by-id box and the wrong one here, where the
    question is "where did this money go", not "show me row 345".

    The columns are concatenated once per row and each term tested against that
    one string, rather than each term against each column. Same rows come back,
    a dozen fewer comparisons per row per term, and the SQL stays readable as
    the column set grows.

    Terms are bind parameters, never interpolated. Column names come from
    data_columns(), which reads the catalog — they are never user input.

    A leading-wildcard ILIKE cannot use an index; this is a sequential scan by
    construction. Fine at statement scale, and the reason limit is capped.
    """
    targets = [f"t.{c['name']}::text" for c in columns] + list(extra_exprs)
    tokens = _search_terms(term)
    if not targets or not tokens:
        return "", [], idx

    row_text = "concat_ws(' ', " + ", ".join(targets) + ")"
    clauses: list[str] = []
    params: list = []
    for token in tokens:
        ors = []
        for pattern in _like_patterns(token):
            ors.append(f"{row_text} ILIKE ${idx}")
            params.append(pattern)
            idx += 1
        clauses.append("(" + " OR ".join(ors) + ")")
    return "(" + " AND ".join(clauses) + ")", params, idx


# ---------- Sorting ----------------------------------------------------------

# Columns whose values sort by byte order unless they are folded first. Without
# this 'AXIS' and 'axis' land in different halves of the alphabet and every
# capitalised entry sorts above every lowercase one, which reads as the sort
# being broken rather than as ASCII order.
_TEXT_TYPES = frozenset({"text", "character varying", "character", "name"})

# Sortable things that are not data columns: the row's own position, its
# workflow state, and the master names the joins resolve. Sorting by Head means
# sorting by the head's name, not by head_id -- an id sorts by creation order,
# which is not an order anybody asked for.
_TEMP_EXTRA_SORTS = {
    "row_number":       ("t.row_number", "integer"),
    "batch_id":         ("t.batch_id", "integer"),
    "is_classified":    ("t.is_classified", "boolean"),
    "created_at":       ("t.created_at", "timestamp"),
    "project_name":     ("p.name", "text"),
    "project_code":     ("p.code", "text"),
    "head_name":        ("h.name", "text"),
    "rera_head_name":   ("rh.name", "text"),
    "idw_head_name":    ("ih.name", "text"),
    "beneficiary_name": ("bn.name", "text"),
}

_LEDGER_EXTRA_SORTS = {
    "id":           ("t.id", "integer"),
    "created_at":   ("t.created_at", "timestamp"),
    "project_name": ("p.name", "text"),
    "project_code": ("p.code", "text"),
    "head_name":    ("h.name", "text"),
    "bank_name":    ("b.bank_name", "text"),
}


def _sort_clause(sort: str | None, direction: str, columns: list[dict],
                 extra: dict[str, tuple[str, str]],
                 default: str) -> tuple[str, str | None, str]:
    """ORDER BY for the requested column, plus the sort actually applied.

    A whitelist, because ORDER BY takes no bind parameter -- the only thing safe
    to put in the string is a name this server produced. The keys come from
    data_columns(), which reads the catalog, so a custom field is sortable the
    moment it exists and nothing here needs updating.

    An unrecognised name falls back to the default order rather than 400-ing. A
    bookmark naming a field that has since been deleted should still list rows;
    the response says which sort was applied, so the screen can correct itself.

    NULLS LAST in both directions. A blank cell is not the smallest value, it is
    an unknown one, and someone sorting by amount to find the largest wants the
    largest first, not forty blanks.

    The default order is kept as the tiebreak, and that is what makes paging
    stable. Sorting on a column full of duplicates otherwise leaves the rest of
    the order to the plan, which can differ between the page-1 and page-2
    queries -- one row shown twice and another never shown at all.
    """
    known: dict[str, tuple[str, str]] = {
        c["name"]: (f"t.{c['name']}", c.get("type") or "") for c in columns
    }
    known.update(extra)

    if sort not in known:
        return default, None, "asc"

    expr, coltype = known[sort]
    if coltype.lower() in _TEXT_TYPES:
        expr = f"lower({expr})"
    way = "desc" if (direction or "").strip().lower() == "desc" else "asc"
    return f"{expr} {way.upper()} NULLS LAST, {default}", sort, way


# ---------- Filters (Date / Account Number / Company) ------------------------

# What the Account and Company dropdowns send for "rows with nothing in this
# column". A distinct-values list has to be able to offer that -- the rows whose
# account matched no bank are exactly the ones worth looking at -- and an empty
# string cannot carry it, because an empty query param means "no filter".
BLANK = "__none__"

_DATE_TYPES = frozenset({
    "date", "timestamp without time zone", "timestamp with time zone",
})


def _as_date(value: str, label: str):
    """Parse a YYYY-MM-DD filter bound, or 400.

    Parsed here rather than handed to Postgres as text. A malformed date reaching
    the database is an unhandled DataError and a 500; parsed here it is a
    sentence naming which of the two calendars is wrong.

    The value goes on as a real date object, so the comparison is date-to-date
    and never string-to-string -- '9' sorting after '10' is exactly the bug that
    kind of comparison produces.
    """
    from datetime import date as _date
    try:
        return _date.fromisoformat((value or "").strip())
    except ValueError:
        raise HTTPException(
            400, f"{label} must be a date in YYYY-MM-DD form, not {value!r}."
        )


def _date_expr(date_col: str, columns: list[dict]) -> str:
    """t.<date column>, cast only if the column is not already a date type.

    Nearly every company maps its date field to a DATE column and the cast is
    not needed. A company that mapped it to text still gets a working range
    filter instead of an operator-does-not-exist error.
    """
    coltype = next(
        (c.get("type") or "" for c in columns if c["name"] == date_col), ""
    )
    return f"t.{date_col}" if coltype.lower() in _DATE_TYPES else f"t.{date_col}::date"


async def _facet_filters(conn, *, columns: list[dict], filters: list[str],
                         params: list, idx: int, date_from: str | None,
                         date_to: str | None, account: str | None,
                         company: str | None) -> int:
    """Append the Date / Account / Company clauses. Returns the next $n.

    Which physical column each of the three means is resolved from the fieldmap,
    never hardcoded: the account number is field_text_17 in one company and can
    be anything in another, and company_001 has no Company column at all. Asking
    for a filter the company has no column for is a 400 that says so, rather
    than a filter that quietly matches every row.

    The account match is on digits only, the same rule the Company fill uses --
    '1200 2464 2195' and '120024642195' are one account, and a filter that
    disagreed with the fill would show rows filled with a company it claims are
    a different account.
    """
    if date_from or date_to:
        date_col = await custom_fields.date_column(conn)
        if not date_col:
            raise HTTPException(
                400, "Cannot filter by date: this company has no date field "
                     "mapped. Add one on the Field Mapping page."
            )
        expr = _date_expr(date_col, columns)
        if date_from:
            filters.append(f"{expr} >= ${idx}")
            params.append(_as_date(date_from, "The From date"))
            idx += 1
        if date_to:
            filters.append(f"{expr} <= ${idx}")
            params.append(_as_date(date_to, "The To date"))
            idx += 1

    if account:
        col = await staging.account_column(conn)
        if not col:
            raise HTTPException(
                400, "Cannot filter by account number: this company has no "
                     "account number field mapped."
            )

        # Comma separated, so several accounts can be asked for at once. A
        # comma cannot occur inside an account number — the values are reduced
        # to digits before anything is compared — so it needs no escaping, and
        # the query string stays readable.
        wanted = [v.strip() for v in account.split(",") if v.strip()]
        include_blank = BLANK in wanted
        # Normalised here rather than in SQL: one array parameter and one
        # comparison, instead of a regexp per value per row. The rule is the
        # one the Company fill uses, so a filter can never disagree with what
        # filled the Company column.
        numbers = sorted({staging.normalise_account(v)
                          for v in wanted if v != BLANK})
        numbers = [n for n in numbers if n]

        clauses: list[str] = []
        if numbers:
            clauses.append(
                f"{staging.account_digits(f't.{col}')} = ANY(${idx}::text[])")
            params.append(numbers)
            idx += 1
        if include_blank:
            clauses.append(f"(t.{col} IS NULL OR btrim(t.{col}) = '')")

        # Asked for accounts, none of which can name one — every value was
        # blank or had no digits in it. Nothing matches, which is the honest
        # answer; dropping the filter would quietly show the whole table.
        filters.append("(" + " OR ".join(clauses) + ")" if clauses else "1 = 0")

    if company:
        col = await staging.company_column(conn)
        if not col:
            raise HTTPException(
                400, "Cannot filter by company: this company has no Company "
                     "field mapped. Add one on the Custom Fields page."
            )
        if company == BLANK:
            filters.append(f"(t.{col} IS NULL OR btrim(t.{col}) = '')")
        else:
            # Case-insensitive: the values are abbreviations written by hand in
            # Master Data, and 'dpl' should not be a different company from 'DPL'.
            filters.append(f"lower(btrim(t.{col})) = lower(btrim(${idx}::text))")
            params.append(company)
            idx += 1

    return idx


async def _distinct_values(conn, table: str, column: str, where: str,
                           params: list) -> dict:
    """{values: [{value, count}], blank: n} for one filter dropdown.

    Counts come back with the values so the dropdown can say "DPL (128)". They
    are what turns a list of account numbers into something you can act on --
    a number with 3 rows against a number with 400 is usually a parse artefact.

    Blanks are counted separately rather than listed as a value, because they
    are one option in the dropdown regardless of how many distinct kinds of
    empty (NULL, '', '  ') are underneath.
    """
    rows = await conn.fetch(
        f"""
        SELECT btrim(t.{column}) AS value, count(*) AS n
          FROM {table} t
         WHERE {where}
           AND t.{column} IS NOT NULL
           AND btrim(t.{column}) <> ''
         GROUP BY 1
         ORDER BY 1
        """,
        *params,
    )
    blank = await conn.fetchval(
        f"""
        SELECT count(*) FROM {table} t
         WHERE {where}
           AND (t.{column} IS NULL OR btrim(t.{column}) = '')
        """,
        *params,
    )
    return {
        "values": [{"value": r["value"], "count": r["n"]} for r in rows],
        "blank": blank,
    }


def _account_label(value: str, company: str | None, account_type: str | None) -> str:
    """'DPL-MASTER-0264' — the company, the account's type, and its last 4 digits.

    A fifteen-digit account number identifies an account to a database and to
    nobody else; the three facts a person picks an account by are whose it is,
    what it is for, and the tail they recognise. Both parts come from the Bank
    row the number matches, so this is a view of Master Data rather than
    anything stored twice.

    The type is printed exactly as Master Data holds it, which is upper case by
    the rule set on that table.

    Degrades rather than invents. Missing either part drops it from the label,
    and an account no Bank row carries keeps its full number — that is the one
    case where the digits are the useful thing, because the fix is to go and add
    the account.
    """
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    tail = digits[-4:] if len(digits) >= 4 else digits
    parts = [p.strip() for p in (company, account_type) if p and p.strip()]
    if not parts or not tail:
        return value
    return "-".join(parts + [tail])


async def _account_values(conn, table: str, column: str, where: str,
                          params: list) -> dict:
    """The Account Number dropdown: distinct values, counts, and their labels.

    The bank lookup is a LATERAL taking one row, not a join. A plain join would
    multiply the row count by however many Bank entries share an account number,
    and the count beside each account is the number people use to sanity-check
    an import — silently doubling it would be worse than not showing it.

    Matched with account_digits, the same reduction the Company fill uses. It
    has to be: this workbook's own sheets disagree about the leading zero,
    '045563200000264' on four of them and '45563400002314' on the others, and a
    dropdown that labelled one and not the other would look like two different
    kinds of account.
    """
    bank_acct = staging.account_digits("b.account_number")
    value_acct = staging.account_digits("v.value")

    rows = await conn.fetch(
        f"""
        SELECT v.value, v.n, b.company, b.account_type, b.bank_name
          FROM (
            SELECT btrim(t.{column}) AS value, count(*) AS n
              FROM {table} t
             WHERE {where}
               AND t.{column} IS NOT NULL
               AND btrim(t.{column}) <> ''
             GROUP BY 1
          ) v
          LEFT JOIN LATERAL (
            SELECT b.company, b.account_type, b.bank_name
              FROM bank_master b
             WHERE b.account_number IS NOT NULL
               AND {bank_acct} <> ''
               AND {bank_acct} = {value_acct}
             -- An archived Bank row still names the account, but a live one
             -- describes it better, so it wins.
             ORDER BY b.is_active DESC, b.id
             LIMIT 1
          ) b ON true
         ORDER BY 1
        """,
        *params,
    )
    blank = await conn.fetchval(
        f"""
        SELECT count(*) FROM {table} t
         WHERE {where}
           AND (t.{column} IS NULL OR btrim(t.{column}) = '')
        """,
        *params,
    )
    return {
        "values": [
            {
                "value": r["value"],
                "count": r["n"],
                "label": _account_label(r["value"], r["company"], r["account_type"]),
                "company": r["company"],
                "account_type": r["account_type"],
                "bank_name": r["bank_name"],
                # Said outright rather than left to be inferred from a missing
                # label: an account with no Bank row is also an account whose
                # Company can never be filled in, and that is worth seeing here.
                "in_bank_master": r["bank_name"] is not None,
            }
            for r in rows
        ],
        "blank": blank,
    }


async def _filter_options(conn, table: str, where: str, params: list) -> dict:
    """Everything the three filter buttons need to draw themselves.

    One request on page load instead of three, and it reports which columns it
    resolved -- a company with no Company field gets company: null and the
    button greys itself out with a reason, rather than offering a filter that
    cannot work.

    Read across the whole table, not the current tab. A dropdown that only
    offers the accounts present in the rows you are already looking at cannot be
    used to change what you are looking at.
    """
    columns = await custom_fields.data_columns(conn)
    label = {c["name"]: c.get("displayname") or c["name"] for c in columns}

    date_col = await custom_fields.date_column(conn)
    account_col = await staging.account_column(conn)
    company_col = await staging.company_column(conn)

    out: dict = {
        "date": None, "account": None, "company": None,
        "total": await conn.fetchval(
            f"SELECT count(*) FROM {table} t WHERE {where}", *params),
    }

    if date_col:
        expr = _date_expr(date_col, columns)
        span = await conn.fetchrow(
            f"SELECT min({expr}) AS lo, max({expr}) AS hi "
            f"FROM {table} t WHERE {where}",
            *params,
        )
        out["date"] = {
            "column": date_col,
            "label": label.get(date_col, date_col),
            # Seeds the two calendars, and bounds them: a range outside the data
            # can only ever return nothing.
            "min": span["lo"].isoformat() if span["lo"] else None,
            "max": span["hi"].isoformat() if span["hi"] else None,
        }

    if account_col:
        out["account"] = {
            "column": account_col,
            "label": label.get(account_col, account_col),
            # Its own reader, not _distinct_values: each account is labelled
            # from the Bank row it matches.
            **await _account_values(conn, table, account_col, where, params),
        }

    if company_col:
        out["company"] = {
            "column": company_col,
            "label": label.get(company_col, company_col),
            **await _distinct_values(conn, table, company_col, where, params),
        }

    return out


async def _assert_live_master_ids(conn, values: dict) -> None:
    """Every non-null id must name an active row in this company's masters.

    Without this the only feedback on a stale dropdown is a raw Postgres
    foreign-key error, and an archived head stays bookable forever because the
    foreign key only checks existence, not is_active.
    """
    for field, value in values.items():
        if value is None:
            continue
        table, label = _MASTER_LOOKUPS[field]
        ok = await conn.fetchval(
            f"SELECT 1 FROM {table} WHERE id = $1 AND is_active = true", value
        )
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No active {label} with id {value} in this company. "
                    f"It may have been archived since the page loaded — reload and retry."
                ),
            )


# ---------- Transactions (the ledger) ----------------------------------------

@router.get("/")
async def list_transactions(
    project_id: int = None,
    head_id: int = None,
    date_from: str = Query(None, description="Start of the date range, YYYY-MM-DD."),
    date_to: str = Query(None, description="End of the date range, YYYY-MM-DD."),
    account: str = Query(
        None,
        description='Account numbers the rows were printed under, comma '
                    'separated for several. Matched on digits only, ignoring '
                    'leading zeros. Include "__none__" for rows with no '
                    'account.',
    ),
    company: str = Query(
        None,
        description='Company the row belongs to. Pass "__none__" for rows with '
                    'no company set.',
    ),
    sort: str = Query(None, description="Column to sort by; unknown names are ignored."),
    dir: str = Query("asc", description="asc or desc."),
    search: str = Query(
        "",
        description='Free text over every column and master name, across the '
                    'whole table. Words are AND-ed; "quote a phrase" to keep '
                    'it together.',
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(get_company_user),
):
    """
    List finalized transactions, optionally filtered.

    A manager or staff member sees only rows belonging to their assigned
    projects. Admins see everything. The project_id query param narrows within
    that; it cannot widen it.

    Query params (all optional):
      project_id  — only transactions for this project
      head_id     — only transactions for this head
      date_from   — start date (YYYY-MM-DD)
      date_to     — end date (YYYY-MM-DD)
      account     — one or more account numbers, comma separated
      company     — company abbreviation, matched case-insensitively
      sort, dir   — order by any listed column or joined master name
      search      — free text, matched against every column on the row
      page, limit — pagination; `total` in the response is the unpaged count

    Returns {columns, rows, total, page, limit, sort, dir}. It returned a bare
    array until it was paged; anything summing the response has to read `total`
    now, because rows is one page and len(rows) is a page size, not a count.
    """
    filters = ["1=1"]
    params = []
    idx = 1

    if project_id is not None:
        filters.append(f"project_id = ${idx}")
        params.append(project_id)
        idx += 1
    if head_id is not None:
        filters.append(f"head_id = ${idx}")
        params.append(head_id)
        idx += 1
    async with company_connection(user["schema"]) as conn:
        # Same rule as staging: the ledger reports its own columns rather than
        # asserting a fixed set, and they are the same set because the two
        # tables are kept in step.
        columns = await custom_fields.data_columns(conn)

        # Date, account number and company. Which column each of the three means
        # is the fieldmap's answer, not a constant, so this needs the connection.
        idx = await _facet_filters(
            conn, columns=columns, filters=filters, params=params, idx=idx,
            date_from=date_from, date_to=date_to, account=account, company=company,
        )

        scope = await scoping.visible_project_ids(conn, user)
        if scoping.scope_is_empty(scope):
            # Still report the columns. An empty result is a row count of zero,
            # not a table with no shape — the client draws its header from this.
            return {"columns": columns, "rows": [], "total": 0,
                    "page": page, "limit": limit, "sort": None, "dir": "asc"}
        # include_unassigned=False: the "unfiled rows belong to everyone" rule
        # exists so a fresh import can be classified, which only concerns
        # staging. A row that reached the ledger with no project is filed data
        # nobody's project owns, and only admins see it.
        clause, scope_params, idx = scoping.project_filter(
            scope, "t.project_id", idx, include_unassigned=False
        )
        if clause:
            filters.append(clause)
            params.extend(scope_params)

        term = (search or "").strip()
        if term:
            clause, sp, idx = _search_filter(
                term, columns, ("p.name", "p.code", "h.name", "b.bank_name"), idx
            )
            if clause:
                filters.append(clause)
                params.extend(sp)

        where = " AND ".join(filters)
        data_cols = ", ".join(f"t.{c['name']}" for c in columns)

        # The joins are repeated in the count because the search reaches into
        # them — counting over a bare temp_trans would over-report the moment
        # someone searches a head name.
        joins = """
            FROM transactions t
            LEFT JOIN projects p ON p.id = t.project_id
            LEFT JOIN head_master h ON h.id = t.head_id
            LEFT JOIN bank_master b ON b.id = t.bank_id
        """

        total = await conn.fetchval(f"SELECT count(*) {joins} WHERE {where}", *params)

        # Newest first when nothing is asked for, which is what a ledger is
        # usually read in. t.id DESC is the tiebreak and also the whole order
        # for a company with no date field.
        date_col = await custom_fields.date_column(conn)
        default_order = (f"t.{date_col} DESC NULLS LAST, t.id DESC"
                         if date_col else "t.id DESC")
        order_by, sort_applied, dir_applied = _sort_clause(
            sort, dir, columns, _LEDGER_EXTRA_SORTS, default_order
        )

        rows = await conn.fetch(
            f"""
            SELECT t.id, t.temp_trans_id, t.created_at,
                   t.project_id, t.bank_id, t.beneficiary_id,
                   t.head_id, t.rera_head_id, t.idw_head_id,
                   {data_cols},
                   p.name AS project_name,
                   p.code AS project_code,
                   h.name AS head_name,
                   b.bank_name
            {joins}
            WHERE {where}
            ORDER BY {order_by}
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, limit, (page - 1) * limit,
        )
    return {
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "limit": limit,
        # What was actually ordered by, which is not always what was asked for.
        # A sort naming a deleted field falls back rather than erroring, and the
        # screen needs to know so its header arrow does not claim otherwise.
        "sort": sort_applied,
        "dir": dir_applied,
    }


@router.get("/summary")
async def transaction_summary(
    date_from: str = None,
    date_to: str = None,
    user: dict = Depends(get_company_user),
):
    """
    Total amounts by head, for a date range.
    Returns one row per head with total CR and DR.

    Scoped like /transactions, so the dashboard totals a manager sees are the
    totals of their own projects, not the company's.
    """
    filters = ["1=1"]
    params = []
    idx = 1

    async with company_connection(user["schema"]) as conn:
        # Which column holds the date is the fieldmap's answer, not a constant.
        # Applied here rather than above because it needs a connection.
        date_col = await custom_fields.date_column(conn)
        if date_col:
            # Parsed, not passed straight through. The bound is compared against
            # a DATE column, so asyncpg types the parameter as a date and a bare
            # string raised DataError -- a 500 on what is a bad request. Latent
            # until now because the dashboard calls this with no range at all.
            columns = await custom_fields.data_columns(conn)
            expr = _date_expr(date_col, columns)
            for value, op, label in ((date_from, ">=", "The From date"),
                                     (date_to, "<=", "The To date")):
                if value is not None:
                    filters.append(f"{expr} {op} ${idx}")
                    params.append(_as_date(value, label))
                    idx += 1

        scope = await scoping.visible_project_ids(conn, user)
        if scoping.scope_is_empty(scope):
            return []
        # include_unassigned=False: the "unfiled rows belong to everyone" rule
        # exists so a fresh import can be classified, which only concerns
        # staging. A row that reached the ledger with no project is filed data
        # nobody's project owns, and only admins see it.
        clause, scope_params, idx = scoping.project_filter(
            scope, "t.project_id", idx, include_unassigned=False
        )
        if clause:
            filters.append(clause)
            params.extend(scope_params)

        where = " AND ".join(filters)

        rows = await conn.fetch(
            f"""
            SELECT h.name AS head_name,
                   SUM(CASE WHEN t.credit_debit = 'CR' THEN t.amount ELSE 0 END) AS total_cr,
                   SUM(CASE WHEN t.credit_debit = 'DR' THEN t.amount ELSE 0 END) AS total_dr
            FROM transactions t
            LEFT JOIN head_master h ON h.id = t.head_id
            WHERE {where}
            GROUP BY h.name
            ORDER BY h.name
            """,
            *params,
        )
    return [dict(r) for r in rows]


@router.post("/fill-company", dependencies=[Depends(require_manager)])
async def fill_company(user: dict = Depends(get_company_user)):
    """Set Company on every staged and posted row from its account number.

    The import already does this for the batch it just staged. This is for the
    other direction in time: a statement imported before its bank account was
    added to Master Data has a blank Company, and adding the account should fill
    it in rather than requiring a re-import.

    Safe to run repeatedly. It only writes where the value would change, and
    leaves rows whose account matches no bank exactly as they are.
    """
    async with company_connection(user["schema"]) as conn:
        async with conn.transaction():
            staged = await staging.fill_company_from_bank(conn, table="temp_trans")
            posted = await staging.fill_company_from_bank(conn, table="transactions")

    if staged.get("skipped"):
        raise HTTPException(400, staged["reason"])

    return {
        "staged_updated": staged["updated"],
        "posted_updated": posted["updated"],
        "account_column": staged["account_column"],
        "company_column": staged["company_column"],
        # Account numbers on staged rows that no bank record carries. The usual
        # reason nothing was filled, and the only one the user can act on.
        "unmatched_accounts": staged["unmatched_accounts"],
    }


@router.post("/fill-derived", dependencies=[Depends(require_manager)])
async def fill_derived(user: dict = Depends(get_company_user)):
    """Recompute every column this app derives, on staged and posted rows.

    Two of them today: Company, which follows from the account number via the
    Bank table, and FY, which follows from the row's own date.

    The import already does both for the batch it just staged, so this is for
    the other direction in time — rows imported before the column existed, or
    before the bank account was added to Master Data. Safe to run repeatedly:
    each fill only writes where the value would change.
    """
    async with company_connection(user["schema"]) as conn:
        async with conn.transaction():
            company_staged = await staging.fill_company_from_bank(conn, table="temp_trans")
            company_posted = await staging.fill_company_from_bank(conn, table="transactions")
            fy_staged = await staging.fill_financial_year(conn, table="temp_trans")
            fy_posted = await staging.fill_financial_year(conn, table="transactions")

    return {
        "company": {
            "staged_updated": company_staged["updated"],
            "posted_updated": company_posted["updated"],
            "skipped": company_staged.get("skipped", False),
            "reason": company_staged.get("reason"),
            # Account numbers on staged rows that no bank record carries — the
            # usual reason nothing was filled, and the only one you can act on.
            "unmatched_accounts": company_staged.get("unmatched_accounts"),
        },
        "financial_year": {
            "staged_updated": fy_staged["updated"],
            "posted_updated": fy_posted["updated"],
            "skipped": fy_staged.get("skipped", False),
            "reason": fy_staged.get("reason"),
            # The only reason a row can be left without one.
            "undated_rows": fy_staged.get("undated_rows"),
        },
    }


@router.delete("/all", dependencies=[Depends(require_level(permissions.COMPANY_ADMIN))])
async def delete_all_transactions(user: dict = Depends(get_company_user)):
    """Empty the ledger.

    Company admin only, not manager. Clearing staging throws away work nobody
    has posted yet; this throws away the posted record itself, and the two are
    not the same decision.

    It is recoverable, which is the reason it can exist at all. Posting a row
    does not consume it: the temp_trans row stays, still classified, and the
    only thing stopping it being posted twice is UNIQUE (temp_trans_id) on this
    table. Remove the transaction and that row becomes postable again -- so
    "delete the ledger" means "un-post everything", not "lose it". Anything
    imported is still in Imported Rows with its classification intact.

    No scoping filter. A partial wipe of somebody's visible projects would leave
    a ledger that balances for nobody, and the level required here already
    exceeds the level at which project scoping applies.
    """
    async with company_connection(user["schema"]) as conn:
        async with conn.transaction():
            total = await conn.fetchval("SELECT count(*) FROM transactions")
            # Counted before the delete: afterwards there is nothing left to
            # join against and the number would always be zero.
            restored = await conn.fetchval(
                "SELECT count(*) FROM transactions WHERE temp_trans_id IS NOT NULL")
            await conn.execute("DELETE FROM transactions")

    logger.info("[ledger] cleared: %d transactions, %d rows back to postable",
                total, restored)
    return {"deleted": total, "rows_postable_again": restored}


# ---------- Temp Import (raw rows before finalization) -----------------------

@router.get("/temp-trans")
async def list_temp_trans(
    batch_id: int = None,
    classified: bool = None,
    date_from: str = Query(None, description="Start of the date range, YYYY-MM-DD."),
    date_to: str = Query(None, description="End of the date range, YYYY-MM-DD."),
    account: str = Query(
        None,
        description='Account numbers the rows were printed under, comma '
                    'separated for several. Matched on digits only, ignoring '
                    'leading zeros. Include "__none__" for rows with no '
                    'account.',
    ),
    company: str = Query(
        None,
        description='Company the row belongs to. Pass "__none__" for rows with '
                    'no company set.',
    ),
    sort: str = Query(None, description="Column to sort by; unknown names are ignored."),
    dir: str = Query("asc", description="asc or desc."),
    search: str = Query(
        "",
        description='Free text over every column and master name, across the '
                    'whole table. Words are AND-ed; "quote a phrase" to keep '
                    'it together.',
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    user: dict = Depends(get_company_user),
):
    """
    List raw rows from the last import, before they're finalized.

    Scoped rows follow the same rule as the ledger, and the "no project yet"
    arm carries the weight here: a freshly imported row has no project until
    someone classifies it, so every manager and staff member can see it and
    file it. Once it names a project, only that project's people keep seeing it.

    Params:
      batch_id    — filter by import batch (which PDF upload)
      classified  — true = only classified rows, false = only unclassified
      date_from,
      date_to     — the date range, on whichever column the fieldmap calls the
                    date field. Either end may be given on its own.
      account     — one or more account numbers, comma separated. Matched on
                    digits only and ignoring leading zeros, so a value typed
                    with spaces, or copied from a sheet that dropped the
                    leading zero, still finds its rows
      company     — company abbreviation, matched case-insensitively
      sort, dir   — order by any data column or joined master name. An
                    unrecognised name falls back to batch/row order and the
                    response reports what was applied.
      search      — free text over every column and every joined master name,
                    matched against the whole table rather than the page being
                    shown. Words are AND-ed and a quoted run stays a phrase;
                    `search_terms` comes back so the browser marks up exactly
                    what was matched.
      page, limit — pagination; `total` is the count matching the filters

    `total` is the filtered count and `summary` is deliberately not: the Clear
    button has to say how much it will delete, which is everything staged, not
    what the current tab and search happen to show.
    """
    filters = ["1=1"]
    params = []
    idx = 1

    if batch_id is not None:
        filters.append(f"t.batch_id = ${idx}")
        params.append(batch_id)
        idx += 1
    if classified is not None:
        filters.append(f"t.is_classified = ${idx}")
        params.append(classified)
        idx += 1

    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        clause, scope_params, idx = scoping.project_filter(scope, "t.project_id", idx)
        if clause:
            filters.append(clause)
            params.extend(scope_params)
        elif scoping.scope_is_empty(scope):
            # Scoped to nothing, but unfiled rows are still everyone's to claim.
            filters.append("t.project_id IS NULL")

        # The data columns are read from the live table, not written out here.
        # A custom field is a real column on temp_trans, and a fixed SELECT is
        # why one could be created, matched during parsing and stored, and still
        # never appear on this screen. Same approach as DPL's get_master_rows:
        # the server decides the column set, the client renders what it is sent.
        columns = await custom_fields.data_columns(conn)
        data_cols = ", ".join(f"t.{c['name']}" for c in columns)

        # Date, account number and company. Resolved from the fieldmap, so the
        # Account Number filter means the same column the Company fill reads.
        idx = await _facet_filters(
            conn, columns=columns, filters=filters, params=params, idx=idx,
            date_from=date_from, date_to=date_to, account=account, company=company,
        )

        term = (search or "").strip()
        if term:
            clause, sp, idx = _search_filter(
                term, columns,
                ("p.name", "p.code", "h.name", "rh.name", "ih.name", "bn.name"), idx
            )
            if clause:
                filters.append(clause)
                params.extend(sp)

        where = " AND ".join(filters)

        # The joins resolve each id to the name the user picked in the master
        # tables, so the staging screen can show "Site Materials" rather than
        # "head_id: 4" without the browser holding a copy of every master list.
        # Repeated in the count query because the search reaches into them.
        joins = """
            FROM temp_trans t
            LEFT JOIN projects            p  ON p.id  = t.project_id
            LEFT JOIN head_master         h  ON h.id  = t.head_id
            LEFT JOIN rera_head_master    rh ON rh.id = t.rera_head_id
            LEFT JOIN idw_head_master     ih ON ih.id = t.idw_head_id
            LEFT JOIN beneficiary_master  bn ON bn.id = t.beneficiary_id
        """

        total = await conn.fetchval(f"SELECT count(*) {joins} WHERE {where}", *params)

        # The order the file was read in, when nothing else is asked for — and
        # the tiebreak under everything else, so paging is stable.
        order_by, sort_applied, dir_applied = _sort_clause(
            sort, dir, columns, _TEMP_EXTRA_SORTS, "t.batch_id, t.row_number"
        )

        rows = await conn.fetch(
            f"""
            SELECT t.id, t.batch_id, t.row_number, t.is_classified, t.is_locked,
                   t.created_at,
                   t.project_id, t.beneficiary_id, t.head_id, t.rera_head_id,
                   t.idw_head_id,
                   {data_cols},
                   p.name  AS project_name,
                   p.code  AS project_code,
                   h.name  AS head_name,
                   rh.name AS rera_head_name,
                   ih.name AS idw_head_name,
                   bn.name AS beneficiary_name
            {joins}
            WHERE {where}
            ORDER BY {order_by}
            LIMIT ${idx} OFFSET ${idx + 1}
            """,
            *params, limit, (page - 1) * limit,
        )

        editable = await _editable_columns(conn)

        # Unfiltered totals, so the Clear button can state what it is about to
        # remove and grey itself out when there is nothing to remove. Taken
        # here rather than counted in the browser, which only ever holds the
        # rows matching the current tab.
        summary = dict(await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM temp_trans)      AS staged_total,
                   (SELECT count(*) FROM import_batches)  AS batches,
                   (SELECT count(*) FROM transactions
                     WHERE temp_trans_id IS NOT NULL)     AS posted
            """
        ))

    return {
        "columns": columns,
        "rows": [dict(r) for r in rows],
        "summary": summary,
        # What the browser should mark up in the cells it draws. Sent rather
        # than left for the client to work out, so highlighting and matching can
        # never disagree about what the query meant.
        "search_terms": highlight_terms(term),
        # Which fields the row editor may change and which column each writes,
        # read from this company's fieldmap. Sent so the dialog is built from
        # the company's own configuration rather than from a list of display
        # names written into the page.
        "editable": editable,
        "total": total,
        "page": page,
        "limit": limit,
        # What was actually ordered by. A sort naming a field deleted since the
        # page loaded falls back instead of erroring, and the header arrow has
        # to follow that rather than claim a sort that did not happen.
        "sort": sort_applied,
        "dir": dir_applied,
    }


@router.get("/temp-trans/filters")
async def temp_trans_filter_options(user: dict = Depends(get_company_user)):
    """The values the Date, Account Number and Company filters can offer.

    Read once when the screen loads instead of three requests, and read across
    the whole staging table rather than the tab in front of you — a dropdown
    offering only the accounts already on screen cannot be used to change what
    is on screen.

    Scoped like the list itself. An account number is not sensitive on its own,
    but the set of them present in a company's staging is, and a staff member
    should not learn it from a filter dropdown.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        clause, params, _ = scoping.project_filter(scope, "t.project_id", 1)
        if clause:
            where = clause
        elif scoping.scope_is_empty(scope):
            where = "t.project_id IS NULL"
        else:
            where = "1=1"
        return await _filter_options(conn, "temp_trans", where, params)


async def _rule_context(conn, account_type: str, account_number: str) -> dict:
    """Resolve everything a rule check and a rule fix share, or 400 saying why.

    Returns the account it resolved, the grid's heads per direction (`expected`,
    and the same thing as id sets in `allowed_ids`), and the account type's
    conditions with the columns they test — everything needed to decide any one
    row, and nothing that depends on which row.

    Checked here, identically, on both endpoints — the fix must never trust
    the browser's copy of a check that may be minutes old, and since the rule
    is now editable from the Rules page it can genuinely have changed between
    the check and the fix.
    """
    wanted = (account_type or "").strip().upper()
    digits = staging.normalise_account(account_number)
    if not digits:
        raise HTTPException(400, "That account number has no digits in it.")

    account_col = await staging.account_column(conn)
    if not account_col:
        raise HTTPException(
            400, "This company has no account number field mapped, so rows "
                 "cannot be matched to an account. Map one on the Field "
                 "Mapping page first.")

    # The account's type comes from the Bank master, matched on digits like
    # everything else. A live row describes the account better than an
    # archived one, so it wins — same preference the filter labels use.
    bank_acct = staging.account_digits("b.account_number")
    bank = await conn.fetchrow(
        f"""
        SELECT b.account_number, b.account_type, b.bank_name, b.company
          FROM bank_master b
         WHERE b.account_number IS NOT NULL
           AND {bank_acct} <> ''
           AND {bank_acct} = $1
         ORDER BY b.is_active DESC, b.id
         LIMIT 1
        """,
        digits,
    )
    if bank is None:
        raise HTTPException(
            400, f"Account {account_number} is not in the Bank master, so its "
                 f"type is unknown. Add it under Master Data first.")
    actual = (bank["account_type"] or "").strip().upper()
    if actual != wanted:
        raise HTTPException(
            400, f"Account {account_number} is recorded as "
                 f"{'a ' + actual if actual else 'an untyped'} account in the "
                 f"Bank master, not {wanted}.")

    # Read after the account has been confirmed to be of this type, so a type
    # with no rule is only reported once the account itself checks out — being
    # told "no rule for MASTER" about an account that is actually RERA would
    # send someone to the Rules page to fix the wrong thing.
    #
    # Conditions first, because whether the grid is allowed to be blank for this
    # type depends on whether the user wrote any: a type judged entirely by
    # conditions is a rule, and refusing to run it would be refusing to run the
    # only thing the user did write.
    columns = await custom_fields.data_columns(conn)
    try:
        conditions = await rules.load_conditions(
            conn, wanted, {c["name"] for c in columns})
    except rules.MissingRuleHeads as e:
        # Its own 400, without the "rules are set for" tail below: this is not
        # a type with no rule, it is a rule that exists and cannot run, and the
        # sentence already names it and says where to repair it.
        raise HTTPException(400, str(e))

    try:
        expected = await rules.allowed_heads(
            conn, wanted, allow_empty=bool(conditions))
    except rules.MissingRuleHeads as e:
        supported = await rules.supported_types(conn)
        extra = (f" Rules are set for: {', '.join(supported)}." if supported
                 else " No account type has a rule yet.")
        raise HTTPException(400, str(e) + extra)

    allowed_ids = {d: {h["id"] for h in heads} for d, heads in expected.items()}
    return {
        "target": rules.TARGET,
        "digits": digits,
        "account_col": account_col,
        "bank": bank,
        "expected": expected,
        "allowed_ids": allowed_ids,
        "conditions": conditions,
        # The columns those conditions test, resolved once: the row queries
        # below select exactly these and nothing else user-named.
        "fields": rules.subject_fields(conditions),
        "columns": columns,
    }


@router.post("/temp-trans/check-rules")
async def check_temp_rules(
    account_type: str = Body(..., description="The type the Bank master gives "
                                              "the account — MASTER, RERA, IDW "
                                              "or FREE."),
    account_number: str = Body(..., description="The account whose staged rows "
                                                "to check, matched on digits "
                                                "only."),
    user: dict = Depends(get_company_user),
):
    """Check one account's staged rows against its account-type rule.

    Reads only — nothing is changed until /check-rules/apply is called with
    the rows the user agreed to fix. Every row printed under the account is
    judged by its direction against the company's own `rule` table: a credit
    must carry one of the heads marked CR for this account type, a debit one of
    those marked DR. Which heads those are is the user's answer, entered on the
    Rules page, not anything written here.

    A row with no CR/DR marker cannot be judged and is reported separately
    rather than counted on either side. Scoped like the list itself.

    Conditions outrank the grid. A row the first matching condition describes is
    judged by that condition alone and carries its id, so the dialog can say
    which sentence decided it and offer that sentence's heads rather than the
    column's general ones.
    """
    async with company_connection(user["schema"]) as conn:
        ctx = await _rule_context(conn, account_type, account_number)
        rule, digits, account_col = ctx["target"], ctx["digits"], ctx["account_col"]
        bank, expected, allowed_ids = ctx["bank"], ctx["expected"], ctx["allowed_ids"]
        conditions, fields = ctx["conditions"], ctx["fields"]

        filters = ["1=1"]
        params: list = []
        idx = 1

        scope = await scoping.visible_project_ids(conn, user)
        clause, scope_params, idx = scoping.project_filter(scope, "t.project_id", idx)
        if clause:
            filters.append(clause)
            params.extend(scope_params)
        elif scoping.scope_is_empty(scope):
            filters.append("t.project_id IS NULL")

        filters.append(f"{staging.account_digits(f't.{account_col}')} = ${idx}")
        params.append(digits)
        idx += 1

        # Date and amount ride along so the dialog can say which rows it
        # means — a conflict list of bare ids cannot be reviewed.
        columns = ctx["columns"]
        date_col = await custom_fields.date_column(conn)
        date_sel = (f"{_date_expr(date_col, columns)} AS txn_date,"
                    if date_col else "NULL::date AS txn_date,")

        field = rule["field"]
        rows = await conn.fetch(
            f"""
            SELECT t.id, t.batch_id, t.row_number, t.is_locked,
                   upper(btrim(coalesce(t.credit_debit, ''))) AS direction,
                   t.amount,
                   {date_sel}
                   t.{field} AS current_id,
                   m.name AS current_name
                   {rules.subject_sql(fields)}
              FROM temp_trans t
              LEFT JOIN {rule['master_table']} m ON m.id = t.{field}
             WHERE {' AND '.join(filters)}
             ORDER BY t.batch_id, t.row_number
            """,
            *params,
        )

        checked: list[dict] = []
        ok = conflicts = locked_conflicts = no_direction = 0
        for r in rows:
            row = dict(r)
            direction = row["direction"] or None
            row["direction"] = direction
            # Popped, not read: the columns the conditions test were fetched for
            # this decision, and shipping them back under s0/s1 would put raw
            # statement text on the wire under names nothing can explain.
            subjects = rules.take_subjects(row, fields)
            _heads, ids, cond = rules.resolve(
                direction, subjects, conditions, expected, allowed_ids)
            status = rules.judge(ids, row["current_id"])
            row["status"] = status
            # Which sentence judged this row — null when the grid did. The
            # dialog reads its heads from the same place, so what it offers as
            # a replacement is always what the check just used.
            row["rule_id"] = cond["id"] if cond else None
            if status == "ok":
                ok += 1
            elif status == "conflict":
                conflicts += 1
                if row["is_locked"]:
                    locked_conflicts += 1
            else:
                no_direction += 1
            checked.append(row)

        # The company's own name for the column being judged, when a fieldmap
        # row mirrors this master — the dialog should speak the fieldmap's
        # language, not this file's.
        ecols = await _editable_columns(conn)
        target_label = (ecols.get(rule["mirrors"]) or {}).get("label") or rule["label"]
        # The fieldmap's word for each column, so a condition reads as "when a
        # debit's Narration contains..." rather than naming the raw column.
        labels = {c["name"]: c["displayname"] for c in columns}

    wanted = (account_type or "").strip().upper()
    return {
        "account_type": wanted,
        "account": {
            "value": account_number,
            "label": _account_label(bank["account_number"], bank["company"],
                                    bank["account_type"]),
            "bank_name": bank["bank_name"],
        },
        "target": {"field": field, "label": target_label},
        "expected": expected,
        # Written from the rule that just ran, so the sentence on screen can
        # never describe a rule other than the one that judged these rows.
        "why": rules.explain(wanted, expected, conditions),
        # Keyed by id, because that is how a row refers to the one that judged
        # it. Sent once rather than repeated on every row it decided.
        "conditions": {
            str(c["id"]): {
                "sentence": rules.describe(c, labels.get(c["subject_field"])),
                "direction": c["direction"],
                "heads": c["heads"],
            }
            for c in conditions
        },
        "rows": checked,
        "summary": {
            "total": len(checked),
            "ok": ok,
            "conflicts": conflicts,
            "locked_conflicts": locked_conflicts,
            "no_direction": no_direction,
        },
    }


@router.post("/temp-trans/check-rules/apply")
async def apply_temp_rules(
    account_type: str = Body(...),
    account_number: str = Body(...),
    rows: list[dict] = Body(..., description="[{id, head_id}] — the conflicting "
                                             "rows and the rule head chosen for "
                                             "each."),
    user: dict = Depends(get_company_user),
):
    """Replace the heads the rule found wrong, as chosen in the dialog.

    Applies the rule rather than trusting the request: every target head is
    re-checked against what the rule allows for that row in particular — its
    direction, and any condition that describes it — on the row's current
    state. This is not a bulk edit endpoint wearing a rule's name, and it is
    the one place that matters, since a condition can admit a head the grid
    leaves blank and must not admit it anywhere else.

    Locked rows are skipped and counted, the same standing the padlock
    has everywhere else; rows that no longer match the account or the caller's
    scope are skipped too, because they are no longer what the user saw.

    Writes the id column and the display column that mirrors it together,
    exactly as the row editor does — the pair must never disagree.
    """
    if not rows:
        raise HTTPException(400, "No rows to change.")
    wanted_rows: dict[int, int] = {}
    for entry in rows:
        try:
            wanted_rows[int(entry["id"])] = int(entry["head_id"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Each row needs an id and a head_id.")

    async with company_connection(user["schema"]) as conn:
        ctx = await _rule_context(conn, account_type, account_number)
        rule, digits, account_col = ctx["target"], ctx["digits"], ctx["account_col"]
        expected, allowed_ids = ctx["expected"], ctx["allowed_ids"]
        conditions, fields = ctx["conditions"], ctx["fields"]

        filters = ["t.id = ANY($1::bigint[])"]
        params: list = [list(wanted_rows)]
        idx = 2

        scope = await scoping.visible_project_ids(conn, user)
        clause, scope_params, idx = scoping.project_filter(scope, "t.project_id", idx)
        if clause:
            filters.append(clause)
            params.extend(scope_params)
        elif scoping.scope_is_empty(scope):
            filters.append("t.project_id IS NULL")

        filters.append(f"{staging.account_digits(f't.{account_col}')} = ${idx}")
        params.append(digits)
        idx += 1

        # FOR UPDATE, because the scope and account checks above are made on
        # this read and enforced by the write below — and project_id, which
        # scope is decided by, is a column the row editor can change. Without
        # the lock another session could move a row out of this caller's scope
        # between the two statements and the write would still land on it.
        # Held for the rest of the transaction, which is the next statement.
        current = await conn.fetch(
            f"""
            SELECT t.id, t.is_locked,
                   upper(btrim(coalesce(t.credit_debit, ''))) AS direction
                   {rules.subject_sql(fields)}
              FROM temp_trans t
             WHERE {' AND '.join(filters)}
             FOR UPDATE
            """,
            *params,
        )

        names = {h["id"]: h["name"] for heads in expected.values() for h in heads}
        names.update({h["id"]: h["name"]
                      for c in conditions for h in c["heads"]})
        # The fieldmap's word for each column, so a refusal below names the
        # column the way the user's own screens do rather than exposing
        # field_text_1 — the same courtesy the check response already extends.
        labels = {c["name"]: c["displayname"] for c in ctx["columns"]}
        by_head: dict[int, list[int]] = {}
        skipped_locked = 0
        found_ids: set[int] = set()
        for r in current:
            found_ids.add(r["id"])
            chosen = wanted_rows[r["id"]]
            direction = r["direction"] or None
            # Re-decided per row, not per request: since a condition can give
            # two rows on the same side different answers, "what this row is
            # allowed to be" is a question about the row. Same call the check
            # made, on the row as it stands now.
            _heads, ids, cond = rules.resolve(
                direction, rules.subject_values(r, fields),
                conditions, expected, allowed_ids)
            if ids is None or chosen not in ids:
                raise HTTPException(
                    400,
                    f"'{names.get(chosen, chosen)}' is not one the "
                    f"{(account_type or '').strip().upper()} rule allows for a "
                    f"{direction or 'directionless'} row"
                    + (f" matching “{rules.phrase(cond, labels.get(cond['subject_field']))}”"
                       if cond else "")
                    + ". The rows may have changed since the check — run Check "
                      "Rules again.",
                )
            if r["is_locked"]:
                skipped_locked += 1
                continue
            by_head.setdefault(chosen, []).append(r["id"])

        ecols = await _editable_columns(conn)
        mirror = ecols.get(rule["mirrors"])

        updated_ids: list[int] = []
        for head_id, ids in by_head.items():
            sets = [f"{rule['field']} = $1"]
            uparams: list = [head_id]
            if mirror:
                uparams.append(names[head_id])
                sets.append(f"{mirror['column']} = ${len(uparams)}")
            uparams.append(ids)
            done = await conn.fetch(
                f"UPDATE temp_trans SET {', '.join(sets)} "
                # NOT is_locked again in the WHERE: the read above and this
                # write are the belt and braces against a lock landing between
                # them.
                f"WHERE id = ANY(${len(uparams)}::bigint[]) AND NOT is_locked "
                f"RETURNING id",
                *uparams,
            )
            updated_ids.extend(row["id"] for row in done)

    logger.info("[check-rules] %s %s: %d updated, %d locked skipped, %d gone",
                (account_type or "").strip().upper(), digits,
                len(updated_ids), skipped_locked,
                len(set(wanted_rows) - found_ids))
    return {
        "status": "applied",
        "updated": len(updated_ids),
        "updated_ids": sorted(updated_ids),
        "skipped_locked": skipped_locked,
        "skipped_missing": len(set(wanted_rows) - found_ids),
    }


@router.get("/filters")
async def transaction_filter_options(user: dict = Depends(get_company_user)):
    """The same three filters, for the ledger.

    include_unassigned=False, matching the ledger list: a posted row with no
    project is filed data nobody's project owns and only admins see it, so its
    account number should not appear in a scoped user's dropdown either.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        if scoping.scope_is_empty(scope):
            return {"date": None, "account": None, "company": None, "total": 0}
        clause, params, _ = scoping.project_filter(
            scope, "t.project_id", 1, include_unassigned=False
        )
        return await _filter_options(
            conn, "transactions", clause or "1=1", params
        )


@router.delete("/temp-trans", dependencies=[Depends(require_manager)])
async def clear_temp_trans(schema: str = Depends(get_current_schema)):
    """
    Clear the staging table — every staged row, from every batch.

    DPL's "Truncate All Data" button, adapted to the one thing that differs
    here: `master` stood alone, but temp_trans has the ledger hanging off it.
    So this is a guarded DELETE, never TRUNCATE. `TRUNCATE temp_trans CASCADE`
    would silently take `transactions` with it, and losing the ledger to a
    "clear the import staging area" button is not a recoverable mistake.

    Refused outright if any staged row has been posted. transactions.
    temp_trans_id is ON DELETE RESTRICT, so Postgres would block it anyway —
    checking first turns a foreign-key error into a sentence that says which
    rows are in the way.

    The batches go too. They cascade to their rows, and leaving them behind
    would keep every file_hash on record, so re-importing the same statement
    you just cleared would come back 409 "already uploaded".

    Not scoped by project: this is an all-or-nothing reset, and clearing "the
    rows I can see" would leave a half-empty staging table that looks cleared
    to the person who pressed the button and not to anyone else.
    """
    async with company_connection(schema) as conn:
        posted = await conn.fetchval(
            "SELECT count(*) FROM transactions WHERE temp_trans_id IS NOT NULL"
        )
        if posted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot clear staging: {posted} staged "
                f"{'row is' if posted == 1 else 'rows are'} already posted to "
                f"the ledger. Reverse those transactions first, or discard the "
                f"unposted batches individually.",
            )

        # Same standing as the posted check: a Clear All that silently took
        # locked rows with it would make the lock a decoration.
        locked = await conn.fetchval(
            "SELECT count(*) FROM temp_trans WHERE is_locked"
        )
        if locked:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot clear staging: {locked} "
                f"{'row is' if locked == 1 else 'rows are'} locked. Unlock "
                f"{'it' if locked == 1 else 'them'} first.",
            )

        rows = await conn.fetchval("SELECT count(*) FROM temp_trans")
        batches = await conn.fetchval("SELECT count(*) FROM import_batches")
        # One statement: temp_trans cascades from import_batches, so deleting
        # the parents clears both sides atomically.
        await conn.execute("DELETE FROM import_batches")
        # Anything left had no batch behind it — belt and braces.
        await conn.execute("DELETE FROM temp_trans")

    return {"status": "cleared", "rows_removed": rows, "batches_removed": batches}


@router.delete("/temp-trans/{row_id}", dependencies=[Depends(require_manager)])
async def delete_temp_row(row_id: int, user: dict = Depends(get_company_user)):
    """
    Remove one staged row.

    The narrow version of Clear All, and the reason it exists: a parser will
    occasionally turn a page header or a carried-forward balance line into a
    transaction, and the only fix available was to clear the entire staging
    table and re-import every statement in it.

    Manager and above, matching Clear All and discard-batch. Deleting a staged
    row destroys parsed work, and the three destructive operations on this data
    should not sit at two different levels.

    Two refusals, in this order:
      * outside your scope — 404, the same answer as a row that does not exist,
        so this cannot be used to probe which ids belong to other projects
      * already posted — 409. transactions.temp_trans_id is ON DELETE RESTRICT,
        so Postgres blocks it regardless; checking first names the transaction
        that is holding on instead of surfacing a foreign-key error.

    The batch is left alone even when this empties it. row_count records what
    the file produced at import time, which is history and stays true; the
    batches list already counts live rows separately.
    """
    async with company_connection(user["schema"]) as conn:
        row = await conn.fetchrow(
            "SELECT id, batch_id, row_number, project_id, is_locked "
            "FROM temp_trans WHERE id = $1",
            row_id,
        )
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Staged row not found.")

        scope = await scoping.visible_project_ids(conn, user)
        # can_use_project passes a NULL project, which is the rule the list uses
        # too: an unfiled row belongs to everyone, so a row nobody has
        # classified yet is still removable.
        if not scoping.can_use_project(scope, row["project_id"]):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Staged row not found.")

        # A lock that stopped edits but not deletion would protect a row from
        # a typo and not from the bin. Same unlock-first rule for both.
        if row["is_locked"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This row is locked. Unlock it first to delete it.",
            )

        posted = await conn.fetchval(
            "SELECT id FROM transactions WHERE temp_trans_id = $1", row_id
        )
        if posted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Row {row_id} is already posted to the ledger as transaction "
                f"{posted}. Reverse that transaction before removing the staged row.",
            )

        await conn.execute("DELETE FROM temp_trans WHERE id = $1", row_id)

    return {"status": "deleted", "row_id": row_id, "batch_id": row["batch_id"]}


# Which id column each editable dropdown writes, and which fieldmap.mirrors
# value names its display column. The master table itself comes from
# _MASTER_LOOKUPS / _MIRROR_TABLES, so nothing here names a table twice.
_EDITABLE_PICKERS = {
    "project_id": "project",
    "head_id": "head",
    "rera_head_id": "rera_head",
    "idw_head_id": "idw_head",
}


async def _editable_columns(conn) -> dict:
    """Which physical column each editable field writes, and its label.

    The four dropdowns are found through fieldmap.mirrors, so they follow
    whatever this company called the columns — BUSINESS UNIT is the project
    column here because that fieldmap row says mirrors='project', not because
    anything is keyed to the words "business unit". Narration has no master
    behind it and is matched by display name.

    Returned to the client so the edit dialog is built from the company's own
    fieldmap rather than from a list written into the page.
    """
    display = {
        c["name"]: (c.get("displayname") or c["name"])
        for c in await custom_fields.data_columns(conn)
    }
    rows = await conn.fetch(
        "SELECT fieldname, mirrors FROM fieldmap "
        "WHERE mirrors = ANY($1::text[]) AND is_active = true",
        list(_MIRROR_TABLES),
    )

    out: dict = {}
    for row in rows:
        column, target = row["fieldname"], row["mirrors"]
        if not _CUSTOM_FIELD_RE.match(column or ""):
            continue
        out[target] = {"column": column, "label": display.get(column, column)}

    narration = await staging.narration_column(conn)
    if narration:
        out["narration"] = {"column": narration,
                            "label": display.get(narration, narration)}
    return out


@router.patch("/temp-trans/{row_id}")
async def edit_temp_row(
    row_id: int,
    payload: dict = Body(
        ...,
        description="Any of project_id, head_id, rera_head_id, idw_head_id "
                    "(master row ids, null to clear) and narration (free text).",
    ),
    user: dict = Depends(get_company_user),
):
    """Edit one staged row.

    Replaces the Classify dialog. The difference is not cosmetic: classify
    filled a row in once and then refused to touch it again — it required
    `is_classified = false` — so a value picked by mistake could only be fixed by
    deleting the row and re-importing the statement. This can be run as often as
    the row needs.

    A raw dict rather than named Body parameters, because PATCH has to tell
    "leave this alone" apart from "set this to nothing", and a missing key and a
    null both arrive as None through a typed parameter. A key that is present
    and null clears the field; a key that is absent is not written at all.

    Each dropdown writes two columns: the id, which is the record, and the
    display column that mirrors it, which is what the table shows. They are
    written together so they cannot disagree — the reason the mirrors mechanism
    exists at all.
    """
    unknown = set(payload) - set(_EDITABLE_PICKERS) - {"narration"}
    if unknown:
        raise HTTPException(
            400,
            f"Not editable: {', '.join(sorted(unknown))}. This screen edits "
            f"{', '.join(sorted(set(_EDITABLE_PICKERS) | {'narration'}))} only — "
            f"the statement's own columns are what the bank sent and are left "
            f"as imported.",
        )
    if not payload:
        raise HTTPException(400, "Nothing to change.")

    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)
        current = await conn.fetchrow(
            "SELECT id, project_id, is_locked FROM temp_trans WHERE id = $1", row_id
        )
        if current is None or not scoping.can_use_project(scope, current["project_id"]):
            raise HTTPException(404, "Staged row not found.")
        if current["is_locked"]:
            # 409, not 403: nothing about the caller is wrong — the row's own
            # state refuses the write, and the fix is on the row.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This row is locked. Unlock it first to edit it.",
            )

        # Filing a row under a project you cannot see would move it out of your
        # own scope and lose it, so the target project is checked as well as the
        # row — the same two checks classify made.
        if "project_id" in payload and payload["project_id"] is not None:
            if not scoping.can_use_project(scope, payload["project_id"]):
                raise HTTPException(403, "You are not assigned to that project.")

        chosen = {
            field: payload[field]
            for field in _EDITABLE_PICKERS
            if field in payload and payload[field] is not None
        }
        await _assert_live_master_ids(conn, chosen)

        columns = await _editable_columns(conn)
        sets: list[str] = []
        params: list = []

        def _set(column: str, value) -> None:
            params.append(value)
            sets.append(f"{column} = ${len(params)}")

        for field, target in _EDITABLE_PICKERS.items():
            if field not in payload:
                continue
            value = payload[field]
            _set(field, value)
            mirror = columns.get(target)
            if not mirror:
                # No display column mirrors this master, so the id is the whole
                # record here. Nothing to keep in step.
                continue
            if value is None:
                _set(mirror["column"], None)
            else:
                name = await conn.fetchval(
                    f"SELECT name FROM {_MIRROR_TABLES[target]} WHERE id = $1",
                    value,
                )
                _set(mirror["column"], name)

        if "narration" in payload:
            mirror = columns.get("narration")
            if not mirror:
                raise HTTPException(
                    400, "This company has no narration column to write to."
                )
            text = payload["narration"]
            text = (str(text).strip() or None) if text is not None else None
            _set(mirror["column"], text)

        params.append(row_id)
        updated = await conn.fetchrow(
            f"UPDATE temp_trans SET {', '.join(sets)} "
            f"WHERE id = ${len(params)} RETURNING id",
            *params,
        )

    if updated is None:
        raise HTTPException(404, "Staged row not found.")
    return {"status": "updated", "row_id": row_id, "changed": sorted(payload)}


@router.post("/temp-trans/{row_id}/lock")
async def set_temp_row_lock(
    row_id: int,
    locked: bool = Body(..., embed=True),
    user: dict = Depends(get_company_user),
):
    """Lock or unlock one staged row.

    While locked, PATCH and DELETE on the row are refused with a 409, and
    Clear All refuses while any locked row exists — the row is done being
    worked on until someone deliberately unlocks it.

    Same access as editing, not manager-gated: the lock protects a row from
    accident, not from colleagues, and anyone trusted to edit a row is trusted
    to say it is finished. Setting the state it already has succeeds and does
    nothing, so two people locking the same row is not an error.

    Scope-checked like every other row operation — a row filed under a project
    you cannot see is a row you cannot lock, and the answer is the same 404 a
    missing row gets.
    """
    async with company_connection(user["schema"]) as conn:
        current = await conn.fetchrow(
            "SELECT id, project_id FROM temp_trans WHERE id = $1", row_id
        )
        scope = await scoping.visible_project_ids(conn, user)
        if current is None or not scoping.can_use_project(scope, current["project_id"]):
            raise HTTPException(404, "Staged row not found.")

        await conn.execute(
            "UPDATE temp_trans SET is_locked = $1 WHERE id = $2", locked, row_id
        )

    return {"status": "locked" if locked else "unlocked", "row_id": row_id,
            "is_locked": locked}


@router.post("/temp-trans/{row_id}/classify")
async def classify_row(
    row_id: int,
    head_id: int = Body(None, description="head_master.id"),
    rera_head_id: int = Body(None, description="rera_head_master.id"),
    idw_head_id: int = Body(None, description="idw_head_master.id"),
    project_id: int = Body(None, description="projects.id"),
    beneficiary_id: int = Body(None, description="beneficiary_master.id"),
    user: dict = Depends(get_company_user),
):
    """
    Tag a raw row with a head (category) before finalizing.
    At least one of head_id, rera_head_id, idw_head_id must be provided.
    project_id and beneficiary_id are optional and carried into the ledger.

    Every id is checked against this company's master tables first, so a value
    can only come from a row someone actually created in Master Data.

    Body(...), not bare defaults. A scalar parameter with a plain default is a
    *query* parameter to FastAPI, so the JSON the frontend was posting never
    reached the handler and every classify attempt failed on "Provide at least
    one of: head_id, rera_head_id, idw_head_id". With several Body params
    FastAPI embeds them into one object, which is the shape already being sent.

    Two scope checks, not one. The row has to be visible to this user, and the
    project they are filing it under has to be one of theirs — otherwise
    classifying would be a way to push rows into a project you cannot see, or
    to move a row out of your own scope and lose it.
    """
    if not any([head_id, rera_head_id, idw_head_id]):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one of: head_id, rera_head_id, idw_head_id",
        )

    sets: list[str] = []
    params: list = []

    def _set(column: str, value) -> None:
        """Add `column = $n` and its value, numbering from the params list.

        Numbering off len(params) rather than a counter kept alongside it: the
        mirror columns below are appended from inside the connection, after this
        list was first built, and a separate index would have to be threaded
        through that to stay in step.
        """
        params.append(value)
        sets.append(f"{column} = ${len(params)}")

    if head_id is not None:
        _set("head_id", head_id)
    if rera_head_id is not None:
        _set("rera_head_id", rera_head_id)
    if idw_head_id is not None:
        _set("idw_head_id", idw_head_id)
    if project_id is not None:
        _set("project_id", project_id)
    if beneficiary_id is not None:
        _set("beneficiary_id", beneficiary_id)

    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)

        if not scoping.can_use_project(scope, project_id):
            raise HTTPException(
                status_code=403,
                detail="You are not assigned to that project.",
            )

        await _assert_live_master_ids(conn, {
            "head_id": head_id,
            "rera_head_id": rera_head_id,
            "idw_head_id": idw_head_id,
            "beneficiary_id": beneficiary_id,
            "project_id": project_id,
        })

        current = await conn.fetchrow(
            "SELECT project_id, is_locked FROM temp_trans WHERE id = $1", row_id
        )
        if current is None or not scoping.can_use_project(scope, current["project_id"]):
            raise HTTPException(status_code=404, detail="Row not found.")
        # The UI no longer calls this route, but it still writes the row, so it
        # honours the lock like the edit it predates.
        if current["is_locked"]:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This row is locked. Unlock it first to edit it.",
            )

        # Write the chosen name into whichever display column mirrors it, so the
        # staging table shows the classification instead of an em dash. The _id
        # columns above are still the real record — finalize reads those, not
        # these — and a company with no mirroring column gets nothing extra.
        #
        # All five, not the three heads. Picking a project set project_id and
        # left BUSINESS UNIT — the column that means Project — blank, so from
        # the table the Project dropdown appeared to do nothing at all.
        for column, name in (await _mirror_values(conn, {
            "head": head_id,
            "rera_head": rera_head_id,
            "idw_head": idw_head_id,
            "project": project_id,
            "beneficiary": beneficiary_id,
        })).items():
            _set(column, name)

        sets.append("is_classified = true")
        params.append(row_id)

        row = await conn.fetchrow(
            f"""
            UPDATE temp_trans
            SET {", ".join(sets)}
            WHERE id = ${len(params)} AND is_classified = false
            RETURNING id
            """,
            *params,
        )

    if row is None:
        raise HTTPException(
            status_code=400,
            detail="Row not found or already classified.",
        )

    return {"status": "classified", "row_id": row_id}


@router.post("/temp-trans/{row_id}/finalize")
async def finalize_row(
    row_id: int,
    user: dict = Depends(get_company_user),
):
    """
    Move a classified row from temp_trans into the transactions ledger.

    This is the point of no return — after this, the transaction exists in
    the real ledger. The UNIQUE (temp_trans_id) constraint on transactions
    means clicking this twice gives an error, not a double-post.
    """
    async with company_connection(user["schema"]) as conn:
        scope = await scoping.visible_project_ids(conn, user)

        # First, grab the raw row and its linked data.
        raw = await conn.fetchrow(
            """
            SELECT t.batch_id, t.head_id, t.rera_head_id, t.idw_head_id,
                   t.project_id
            FROM temp_trans t
            WHERE t.id = $1 AND t.is_classified = true
            """,
            row_id,
        )

        if raw is None:
            raise HTTPException(
                status_code=400,
                detail="Row not found or not classified. Classify it first.",
            )

        if not scoping.can_use_project(scope, raw["project_id"]):
            raise HTTPException(status_code=404, detail="Row not found.")

        # The data columns are read from the live tables and carried across
        # one-for-one. temp_trans and transactions are kept to the same set of
        # them (migration 007, and every custom-field create/delete alters
        # both), so this copies whatever the company has configured today
        # instead of the five columns that happened to exist when it was
        # written. Naming them is what broke finalize when txn_date,
        # description and balance were deleted as fields.
        # hide_redundant=False: amount and credit_debit are hidden from the
        # staging and ledger views when the bank's own debit/credit columns are
        # present, but they are still real, still NOT NULL on transactions, and
        # still what the ledger totals on. Copying is not displaying.
        carried = [
            c["name"] for c in await custom_fields.data_columns(conn, hide_redundant=False)
        ]
        cols = ", ".join(carried)
        src = ", ".join(f"t.{c}" for c in carried)

        # UNIQUE (temp_trans_id) on transactions means a second call here
        # raises a Postgres error — double-click is safe.
        try:
            txn = await conn.fetchrow(
                f"""
                INSERT INTO transactions (
                    {cols},
                    project_id, bank_id, beneficiary_id, head_id, rera_head_id,
                    idw_head_id, temp_trans_id
                )
                SELECT
                    {src},
                    t.project_id,
                    (SELECT bank_id FROM import_batches WHERE id = t.batch_id),
                    t.beneficiary_id, t.head_id, t.rera_head_id, t.idw_head_id,
                    t.id
                FROM temp_trans t
                WHERE t.id = $1
                RETURNING id
                """,
                row_id,
            )
        except Exception as e:
            # UNIQUE violation = already finalized.
            if "unique" in str(e).lower():
                raise HTTPException(
                    status_code=400,
                    detail="This row is already finalized.",
                )
            raise

    # Only the id is echoed back: which data columns exist is the company's
    # choice, so there is no fixed set of them to report here. The caller
    # reloads the ledger, which describes its own columns.
    return {"status": "finalized", "transaction_id": txn["id"]}

