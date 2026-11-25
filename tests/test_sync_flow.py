"""End-to-end tests through the HTTP API, including every fault scenario."""

from syncwatch import db


def run(client):
    r = client.post("/api/sync/run")
    assert r.status_code == 200, r.text
    return r.json()


def events(client, run_id):
    return client.get(f"/api/runs/{run_id}").json()["events"]


def test_clean_run_syncs_good_records_and_isolates_bad_ones(client):
    r = run(client)
    assert r["status"] == "partial"
    assert r["records_seen"] == 65
    assert r["records_synced"] == 61
    assert r["records_failed"] == 4
    assert r["error_summary"] == {"MISSING_AMOUNT": 2, "MISSING_CURRENCY": 1, "FINANCE_VALIDATION": 1}


def test_rerun_is_idempotent(client):
    run(client)
    second = run(client)
    assert second["records_synced"] == 0
    assert second["records_skipped"] == 61
    dupes = db.rows("SELECT source_reference FROM fin_invoices GROUP BY source_reference HAVING COUNT(*) > 1")
    assert dupes == []


def test_lost_ledger_write_is_healed_by_idempotency_key(client):
    run(client)
    # Simulate the integration crashing after Finance created the invoice but before we recorded it.
    db.rows("DELETE FROM sync_ledger WHERE rowid IN (SELECT rowid FROM sync_ledger LIMIT 3)")
    r = run(client)
    assert r["records_synced"] == 3
    assert db.one("SELECT COUNT(*) AS n FROM fin_invoices")["n"] == 61  # no new invoices
    assert any("idempotent replay" in e["message"] for e in events(client, r["id"]))


def test_open_issues_track_repeat_failures(client):
    run(client)
    run(client)
    issues = client.get("/api/health/summary").json()["open_issues"]
    assert len(issues) == 4
    assert all(i["runs_failed"] == 2 for i in issues)
    assert {i["explanation"]["owner"] for i in issues} == {"Sales / CRM data owner"}


def test_throttling_and_token_expiry_recover(client, scenario):
    scenario("crm_throttling")
    scenario("token_expiry")
    r = run(client)
    assert r["records_synced"] == 61
    warnings = {e["category"] for e in events(client, r["id"]) if e["level"] == "warning"}
    assert {"THROTTLED", "TOKEN_EXPIRED"} <= warnings


def test_finance_outage_trips_circuit_breaker(client, scenario):
    scenario("finance_outage")
    r = run(client)
    assert r["status"] == "failed"
    assert r["records_synced"] == 0
    assert r["records_failed"] == 3  # stops after 3 consecutive outages, not 61
    assert "UPSTREAM_UNAVAILABLE" in r["error_summary"]


def test_schema_drift_stops_with_one_clear_error(client, scenario):
    scenario("schema_drift")
    r = run(client)
    assert r["status"] == "failed"
    assert r["error_summary"] == {"SCHEMA_CHANGE": 1}


def test_schema_drift_caught_even_when_nothing_new_to_sync(client, scenario):
    run(client)
    client.post("/api/demo/repair-data")
    assert run(client)["status"] == "succeeded"  # everything invoiced now
    scenario("schema_drift")
    r = run(client)
    assert r["status"] == "failed"
    assert r["records_seen"] == 1  # stopped on the very first record
    assert r["error_summary"] == {"SCHEMA_CHANGE": 1}


def test_bad_credentials_fail_fast(client, scenario):
    scenario("bad_credentials")
    r = run(client)
    assert r["status"] == "failed"
    assert r["records_seen"] == 0
    assert r["error_summary"] == {"AUTH_FAILED": 1}


def test_repair_then_rerun_succeeds_and_reconciles(client):
    run(client)
    fixes = client.post("/api/demo/repair-data").json()["fixes"]
    assert len(fixes) == 4
    r = run(client)
    assert r["status"] == "succeeded"
    assert r["records_synced"] == 4
    rec = client.post("/api/reconcile").json()
    assert rec["overall"] == "pass"
    assert rec["numbers_match"] is True
    assert client.get("/api/health/summary").json()["open_issues"] == []


def test_reconciliation_explains_gap_from_failed_records(client):
    run(client)
    rec = client.post("/api/reconcile").json()
    assert rec["overall"] == "explained"
    assert rec["numbers_match"] is True
    gap = {c["name"]: c for c in rec["checks"]}["won_not_invoiced"]
    assert gap["count"] == 4
    assert all(row["last_error_category"] for row in gap["rows"])


def test_reconciliation_catches_post_invoice_edit(client):
    run(client)
    client.post("/api/demo/edit-invoiced-deal")
    rec = client.post("/api/reconcile").json()
    assert rec["overall"] == "fail"
    assert rec["numbers_match"] is False
    mismatch = {c["name"]: c for c in rec["checks"]}["amount_mismatch"]
    assert mismatch["count"] == 1
    assert mismatch["rows"][0]["difference_minor"] == 25_000


def test_pages_render(client):
    run(client)
    for path in ["/", "/runs/1", "/reconciliation", "/scenarios", "/errors", "/connect"]:
        assert client.get(path).status_code == 200, path


def test_mcp_endpoint_lists_and_calls_tools(client):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "1"}},
    }
    assert client.post("/mcp/", json=init, headers=headers).status_code == 200
    listed = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=headers).json()
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"sync_health", "list_open_issues", "reconcile", "trigger_sync", "explain_run"} <= names

    called = client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "trigger_sync", "arguments": {}}},
        headers=headers,
    ).json()
    assert called["result"]["isError"] is False
