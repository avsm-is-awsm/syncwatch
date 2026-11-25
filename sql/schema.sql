-- SyncWatch schema (SQLite).
-- Three groups of tables:
--   crm_* / fin_*  : the *mock* source and target systems (stand-ins for Dynamics 365 and a finance app)
--   sync_* / run_* : the integration's own run log and ledger
--   stg_*          : staging copies landed from both APIs for reconciliation

PRAGMA foreign_keys = ON;

-- ---------- Mock CRM (Dynamics-style) ----------
CREATE TABLE IF NOT EXISTS crm_accounts (
    accountid      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    accountnumber  TEXT NOT NULL UNIQUE,
    emailaddress1  TEXT,
    modifiedon     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crm_opportunities (
    opportunityid     TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    parentaccountid   TEXT NOT NULL REFERENCES crm_accounts(accountid),
    actualvalue       REAL,               -- NULL allowed on purpose: real CRMs have gaps
    isocurrencycode   TEXT,
    statecode         INTEGER NOT NULL,   -- 0 = open, 1 = won, 2 = lost
    actualclosedate   TEXT,
    modifiedon        TEXT NOT NULL
);

-- ---------- Mock Finance ----------
CREATE TABLE IF NOT EXISTS fin_invoices (
    invoice_id        TEXT PRIMARY KEY,
    idempotency_key   TEXT UNIQUE,
    customer_number   TEXT NOT NULL,
    customer_name     TEXT NOT NULL,
    customer_email    TEXT NOT NULL,
    source_reference  TEXT NOT NULL,      -- CRM opportunity id
    amount_minor      INTEGER NOT NULL,   -- pence/cents, never floats
    currency          TEXT NOT NULL,
    issued_on         TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- ---------- Integration run log ----------
CREATE TABLE IF NOT EXISTS sync_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    trigger          TEXT NOT NULL,       -- manual | schedule | mcp | api
    status           TEXT NOT NULL,       -- running | succeeded | partial | failed
    records_seen     INTEGER NOT NULL DEFAULT 0,
    records_synced   INTEGER NOT NULL DEFAULT 0,
    records_skipped  INTEGER NOT NULL DEFAULT 0,  -- already invoiced earlier
    records_failed   INTEGER NOT NULL DEFAULT 0,
    active_scenarios TEXT,                -- JSON list of fault scenarios that were on
    error_summary    TEXT                 -- JSON {category: count}
);

CREATE TABLE IF NOT EXISTS run_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    ts         TEXT NOT NULL,
    level      TEXT NOT NULL,             -- info | warning | error
    step       TEXT NOT NULL,             -- auth | extract | transform | load | finish
    record_id  TEXT,                      -- CRM opportunity id, when the event is about one record
    record_label TEXT,                    -- human-readable name of that record
    category   TEXT,                      -- error catalogue code, see syncwatch/errors.py
    message    TEXT NOT NULL,
    detail     TEXT                       -- JSON blob: status code, response body, attempt count...
);
CREATE INDEX IF NOT EXISTS ix_run_events_run ON run_events(run_id);
CREATE INDEX IF NOT EXISTS ix_run_events_record ON run_events(record_id, category);

-- Ledger of what the integration believes it has invoiced.
CREATE TABLE IF NOT EXISTS sync_ledger (
    opportunity_id TEXT PRIMARY KEY,
    invoice_id     TEXT NOT NULL,
    amount_minor   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    run_id         INTEGER NOT NULL REFERENCES sync_runs(id),
    synced_at      TEXT NOT NULL
);

-- Fault-injection switches for the demo.
CREATE TABLE IF NOT EXISTS scenarios (
    name    TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0
);

-- ---------- Staging for reconciliation (landed via the APIs, not read directly) ----------
CREATE TABLE IF NOT EXISTS stg_crm_won (
    opportunity_id  TEXT PRIMARY KEY,
    name            TEXT,
    account_number  TEXT,
    amount_minor    INTEGER,              -- NULL when CRM has no value
    currency        TEXT,
    close_date      TEXT
);

CREATE TABLE IF NOT EXISTS stg_fin_invoices (
    invoice_id       TEXT PRIMARY KEY,
    source_reference TEXT,
    customer_number  TEXT,
    amount_minor     INTEGER,
    currency         TEXT
);
