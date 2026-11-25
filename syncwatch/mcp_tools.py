"""MCP server: lets an AI agent (Copilot Studio, Claude, VS Code...) ask
"is the sync healthy?", "what's broken and who should fix it?" or
"do the numbers match?" in plain language.

Served over Streamable HTTP at /mcp/ by the main app.
"""

from __future__ import annotations

from mcp.server import MCPServer

from . import service
from .errors import CATALOG, describe
from .reconcile import run_reconciliation
from .sync import SyncAlreadyRunning, run_sync

mcp = MCPServer(
    "syncwatch",
    title="SyncWatch",
    instructions=(
        "Tools for monitoring the CRM -> Finance invoice sync. Start with sync_health. "
        "Use list_open_issues to see which records are failing and who should fix them. "
        "Use reconcile to check that CRM won-deal totals match Finance invoice totals. "
        "Amounts ending in _minor are in pence/cents."
    ),
)


@mcp.tool()
def sync_health() -> dict:
    """Overall health: last run, success rate over the last 20 runs, and open issues by category."""
    s = service.stats()
    issues = service.open_issues()
    by_cat: dict[str, int] = {}
    for i in issues:
        by_cat[i["category"]] = by_cat.get(i["category"], 0) + 1
    return {
        "last_run": s.get("last_run"),
        "success_rate_last_20_percent": s.get("success_rate"),
        "runs_last_20": {k: s.get(k) for k in ("succeeded", "partial", "failed")},
        "invoices_created_total": s.get("invoices_total"),
        "open_issues_by_category": by_cat,
    }


@mcp.tool()
def list_open_issues(limit: int = 20) -> list[dict]:
    """Records still failing to sync, with the reason, how many runs they've failed, and who should act."""
    return [
        {
            "record_id": i["record_id"],
            "record": i["record_label"],
            "category": i["category"],
            "error": i["message"],
            "runs_failed": i["runs_failed"],
            "first_seen_run": i["first_seen_run"],
            "owner": i["explanation"]["owner"],
            "how_to_fix": i["explanation"]["how_to_fix"],
            "retry_will_help": i["explanation"]["retryable"],
        }
        for i in service.open_issues()[:limit]
    ]


@mcp.tool()
def explain_run(run_id: int) -> dict:
    """Explain one sync run: counts, and its errors grouped by category with plain-English fixes."""
    run = service.get_run(run_id)
    if not run:
        return {"error": f"Run {run_id} not found"}
    return {
        "run": {k: v for k, v in run.items() if k not in ("events", "error_groups")},
        "problems": [
            {
                "category": g["category"]["code"],
                "title": g["category"]["title"],
                "count": len(g["events"]),
                "how_to_fix": g["category"]["how_to_fix"],
                "examples": [e["message"] for e in g["events"][:3]],
            }
            for g in run["error_groups"]
        ],
    }


@mcp.tool()
async def reconcile() -> dict:
    """Compare CRM won deals with Finance invoices. Says whether the numbers match and lists any differences."""
    result = await run_reconciliation()
    return {
        "overall": result["overall"],
        "numbers_match": result["numbers_match"],
        "totals_by_currency": result["totals"],
        "checks": [{k: c[k] for k in ("title", "status", "count")} | {"sample": c["rows"][:5]} for c in result["checks"]],
    }


@mcp.tool()
async def trigger_sync() -> dict:
    """Start a sync run now and return its outcome."""
    try:
        run = await run_sync(trigger="mcp")
    except SyncAlreadyRunning:
        return {"error": "A sync run is already in progress. Try again in a few seconds."}
    return explain_run(run["id"])


@mcp.tool()
def explain_error_category(code: str) -> dict:
    """Look up an error category code (e.g. FINANCE_VALIDATION). Pass 'ALL' to list every category."""
    if code.upper() == "ALL":
        return {"categories": [describe(c) for c in CATALOG]}
    return describe(code.upper())
