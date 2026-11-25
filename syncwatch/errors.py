"""Error catalogue and classifier.

The goal is that whoever looks at a failed run sees *what kind* of problem it
is and *who needs to act*, not just a stack trace. Every failure the sync
records is tagged with one of these category codes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ErrorCategory:
    code: str
    title: str
    meaning: str
    likely_cause: str
    how_to_fix: str
    owner: str          # who usually has to act
    retryable: bool     # will simply re-running help?
    severity: str       # "high" stops the whole run, "medium" affects records, "low" recovered automatically


CATALOG: dict[str, ErrorCategory] = {
    c.code: c
    for c in [
        ErrorCategory(
            "AUTH_FAILED",
            "Could not sign in to the CRM",
            "The CRM's identity provider rejected our client credentials, so no data could be read.",
            "The app registration's client secret has expired or been rotated, or the app user was removed.",
            "Create a new client secret on the app registration, update CRM_CLIENT_SECRET, then re-run.",
            "Platform admin",
            False,
            "high",
        ),
        ErrorCategory(
            "TOKEN_EXPIRED",
            "Access token expired mid-run",
            "The CRM rejected a token that had worked moments earlier.",
            "Long-running run or short token lifetime. Expected now and then.",
            "Nothing to do if the run recovered: the connector fetches a new token and retries once.",
            "None (self-healing)",
            True,
            "low",
        ),
        ErrorCategory(
            "THROTTLED",
            "Rate limited by the API",
            "The API answered 429 Too Many Requests and asked us to wait.",
            "Too many calls in a short window (Dataverse service protection limits).",
            "Usually self-heals through Retry-After. If it persists, reduce page size or the run frequency.",
            "None (self-healing)",
            True,
            "low",
        ),
        ErrorCategory(
            "UPSTREAM_UNAVAILABLE",
            "Target system unavailable",
            "The API kept returning 5xx or timing out after every retry.",
            "Outage or maintenance on the other system.",
            "Check the vendor's status page. Records stay unsynced and will be picked up by the next run automatically.",
            "Wait / vendor",
            True,
            "high",
        ),
        ErrorCategory(
            "SCHEMA_CHANGE",
            "CRM data shape changed",
            "A field the mapping depends on is missing from the CRM response.",
            "A column was renamed or removed, or a security role no longer exposes it.",
            "Compare the latest response with the mapping in sync.py, then update the mapping or the CRM view. Re-running will not help.",
            "Integration developer",
            False,
            "high",
        ),
        ErrorCategory(
            "MISSING_AMOUNT",
            "Won deal has no amount",
            "The opportunity is marked as won but its actual value is empty, so no invoice can be raised.",
            "The sales rep closed the deal without filling in the actual revenue.",
            "Ask the deal owner to enter the actual value in the CRM. It will sync on the next run.",
            "Sales / CRM data owner",
            False,
            "medium",
        ),
        ErrorCategory(
            "MISSING_CURRENCY",
            "Won deal has no currency",
            "We cannot tell which currency to invoice in.",
            "Currency was cleared or never set on the opportunity.",
            "Set the currency on the opportunity in the CRM.",
            "Sales / CRM data owner",
            False,
            "medium",
        ),
        ErrorCategory(
            "FINANCE_VALIDATION",
            "Finance rejected the invoice",
            "The finance system refused the invoice because a required field was missing or invalid (details below).",
            "Usually incomplete customer data in the CRM, e.g. no billing email on the account.",
            "Fix the field named in the error on the CRM account or opportunity. Re-running without a data fix will fail again.",
            "Sales / CRM data owner",
            False,
            "medium",
        ),
        ErrorCategory(
            "UNEXPECTED",
            "Unexpected error",
            "Something failed that the integration does not have a specific rule for.",
            "A bug, or a new kind of failure.",
            "Open the run, check the raw detail, then add a classification rule in errors.py so it is easier to spot next time.",
            "Integration developer",
            False,
            "high",
        ),
    ]
}


def describe(code: str | None) -> dict:
    return asdict(CATALOG.get(code or "UNEXPECTED", CATALOG["UNEXPECTED"]))


class SyncError(Exception):
    """An error the sync understands, carrying a catalogue code and context."""

    def __init__(self, category: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.category = category
        self.message = message
        self.detail = detail or {}


def classify_http(system: str, status: int, body: object) -> str:
    """Map an HTTP failure to a catalogue code."""
    text = str(body).lower()
    if status == 401 and system == "crm" and ("invalid_client" in text or "aadsts" in text):
        return "AUTH_FAILED"
    if status == 401 and "expired" in text:
        return "TOKEN_EXPIRED"
    if status == 401:
        return "AUTH_FAILED"
    if status == 429:
        return "THROTTLED"
    if status >= 500 or status == 408:
        return "UPSTREAM_UNAVAILABLE"
    if status == 422 and system == "finance":
        return "FINANCE_VALIDATION"
    return "UNEXPECTED"
