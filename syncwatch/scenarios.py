"""Fault-injection switches. Each one makes a mock system misbehave in a
realistic way so the sync's error handling can be demonstrated live."""

from __future__ import annotations

from . import db

DESCRIPTIONS = {
    "crm_throttling": "CRM answers every other request with HTTP 429 + Retry-After (Dataverse service protection limits).",
    "token_expiry": "CRM access tokens expire after 2 calls, forcing a mid-run token refresh.",
    "finance_outage": "Finance API returns HTTP 503 for every request.",
    "schema_drift": "CRM renames the 'actualvalue' column to 'actualvalue_base' (an unannounced schema change).",
    "bad_credentials": "The CRM rejects our client secret (e.g. the secret expired in Entra ID).",
}


def is_on(name: str) -> bool:
    row = db.one("SELECT enabled FROM scenarios WHERE name = ?", (name,))
    return bool(row and row["enabled"])


def active() -> list[str]:
    return [r["name"] for r in db.rows("SELECT name FROM scenarios WHERE enabled = 1 ORDER BY name")]


def all_with_state() -> list[dict]:
    state = {r["name"]: bool(r["enabled"]) for r in db.rows("SELECT name, enabled FROM scenarios")}
    return [{"name": n, "enabled": state.get(n, False), "description": d} for n, d in DESCRIPTIONS.items()]


def set_state(name: str, enabled: bool) -> None:
    if name not in DESCRIPTIONS:
        raise KeyError(name)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO scenarios(name, enabled) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled",
            (name, int(enabled)),
        )
