"""A small stand-in for a finance / invoicing system's REST API.

Behaviour worth noticing:
* API-key auth via the X-Api-Key header
* strict validation with a field-level 422 error body
* Idempotency-Key support, so a retried POST never creates a second invoice
* simple page/page_size listing
"""

from __future__ import annotations

import re
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, scenarios
from .config import settings

app = FastAPI(title="Mock Finance API", docs_url="/docs", openapi_url="/openapi.json")

SUPPORTED_CURRENCIES = {"GBP", "EUR", "USD"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _guard(request: Request) -> JSONResponse | None:
    if scenarios.is_on("finance_outage"):
        return JSONResponse({"message": "Service temporarily unavailable. Please retry later."}, status_code=503)
    if request.headers.get("x-api-key") != settings.finance_api_key:
        return JSONResponse({"message": "Invalid or missing API key."}, status_code=401)
    return None


def validate_invoice(body: dict) -> list[dict]:
    errors = []
    customer = body.get("customer") or {}
    if not customer.get("number"):
        errors.append({"field": "customer.number", "message": "is required"})
    if not customer.get("name"):
        errors.append({"field": "customer.name", "message": "is required"})
    if not customer.get("email"):
        errors.append({"field": "customer.email", "message": "is required"})
    elif not EMAIL.match(customer["email"]):
        errors.append({"field": "customer.email", "message": "is not a valid email address"})
    if not body.get("source_reference"):
        errors.append({"field": "source_reference", "message": "is required"})
    amount = body.get("amount_minor")
    if not isinstance(amount, int) or isinstance(amount, bool):
        errors.append({"field": "amount_minor", "message": "must be an integer number of minor units (e.g. pence)"})
    elif amount <= 0:
        errors.append({"field": "amount_minor", "message": "must be greater than zero"})
    if body.get("currency") not in SUPPORTED_CURRENCIES:
        errors.append({"field": "currency", "message": f"must be one of {sorted(SUPPORTED_CURRENCIES)}"})
    if not body.get("issued_on"):
        errors.append({"field": "issued_on", "message": "is required"})
    return errors


def _invoice_json(row: dict) -> dict:
    return {
        "invoice_id": row["invoice_id"],
        "customer": {"number": row["customer_number"], "name": row["customer_name"], "email": row["customer_email"]},
        "source_reference": row["source_reference"],
        "amount_minor": row["amount_minor"],
        "currency": row["currency"],
        "issued_on": row["issued_on"],
        "created_at": row["created_at"],
    }


@app.post("/v1/invoices")
async def create_invoice(request: Request):
    if err := _guard(request):
        return err
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"message": "Body must be JSON."}, status_code=400)

    key = request.headers.get("idempotency-key")
    if key:
        existing = db.one("SELECT * FROM fin_invoices WHERE idempotency_key = ?", (key,))
        if existing:
            return JSONResponse(_invoice_json(existing), status_code=200, headers={"Idempotent-Replayed": "true"})

    if errors := validate_invoice(body):
        return JSONResponse({"message": "Validation failed", "errors": errors}, status_code=422)

    row = {
        "invoice_id": f"INV-{uuid.uuid4().hex[:10].upper()}",
        "idempotency_key": key,
        "customer_number": body["customer"]["number"],
        "customer_name": body["customer"]["name"],
        "customer_email": body["customer"]["email"],
        "source_reference": body["source_reference"],
        "amount_minor": body["amount_minor"],
        "currency": body["currency"],
        "issued_on": body["issued_on"],
        "created_at": db.utcnow(),
    }
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO fin_invoices VALUES (:invoice_id,:idempotency_key,:customer_number,:customer_name,"
            ":customer_email,:source_reference,:amount_minor,:currency,:issued_on,:created_at)",
            row,
        )
    return JSONResponse(_invoice_json(row), status_code=201)


@app.get("/v1/invoices")
async def list_invoices(request: Request, page: int = 1, page_size: int = 50):
    if err := _guard(request):
        return err
    page_size = max(1, min(page_size, 100))
    total = db.one("SELECT COUNT(*) AS n FROM fin_invoices")["n"]
    items = db.rows(
        "SELECT * FROM fin_invoices ORDER BY created_at, invoice_id LIMIT ? OFFSET ?",
        (page_size, (page - 1) * page_size),
    )
    return {
        "items": [_invoice_json(r) for r in items],
        "page": page,
        "page_size": page_size,
        "total": total,
        "next_page": page + 1 if page * page_size < total else None,
    }
