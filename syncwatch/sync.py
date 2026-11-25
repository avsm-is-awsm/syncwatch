"""The sync workflow: won CRM opportunities -> Finance invoices.

    auth -> extract (paged) -> transform/validate each record -> load (idempotent) -> finish

Design choices worth calling out:
* A bad record never stops the run. It is logged with a category and skipped.
* A problem that affects *everything* (auth, schema change, outage) stops the
  run early with a single clear error instead of hundreds of identical ones.
* Re-running is always safe: the ledger skips records already invoiced and the
  Finance Idempotency-Key stops duplicates if the ledger write was lost.
* Money is converted to integer minor units with Decimal, never float maths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from decimal import ROUND_HALF_UP, Decimal

from . import alerts, db, scenarios
from .connectors import CrmClient, FinanceClient
from .errors import SyncError

log = logging.getLogger("syncwatch.sync")
_lock = asyncio.Lock()
OUTAGE_CIRCUIT_BREAKER = 3  # consecutive load failures before we stop hammering Finance


class SyncAlreadyRunning(Exception):
    pass


class RunRecorder:
    def __init__(self, trigger: str):
        with db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO sync_runs(started_at, trigger, status, active_scenarios) VALUES (?,?, 'running', ?)",
                (db.utcnow(), trigger, db.dumps(scenarios.active())),
            )
            self.run_id = cur.lastrowid

    def event(self, level: str, step: str, message: str, category: str | None = None,
              record_id: str | None = None, detail: dict | None = None, label: str | None = None) -> None:
        if detail and not record_id:
            record_id = detail.pop("record_id", None)
        elif detail:
            detail.pop("record_id", None)
        with db.tx() as conn:
            conn.execute(
                "INSERT INTO run_events(run_id, ts, level, step, record_id, record_label, category, message, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (self.run_id, db.utcnow(), level, step, record_id, label, category, message,
                 db.dumps(detail) if detail else None),
            )
        getattr(log, "error" if level == "error" else "info")("run=%s %s/%s %s %s", self.run_id, step, category or "-", record_id or "", message)

    def finish(self, status: str, counts: dict) -> dict:
        summary = {
            r["category"]: r["n"]
            for r in db.rows(
                "SELECT category, COUNT(*) AS n FROM run_events WHERE run_id = ? AND level = 'error' GROUP BY category",
                (self.run_id,),
            )
        }
        with db.tx() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status=?, records_seen=?, records_synced=?, records_skipped=?, "
                "records_failed=?, error_summary=? WHERE id=?",
                (db.utcnow(), status, counts["seen"], counts["synced"], counts["skipped"], counts["failed"],
                 db.dumps(summary), self.run_id),
            )
        run = db.one("SELECT * FROM sync_runs WHERE id = ?", (self.run_id,))
        run["error_summary"] = summary
        run["active_scenarios"] = json.loads(run["active_scenarios"] or "[]")
        return run


def to_minor_units(amount: float | int | str) -> int:
    """1234.565 -> 123457. Goes through str() so the float's repr is used, not its binary value."""
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


REQUIRED_FIELDS = ("opportunityid", "actualvalue", "transactioncurrencyid", "parentaccountid")


def check_shape(opp: dict) -> None:
    """Raise SCHEMA_CHANGE if a field the mapping relies on is absent (absent, not merely empty)."""
    missing = [f for f in REQUIRED_FIELDS if f not in opp]
    if missing:
        raise SyncError(
            "SCHEMA_CHANGE",
            f"Field(s) {', '.join(repr(m) for m in missing)} missing from the CRM response",
            {"missing": missing, "fields_received": sorted(opp.keys())},
        )


def to_invoice(opp: dict) -> dict:
    """Map one CRM opportunity to a Finance invoice payload, or raise SyncError."""
    oid = opp.get("opportunityid")
    check_shape(opp)
    if opp["actualvalue"] is None:
        raise SyncError("MISSING_AMOUNT", f"'{opp.get('name')}' is won but has no actual value", {"opportunityid": oid})
    currency = (opp.get("transactioncurrencyid") or {}).get("isocurrencycode")
    if not currency:
        raise SyncError("MISSING_CURRENCY", f"'{opp.get('name')}' is won but has no currency", {"opportunityid": oid})
    account = opp.get("parentaccountid") or {}
    return {
        "customer": {
            "number": account.get("accountnumber"),
            "name": account.get("name"),
            "email": account.get("emailaddress1"),
        },
        "source_reference": oid,
        "description": opp.get("name"),
        "amount_minor": to_minor_units(opp["actualvalue"]),
        "currency": currency,
        "issued_on": opp.get("actualclosedate") or db.utcnow()[:10],
    }


async def run_sync(trigger: str = "manual") -> dict:
    if _lock.locked():
        raise SyncAlreadyRunning("A sync run is already in progress")
    async with _lock:
        return await _run(trigger)


async def _run(trigger: str) -> dict:
    rec = RunRecorder(trigger)
    counts = {"seen": 0, "synced": 0, "skipped": 0, "failed": 0}
    crm = CrmClient(on_event=rec.event)
    fin = FinanceClient(on_event=rec.event)
    fatal: SyncError | None = None
    rec.event("info", "auth", f"Run started ({trigger}). Active scenarios: {', '.join(scenarios.active()) or 'none'}")

    try:
        await crm.authenticate()
        rec.event("info", "auth", "Signed in to CRM")

        already = {r["opportunity_id"] for r in db.rows("SELECT opportunity_id FROM sync_ledger")}
        consecutive_outages = 0

        async for opp in crm.won_opportunities():
            counts["seen"] += 1
            oid = opp.get("opportunityid")
            # Check the shape before skipping, so a renamed column is caught even
            # on days when every record is already invoiced.
            check_shape(opp)
            if oid in already:
                counts["skipped"] += 1
                continue

            try:
                payload = to_invoice(opp)
            except SyncError as exc:
                if exc.category == "SCHEMA_CHANGE":
                    raise  # mapping is broken for every record; stop with one clear error
                counts["failed"] += 1
                rec.event("error", "transform", exc.message, exc.category, oid, exc.detail, label=opp.get("name"))
                continue

            try:
                invoice, replayed = await fin.create_invoice(payload, idempotency_key=oid)
            except SyncError as exc:
                counts["failed"] += 1
                rec.event("error", "load", exc.message, exc.category, oid, exc.detail | {"payload": payload},
                          label=opp.get("name"))
                if exc.category == "UPSTREAM_UNAVAILABLE":
                    consecutive_outages += 1
                    if consecutive_outages >= OUTAGE_CIRCUIT_BREAKER:
                        raise SyncError(
                            "UPSTREAM_UNAVAILABLE",
                            f"Finance failed {consecutive_outages} records in a row; stopping early to avoid piling on",
                            {"last_error": exc.detail},
                        )
                continue

            consecutive_outages = 0
            with db.tx() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO sync_ledger VALUES (?,?,?,?,?,?)",
                    (oid, invoice["invoice_id"], invoice["amount_minor"], invoice["currency"], rec.run_id, db.utcnow()),
                )
            counts["synced"] += 1
            if replayed:
                rec.event("warning", "load", "Invoice already existed in Finance (idempotent replay); ledger repaired",
                          None, oid, {"invoice_id": invoice["invoice_id"]}, label=opp.get("name"))

        rec.event("info", "extract", f"Read {counts['seen']} won opportunities from CRM")
    except SyncError as exc:
        fatal = exc
        step = {"AUTH_FAILED": "auth", "SCHEMA_CHANGE": "transform", "THROTTLED": "extract"}.get(exc.category, "load")
        rec.event("error", step, f"Run stopped: {exc.message}", exc.category, None, exc.detail)
    except Exception as exc:  # anything we did not anticipate
        fatal = SyncError("UNEXPECTED", f"{type(exc).__name__}: {exc}")
        rec.event("error", "finish", f"Run crashed: {type(exc).__name__}: {exc}", "UNEXPECTED", None,
                  {"traceback": traceback.format_exc(limit=5)})
    finally:
        await crm.aclose()
        await fin.aclose()

    status = "failed" if fatal else "partial" if counts["failed"] else "succeeded"
    rec.event("info" if status == "succeeded" else "warning", "finish",
              f"Run {status}: {counts['synced']} synced, {counts['skipped']} already done, {counts['failed']} failed")
    run = rec.finish(status, counts)
    if status != "succeeded":
        await alerts.notify_run(run)
    return run
