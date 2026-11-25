"""SyncWatch web app: REST API, dashboard, MCP endpoint and the two mock systems."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import mock_crm, mock_finance, scenarios, seed, service
from .config import ROOT, settings
from .errors import CATALOG, SyncError, describe
from .mcp_tools import mcp
from .reconcile import run_reconciliation
from .sync import SyncAlreadyRunning, run_sync

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("syncwatch")

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",  # public deployment: don't restrict Host headers to localhost
)


async def _scheduler() -> None:
    while True:
        await asyncio.sleep(settings.sync_interval_minutes * 60)
        with contextlib.suppress(SyncAlreadyRunning):
            try:
                await run_sync(trigger="schedule")
            except Exception:  # never let the scheduler die
                log.exception("Scheduled sync crashed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    seed.ensure_seeded()
    task = asyncio.create_task(_scheduler()) if settings.sync_interval_minutes > 0 else None
    async with mcp.session_manager.run():
        yield
    if task:
        task.cancel()


app = FastAPI(
    title="SyncWatch",
    version="1.0.0",
    description="CRM -> Finance invoice sync with error triage, reconciliation and an MCP endpoint.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=ROOT / "syncwatch" / "static"), name="static")
app.mount("/mock/crm", mock_crm.app)
app.mount("/mock/finance", mock_finance.app)
app.mount("/mcp", mcp_app)

templates = Jinja2Templates(directory=ROOT / "syncwatch" / "templates")


def money(minor: int | None, currency: str | None = "") -> str:
    if minor is None:
        return "-"
    symbol = {"GBP": "£", "EUR": "€", "USD": "$"}.get(currency or "", "")
    return f"{'-' if minor < 0 else ''}{symbol}{abs(minor) / 100:,.2f}"


templates.env.filters["money"] = money
templates.env.filters["pretty"] = lambda v: json.dumps(v, indent=2, default=str)
templates.env.globals["describe"] = describe


# ---------------------------------------------------------------- API

class ScenarioUpdate(BaseModel):
    enabled: bool


@app.get("/health", tags=["ops"])
async def health():
    return {"status": "ok"}


@app.post("/api/sync/run", tags=["sync"], summary="Run the CRM -> Finance sync now")
async def api_run_sync():
    try:
        return await run_sync(trigger="api")
    except SyncAlreadyRunning as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/runs", tags=["sync"])
async def api_runs(limit: int = 20):
    return service.recent_runs(min(limit, 100))


@app.get("/api/runs/{run_id}", tags=["sync"])
async def api_run(run_id: int):
    run = service.get_run(run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return run


@app.get("/api/health/summary", tags=["sync"])
async def api_summary():
    return {"stats": service.stats(), "open_issues": service.open_issues(), "error_breakdown": service.error_breakdown()}


@app.get("/api/errors/catalog", tags=["errors"])
async def api_catalog():
    return [describe(c) for c in CATALOG]


@app.post("/api/reconcile", tags=["reconciliation"])
async def api_reconcile():
    try:
        return await run_reconciliation()
    except SyncError as exc:
        return JSONResponse({"error": exc.message, "category": describe(exc.category), "detail": exc.detail}, status_code=503)


@app.get("/api/scenarios", tags=["demo"])
async def api_scenarios():
    return scenarios.all_with_state()


@app.put("/api/scenarios/{name}", tags=["demo"])
async def api_set_scenario(name: str, body: ScenarioUpdate):
    try:
        scenarios.set_state(name, body.enabled)
    except KeyError:
        raise HTTPException(404, f"Unknown scenario '{name}'")
    return scenarios.all_with_state()


@app.post("/api/demo/repair-data", tags=["demo"], summary="Fix the bad CRM records through the CRM API")
async def api_repair():
    try:
        return {"fixes": await service.repair_crm_data()}
    except SyncError as exc:
        raise HTTPException(503, f"{exc.message} ({exc.category}) - turn off fault scenarios first")


@app.post("/api/demo/edit-invoiced-deal", tags=["demo"], summary="Change a deal after it was invoiced")
async def api_edit_deal():
    try:
        return {"message": await service.edit_invoiced_deal()}
    except LookupError as exc:
        raise HTTPException(400, str(exc))
    except SyncError as exc:
        raise HTTPException(503, f"{exc.message} ({exc.category}) - turn off fault scenarios first")


@app.post("/api/demo/reset", tags=["demo"], summary="Wipe all data and reseed the demo")
async def api_reset():
    seed.reset()
    return {"status": "reset"}


# ---------------------------------------------------------------- pages

def page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"active_scenarios": scenarios.active(), **ctx})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def overview(request: Request):
    return page(
        request,
        "overview.html",
        stats=service.stats(),
        runs=service.recent_runs(15),
        issues=service.open_issues(),
        breakdown=service.error_breakdown(),
        nav="overview",
    )


@app.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
async def run_page(request: Request, run_id: int):
    run = service.get_run(run_id)
    if not run:
        return RedirectResponse("/")
    return page(request, "run.html", run=run, nav="overview")


@app.get("/reconciliation", response_class=HTMLResponse, include_in_schema=False)
async def reconciliation_page(request: Request):
    result, error = None, None
    try:
        result = await run_reconciliation()
    except SyncError as exc:
        error = {"message": exc.message, "category": describe(exc.category)}
    return page(request, "reconciliation.html", result=result, error=error, nav="reconciliation")


@app.get("/scenarios", response_class=HTMLResponse, include_in_schema=False)
async def scenarios_page(request: Request):
    return page(request, "scenarios.html", scenarios=scenarios.all_with_state(), nav="scenarios")


@app.get("/errors", response_class=HTMLResponse, include_in_schema=False)
async def errors_page(request: Request):
    return page(request, "errors.html", catalog=[describe(c) for c in CATALOG], nav="errors")


@app.get("/connect", response_class=HTMLResponse, include_in_schema=False)
async def connect_page(request: Request):
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-proto") == "https":
        base = base.replace("http://", "https://", 1)
    tools = [{"name": t.name, "description": t.description} for t in await mcp.list_tools()]
    return page(request, "connect.html", base=base, tools=tools, nav="connect")
