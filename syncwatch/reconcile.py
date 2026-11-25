"""Reconciliation: land both systems' data through their APIs, then compare in SQL.

Same pattern as a lakehouse: extract -> stage -> query. We never read the
mock systems' tables directly, so this would work unchanged against the
real Dynamics and Finance APIs.
"""

from __future__ import annotations

from . import db, sqlfile
from .connectors import CrmClient, FinanceClient
from .sync import to_minor_units

CHECKS = [
    # name, title, what a non-empty result means
    ("amount_mismatch", "Invoice amount matches the CRM deal", "fail"),
    ("duplicate_invoices", "No duplicate invoices", "fail"),
    ("invoiced_not_won", "Every invoice belongs to a won deal", "fail"),
    ("ledger_drift", "Integration ledger agrees with Finance", "fail"),
    ("won_not_invoiced", "Every won deal is invoiced", "warn"),
]


async def land_staging() -> dict:
    crm, fin = CrmClient(), FinanceClient()
    try:
        opps = [o async for o in crm.won_opportunities(page_size=50)]
        invoices = await fin.all_invoices()
    finally:
        await crm.aclose()
        await fin.aclose()

    with db.tx() as conn:
        conn.execute("DELETE FROM stg_crm_won")
        conn.execute("DELETE FROM stg_fin_invoices")
        conn.executemany(
            "INSERT INTO stg_crm_won VALUES (?,?,?,?,?,?)",
            [
                (
                    o["opportunityid"],
                    o["name"],
                    (o.get("parentaccountid") or {}).get("accountnumber"),
                    None if o.get("actualvalue") is None else to_minor_units(o["actualvalue"]),
                    (o.get("transactioncurrencyid") or {}).get("isocurrencycode"),
                    o.get("actualclosedate"),
                )
                for o in opps
            ],
        )
        conn.executemany(
            "INSERT INTO stg_fin_invoices VALUES (?,?,?,?,?)",
            [(i["invoice_id"], i["source_reference"], i["customer"]["number"], i["amount_minor"], i["currency"]) for i in invoices],
        )
    return {"crm_won": len(opps), "finance_invoices": len(invoices)}


async def run_reconciliation() -> dict:
    landed = await land_staging()
    queries = sqlfile.load("reconciliation.sql")

    checks = []
    for name, title, severity in CHECKS:
        found = db.rows(queries[name])
        status = "pass"
        if found:
            status = severity
            # A gap where every record has a known, logged reason is an *explained* exception.
            if name == "won_not_invoiced" and all(r["last_error_category"] for r in found):
                status = "explained"
        checks.append({"name": name, "title": title, "status": status, "count": len(found), "rows": found[:50]})

    totals = db.rows(queries["totals_by_currency"])
    unexplained = sum(abs(t["unexplained_minor"]) for t in totals)
    hard_fail = any(c["status"] == "fail" for c in checks)
    statuses = {c["status"] for c in checks}
    if hard_fail or unexplained:
        overall = "fail"
    elif "warn" in statuses:
        overall = "warn"
    elif "explained" in statuses:
        overall = "explained"
    else:
        overall = "pass"
    return {
        "ran_at": db.utcnow(),
        "landed": landed,
        "overall": overall,
        "numbers_match": unexplained == 0,
        "checks": checks,
        "totals": totals,
    }
