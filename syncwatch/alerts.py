"""Failure alerts. Posts a short message to an incoming webhook (Teams
Workflows, Slack, Discord all accept a JSON {"text": ...} body) when a run
does not fully succeed. Without ALERT_WEBHOOK_URL it only logs."""

from __future__ import annotations

import json
import logging

import httpx

from .config import settings
from .errors import CATALOG

log = logging.getLogger("syncwatch.alerts")


def build_message(run: dict) -> str:
    summary = run.get("error_summary") or {}
    if isinstance(summary, str):
        summary = json.loads(summary)
    lines = [
        f"SyncWatch run #{run['id']} {run['status'].upper()}: "
        f"{run['records_synced']} synced, {run['records_failed']} failed.",
    ]
    for code, n in sorted(summary.items(), key=lambda kv: -kv[1]):
        cat = CATALOG.get(code)
        if cat:
            lines.append(f"- {n} x {cat.title} -> {cat.owner}: {cat.how_to_fix}")
    lines.append(f"Details: {settings.public_base_url}/runs/{run['id']}")
    return "\n".join(lines)


async def notify_run(run: dict) -> None:
    text = build_message(run)
    log.warning(text)
    if not settings.alert_webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(settings.alert_webhook_url, json={"text": text})
            resp.raise_for_status()
    except httpx.HTTPError as exc:  # an alert failing must never fail the run
        log.error("Alert webhook failed: %s", exc)
