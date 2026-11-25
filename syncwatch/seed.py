"""Deterministic demo data for the mock CRM.

The data is mostly clean, but a handful of records are deliberately broken
in the ways real CRM data usually is, so the sync has something to catch:

* a won deal with no amount
* a won deal with no currency
* a customer account with no billing email (Finance will reject it)
* an amount with a half-penny (catches float rounding bugs)
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone

from . import db

COMPANIES = [
    "Northwind Traders", "Contoso Retail", "Fabrikam Health", "Adventure Works", "Tailspin Toys",
    "Wingtip Solutions", "Litware Learning", "Proseware Labs", "Woodgrove Bank", "Alpine Ski House",
    "Coho Winery", "Fourth Coffee", "Lamna Healthcare", "Margie's Travel", "Relecloud",
    "Trey Research", "VanArsdel Ltd", "Wide World Importers", "Blue Yonder Airlines", "City Power & Light",
    "Graphic Design Institute", "Humongous Insurance", "Lucerne Publishing", "Munson's Pickles",
    "Nod Publishers", "Southridge Video", "School of Fine Art", "Datum Corporation", "Bellows College",
    "Harbour Logistics",
]
DEAL_TYPES = ["Annual licence", "Onboarding package", "Leadership programme", "Renewal", "Add-on seats", "Pilot"]
CURRENCIES = ["GBP", "GBP", "GBP", "EUR", "USD"]

SCENARIO_NAMES = ["crm_throttling", "token_expiry", "finance_outage", "schema_drift", "bad_credentials"]


def _guid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def seed(conn) -> None:
    rng = random.Random(20261125)
    base = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)

    accounts = []
    for i, company in enumerate(COMPANIES):
        acc_id = _guid(rng)
        slug = "".join(ch for ch in company.lower() if ch.isalnum())[:14]
        email = None if company == "Harbour Logistics" else f"accounts@{slug}.example"
        accounts.append(acc_id)
        conn.execute(
            "INSERT INTO crm_accounts VALUES (?,?,?,?,?)",
            (acc_id, company, f"ACC-{1001 + i}", email, base.isoformat()),
        )

    for n in range(90):
        acc_id = accounts[n % len(accounts)]
        state = 1 if n % 3 != 2 else rng.choice([0, 2])  # ~2/3 won
        amount = round(rng.uniform(1500, 48000), 2)
        currency = rng.choice(CURRENCIES)
        closed = base + timedelta(days=n // 2, hours=rng.randint(0, 8))
        conn.execute(
            "INSERT INTO crm_opportunities VALUES (?,?,?,?,?,?,?,?)",
            (
                _guid(rng),
                f"{rng.choice(DEAL_TYPES)} - {COMPANIES[n % len(COMPANIES)]}",
                acc_id,
                amount,
                currency,
                state,
                closed.date().isoformat() if state else None,
                closed.isoformat(),
            ),
        )

    # Deliberately dirty records (always won, so the sync picks them up).
    dirty = [
        ("Renewal - Trey Research (amount not entered)", accounts[15], None, "GBP"),
        ("Pilot - Relecloud (amount not entered)", accounts[14], None, "EUR"),
        ("Add-on seats - Coho Winery (no currency)", accounts[10], 4200.00, None),
        ("Annual licence - Harbour Logistics", accounts[29], 18750.00, "GBP"),
        ("Leadership programme - Fourth Coffee", accounts[11], 1234.565, "GBP"),  # half-penny
    ]
    for name, acc_id, amount, currency in dirty:
        conn.execute(
            "INSERT INTO crm_opportunities VALUES (?,?,?,?,?,?,?,?)",
            (_guid(rng), name, acc_id, amount, currency, 1, "2026-11-02", "2026-11-02T10:00:00+00:00"),
        )

    for name in SCENARIO_NAMES:
        conn.execute("INSERT OR IGNORE INTO scenarios(name, enabled) VALUES (?, 0)", (name,))


def reset() -> None:
    """Wipe everything and reseed. Used on first start and by the 'Reset demo' button."""
    db.init_schema()
    with db.tx() as conn:
        for table in [
            "run_events", "sync_ledger", "sync_runs", "fin_invoices", "crm_opportunities",
            "crm_accounts", "scenarios", "stg_crm_won", "stg_fin_invoices",
        ]:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('sync_runs','run_events')")
        seed(conn)


def ensure_seeded() -> None:
    db.init_schema()
    if not db.one("SELECT 1 AS x FROM crm_accounts LIMIT 1"):
        reset()
