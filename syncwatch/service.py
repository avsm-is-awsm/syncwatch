"""Read models for the dashboard/MCP, plus the demo data actions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import db, sqlfile
from .connectors import CrmClient
from .errors import describe


def _q(name: str) -> str:
    return sqlfile.load("insights.sql")[name]


def _decode(run: dict | None) -> dict | None:
    if run:
        run["error_summary"] = json.loads(run.get("error_summary") or "{}")
        run["active_scenarios"] = json.loads(run.get("active_scenarios") or "[]")
    return run


def recent_runs(limit: int = 20) -> list[dict]:
    return [_decode(r) for r in db.rows("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))]


def get_run(run_id: int) -> dict | None:
    run = _decode(db.one("SELECT * FROM sync_runs WHERE id = ?", (run_id,)))
    if not run:
        return None
    events = db.rows("SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,))
    for e in events:
        e["detail"] = json.loads(e["detail"]) if e["detail"] else None
    run["events"] = events
    # Group errors by category so 40 identical failures read as one problem.
    groups: dict[str, dict] = {}
    for e in events:
        if e["level"] == "error" and e["category"]:
            g = groups.setdefault(e["category"], {"category": describe(e["category"]), "events": []})
            g["events"].append(e)
    run["error_groups"] = sorted(groups.values(), key=lambda g: -len(g["events"]))
    return run


def stats() -> dict:
    s = db.one(_q("run_stats")) or {}
    completed = s.get("runs") or 0
    s["success_rate"] = round(100 * (s.get("succeeded") or 0) / completed) if completed else None
    s["last_run"] = _decode(db.one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"))
    s["invoices_total"] = db.one("SELECT COUNT(*) AS n FROM sync_ledger")["n"]
    return s


def open_issues() -> list[dict]:
    issues = db.rows(_q("open_issues"))
    for i in issues:
        i["explanation"] = describe(i["category"])
    return issues


def error_breakdown(days: int = 7) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    found = db.rows(_q("error_breakdown"), {"since": since})
    for f in found:
        f["explanation"] = describe(f["category"])
    return found


# ---------- demo data actions (done through the CRM API, like a real fix would be) ----------

async def repair_crm_data() -> list[str]:
    """Fix the deliberately bad seed records via CRM PATCH calls."""
    crm = CrmClient()
    fixes: list[str] = []
    try:
        # Read everything first, then patch. Patching while paging would change
        # `modifiedon`, which the paging order depends on, and records could be skipped.
        opps = [o async for o in crm.won_opportunities()]
        for o in opps:
            if "actualvalue" in o and o["actualvalue"] is None:
                await crm.patch("opportunities", o["opportunityid"], {"actualvalue": 5000.00})
                fixes.append(f"Set actual value 5,000.00 on '{o['name']}'")
            if not (o.get("transactioncurrencyid") or {}).get("isocurrencycode"):
                await crm.patch("opportunities", o["opportunityid"], {"isocurrencycode": "GBP"})
                fixes.append(f"Set currency GBP on '{o['name']}'")
        for a in await crm.accounts():
            if not a.get("emailaddress1"):
                await crm.patch("accounts", a["accountid"], {"emailaddress1": "billing@harbourlogistics.example"})
                fixes.append(f"Added billing email to account '{a['name']}'")
    finally:
        await crm.aclose()
    return fixes


async def edit_invoiced_deal() -> str:
    """Change the value of a deal that was already invoiced, to show reconciliation catching it."""
    row = db.one("SELECT opportunity_id, amount_minor FROM sync_ledger ORDER BY synced_at, opportunity_id LIMIT 1")
    if not row:
        raise LookupError("Nothing has been invoiced yet. Run a sync first.")
    crm = CrmClient()
    try:
        new_value = (row["amount_minor"] + 25_000) / 100
        await crm.patch("opportunities", row["opportunity_id"], {"actualvalue": new_value})
    finally:
        await crm.aclose()
    return f"Raised the CRM value of opportunity {row['opportunity_id'][:8]} by 250.00 after it was invoiced"
