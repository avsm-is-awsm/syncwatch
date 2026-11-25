"""A small stand-in for the Dynamics 365 / Dataverse Web API.

It copies the parts of the real API that integrations trip over:
OAuth2 client-credentials tokens, OData $filter/$select/$top, server-driven
paging via @odata.nextLink, 429 throttling with Retry-After, and PATCH for
updates. It is deliberately not a full OData implementation.
"""

from __future__ import annotations

import re
import secrets
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, Response

from . import db, scenarios
from .config import settings

app = FastAPI(title="Mock CRM (Dataverse-style)", docs_url="/docs", openapi_url="/openapi.json")

API = "/api/data/v9.2"
PAGE_SIZE_DEFAULT = 25
PAGE_SIZE_MAX = 50

_tokens: dict[str, dict] = {}
_request_counter = {"n": 0}

# OData field -> SQL column. Only whitelisted fields can be filtered on.
FILTERABLE = {
    "statecode": "o.statecode",
    "modifiedon": "o.modifiedon",
    "actualclosedate": "o.actualclosedate",
    "_parentaccountid_value": "o.parentaccountid",
}
OPS = {"eq": "=", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?")
CLAUSE = re.compile(r"^\s*(\w+)\s+(eq|ne|gt|ge|lt|le)\s+(.+?)\s*$")


def odata_error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def parse_filter(expr: str | None) -> tuple[str, list]:
    """Translate a tiny OData $filter subset into a parameterised SQL WHERE clause.

    Supports `field op value` clauses joined with `and`. Values are numbers,
    'quoted strings' or bare ISO dates. Raises ValueError on anything else.
    """
    if not expr:
        return "1=1", []
    parts, params = [], []
    for clause in re.split(r"\s+and\s+", expr.strip(), flags=re.IGNORECASE):
        m = CLAUSE.match(clause)
        if not m:
            raise ValueError(f"Could not parse filter clause '{clause}'")
        field, op, raw = m.groups()
        if field not in FILTERABLE:
            raise ValueError(f"Could not find a property named '{field}' on type 'Microsoft.Dynamics.CRM.opportunity'")
        if raw.startswith("'") and raw.endswith("'"):
            value: object = raw[1:-1]
        elif re.fullmatch(r"-?\d+", raw):
            value = int(raw)
        elif raw.lower() == "null":
            parts.append(f"{FILTERABLE[field]} IS {'NOT ' if op == 'ne' else ''}NULL")
            continue
        elif ISO_DATE.fullmatch(raw):
            value = raw  # ISO timestamp, compared as text (ISO-8601 sorts correctly)
        else:
            raise ValueError(f"Unsupported literal '{raw}' in filter clause '{clause}'")
        parts.append(f"{FILTERABLE[field]} {OPS[op]} ?")
        params.append(value)
    return " AND ".join(parts), params


def _check_auth(request: Request) -> JSONResponse | None:
    header = request.headers.get("authorization", "")
    token = header.removeprefix("Bearer ").strip()
    info = _tokens.get(token)
    if not info:
        return odata_error(401, "0x80040220", "Bearer token is missing or invalid.")
    if info["expires_at"] < time.time() or info["calls_left"] <= 0:
        return odata_error(401, "0x80072182", "The access token has expired. Request a new token.")
    info["calls_left"] -= 1
    return None


def _maybe_throttle() -> JSONResponse | None:
    if not scenarios.is_on("crm_throttling"):
        return None
    _request_counter["n"] += 1
    if _request_counter["n"] % 2 == 1:
        return odata_error(
            429,
            "0x80072322",
            "Number of requests exceeded the limit of 6000 over time window of 300 seconds.",
            headers={"Retry-After": "1"},
        )
    return None


@app.post("/oauth2/token")
async def token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    if grant_type != "client_credentials":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    if client_secret != settings.crm_client_secret or scenarios.is_on("bad_credentials"):
        return JSONResponse(
            {
                "error": "invalid_client",
                "error_description": "AADSTS7000215: Invalid client secret provided. "
                "Ensure the secret being sent in the request is the client secret value, not the client secret ID.",
            },
            status_code=401,
        )
    access_token = secrets.token_hex(16)
    _tokens[access_token] = {
        "expires_at": time.time() + 3600,
        "calls_left": 2 if scenarios.is_on("token_expiry") else 10_000,
    }
    return {"token_type": "Bearer", "expires_in": 3600, "access_token": access_token}


def _opportunity_json(row: dict, drift: bool) -> dict:
    value_field = "actualvalue_base" if drift else "actualvalue"
    return {
        "opportunityid": row["opportunityid"],
        "name": row["name"],
        value_field: row["actualvalue"],
        "statecode": row["statecode"],
        "actualclosedate": row["actualclosedate"],
        "modifiedon": row["modifiedon"],
        "_parentaccountid_value": row["parentaccountid"],
        "transactioncurrencyid": {"isocurrencycode": row["isocurrencycode"]},
        "parentaccountid": {
            "accountid": row["parentaccountid"],
            "name": row["account_name"],
            "accountnumber": row["accountnumber"],
            "emailaddress1": row["emailaddress1"],
        },
    }


@app.get(f"{API}/opportunities")
async def list_opportunities(request: Request):
    if (err := _check_auth(request)) or (err := _maybe_throttle()):
        return err
    q = request.query_params
    try:
        where, params = parse_filter(q.get("$filter"))
        top = min(int(q.get("$top", PAGE_SIZE_DEFAULT)), PAGE_SIZE_MAX)
        skip = int(q.get("$skiptoken", 0))
    except ValueError as exc:
        return odata_error(400, "0x80060888", str(exc))

    sql = (
        "SELECT o.*, a.name AS account_name, a.accountnumber, a.emailaddress1 "
        "FROM crm_opportunities o JOIN crm_accounts a ON a.accountid = o.parentaccountid "
        f"WHERE {where} ORDER BY o.modifiedon, o.opportunityid LIMIT ? OFFSET ?"
    )
    page = db.rows(sql, (*params, top + 1, skip))
    has_more = len(page) > top
    drift = scenarios.is_on("schema_drift")
    body: dict = {
        "@odata.context": f"{request.base_url}api/data/v9.2/$metadata#opportunities",
        "value": [_opportunity_json(r, drift) for r in page[:top]],
    }
    if has_more:
        next_q = {k: v for k, v in q.items() if k != "$skiptoken"} | {"$skiptoken": str(skip + top)}
        body["@odata.nextLink"] = f"{str(request.url).split('?')[0]}?{urlencode(next_q)}"
    return body


@app.patch(f"{API}/opportunities({{opportunity_id}})")
async def patch_opportunity(opportunity_id: str, request: Request):
    if err := _check_auth(request):
        return err
    body = await request.json()
    allowed = {"actualvalue": "actualvalue", "isocurrencycode": "isocurrencycode", "name": "name"}
    updates = {allowed[k]: v for k, v in body.items() if k in allowed}
    if not updates:
        return odata_error(400, "0x80048d19", "No updatable properties in request body.")
    with db.tx() as conn:
        sets = ", ".join(f"{col} = ?" for col in updates)
        cur = conn.execute(
            f"UPDATE crm_opportunities SET {sets}, modifiedon = ? WHERE opportunityid = ?",
            (*updates.values(), db.utcnow(), opportunity_id),
        )
    if cur.rowcount == 0:
        return odata_error(404, "0x80040217", f"opportunity With Id = {opportunity_id} Does Not Exist")
    return Response(status_code=204)


@app.get(f"{API}/accounts")
async def list_accounts(request: Request):
    if err := _check_auth(request):
        return err
    return {"value": db.rows("SELECT accountid, name, accountnumber, emailaddress1, modifiedon FROM crm_accounts ORDER BY name")}


@app.patch(f"{API}/accounts({{account_id}})")
async def patch_account(account_id: str, request: Request):
    if err := _check_auth(request):
        return err
    body = await request.json()
    if "emailaddress1" not in body:
        return odata_error(400, "0x80048d19", "Only emailaddress1 can be updated on this mock.")
    with db.tx() as conn:
        cur = conn.execute(
            "UPDATE crm_accounts SET emailaddress1 = ?, modifiedon = ? WHERE accountid = ?",
            (body["emailaddress1"], db.utcnow(), account_id),
        )
    if cur.rowcount == 0:
        return odata_error(404, "0x80040217", f"account With Id = {account_id} Does Not Exist")
    return Response(status_code=204)
