"""HTTP connectors for the CRM and Finance APIs.

Both share one retry policy: exponential backoff for 429/5xx/timeouts, and
Retry-After is respected when the server sends it. Every retry is reported
through `on_event` so it appears in the run log instead of vanishing.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator, Awaitable, Callable

import httpx

from .config import settings
from .errors import SyncError, classify_http

EventFn = Callable[..., Awaitable[None] | None]
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}


def _client_for(base_url: str, internal_app, internal_host: str) -> httpx.AsyncClient:
    if base_url == "internal":
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=internal_app),
            base_url=f"http://{internal_host}",
            timeout=settings.http_timeout_seconds,
        )
    return httpx.AsyncClient(base_url=base_url, timeout=settings.http_timeout_seconds)


def _body(resp: httpx.Response) -> object:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


class BaseClient:
    system = "base"
    step = "extract"

    def __init__(self, client: httpx.AsyncClient, on_event: EventFn | None = None):
        self.http = client
        self.on_event = on_event

    async def _emit(self, level: str, category: str, message: str, detail: dict) -> None:
        if self.on_event:
            result = self.on_event(level=level, step=self.step, category=category, message=message, detail=detail)
            if asyncio.iscoroutine(result):
                await result

    async def send(self, method: str, url: str, record_id: str | None = None, **kwargs) -> httpx.Response:
        """Send with retries. Returns any non-retryable response; raises SyncError when retries run out."""
        last_detail: dict = {}
        for attempt in range(1, settings.max_attempts + 1):
            try:
                resp = await self.http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                last_detail = {"attempt": attempt, "error": type(exc).__name__, "url": url}
                category = "UPSTREAM_UNAVAILABLE"
            else:
                if resp.status_code not in RETRYABLE_STATUS:
                    return resp
                last_detail = {"attempt": attempt, "status": resp.status_code, "body": _body(resp), "url": url}
                category = classify_http(self.system, resp.status_code, last_detail["body"])

            if attempt == settings.max_attempts:
                break
            wait = settings.retry_base_seconds * (2 ** (attempt - 1))
            retry_after = resp.headers.get("retry-after") if "status" in last_detail else None
            if retry_after and retry_after.isdigit():
                wait = float(retry_after)
            wait = min(wait, settings.retry_max_wait_seconds)
            await self._emit(
                "warning",
                category,
                f"{self.system.upper()} {method} returned {last_detail.get('status', last_detail.get('error'))}; "
                f"retrying in {wait:.2f}s (attempt {attempt}/{settings.max_attempts})",
                last_detail | {"record_id": record_id},
            )
            await asyncio.sleep(wait)

        raise SyncError(
            "UPSTREAM_UNAVAILABLE" if last_detail.get("status") != 429 else "THROTTLED",
            f"{self.system.upper()} {method} {url.split('?')[0]} still failing after {settings.max_attempts} attempts",
            last_detail,
        )


class CrmClient(BaseClient):
    system = "crm"
    step = "extract"
    API = "/api/data/v9.2"

    def __init__(self, on_event: EventFn | None = None):
        from . import mock_crm

        super().__init__(_client_for(settings.crm_base_url, mock_crm.app, "crm.internal"), on_event)
        self._token: str | None = None

    async def authenticate(self) -> None:
        resp = await self.send(
            "POST",
            "/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.crm_client_id,
                "client_secret": settings.crm_client_secret,
            },
        )
        if resp.status_code != 200:
            body = _body(resp)
            raise SyncError(classify_http("crm", resp.status_code, body), "CRM token request was rejected", {"status": resp.status_code, "body": body})
        self._token = resp.json()["access_token"]

    async def call(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Authenticated call. On an expired token, refresh once and retry."""
        if not self._token:
            await self.authenticate()
        for refreshed in (False, True):
            resp = await self.send(method, url, headers={"Authorization": f"Bearer {self._token}"}, **kwargs)
            if resp.status_code == 401 and not refreshed:
                body = _body(resp)
                await self._emit("warning", classify_http("crm", 401, body), "CRM token rejected; requesting a new one", {"body": body})
                await self.authenticate()
                continue
            return resp
        return resp  # pragma: no cover

    async def won_opportunities(self, page_size: int = 25) -> AsyncIterator[dict]:
        url: str | None = f"{self.API}/opportunities?$filter=statecode eq 1&$top={page_size}"
        while url:
            resp = await self.call("GET", url)
            if resp.status_code != 200:
                body = _body(resp)
                raise SyncError(classify_http("crm", resp.status_code, body), "CRM query failed", {"status": resp.status_code, "body": body})
            data = resp.json()
            for item in data["value"]:
                yield item
            url = data.get("@odata.nextLink")

    async def patch(self, entity: str, entity_id: str, body: dict) -> None:
        resp = await self.call("PATCH", f"{self.API}/{entity}({entity_id})", json=body)
        if resp.status_code != 204:
            raise SyncError("UNEXPECTED", f"PATCH {entity} failed", {"status": resp.status_code, "body": _body(resp)})

    async def accounts(self) -> list[dict]:
        resp = await self.call("GET", f"{self.API}/accounts")
        return resp.json()["value"]

    async def aclose(self) -> None:
        await self.http.aclose()


class FinanceClient(BaseClient):
    system = "finance"
    step = "load"

    def __init__(self, on_event: EventFn | None = None):
        from . import mock_finance

        super().__init__(_client_for(settings.finance_base_url, mock_finance.app, "finance.internal"), on_event)
        self.headers = {"X-Api-Key": settings.finance_api_key}

    async def create_invoice(self, payload: dict, idempotency_key: str) -> tuple[dict, bool]:
        """Returns (invoice, replayed). Raises SyncError on rejection."""
        resp = await self.send(
            "POST",
            "/v1/invoices",
            record_id=idempotency_key,
            json=payload,
            headers=self.headers | {"Idempotency-Key": idempotency_key},
        )
        if resp.status_code in (200, 201):
            return resp.json(), resp.headers.get("idempotent-replayed") == "true"
        body = _body(resp)
        raise SyncError(
            classify_http("finance", resp.status_code, body),
            "Finance rejected the invoice: "
            + "; ".join(f"{e['field']} {e['message']}" for e in (body.get("errors", []) if isinstance(body, dict) else [])),
            {"status": resp.status_code, "body": body},
        )

    async def all_invoices(self) -> list[dict]:
        items, page = [], 1
        while page:
            resp = await self.send("GET", f"/v1/invoices?page={page}&page_size=100", headers=self.headers)
            if resp.status_code != 200:
                body = _body(resp)
                raise SyncError(classify_http("finance", resp.status_code, body), "Finance list failed", {"status": resp.status_code, "body": body})
            data = resp.json()
            items.extend(data["items"])
            page = data["next_page"]
        return items

    async def aclose(self) -> None:
        await self.http.aclose()
