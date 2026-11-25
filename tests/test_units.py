"""Unit tests for the pure pieces: mapping, money, OData filter parsing, error classification."""

import pytest

from syncwatch.errors import SyncError, classify_http
from syncwatch.mock_crm import parse_filter
from syncwatch.mock_finance import validate_invoice
from syncwatch.sync import to_invoice, to_minor_units


def opp(**overrides):
    base = {
        "opportunityid": "abc",
        "name": "Deal",
        "actualvalue": 100.0,
        "actualclosedate": "2026-11-01",
        "transactioncurrencyid": {"isocurrencycode": "GBP"},
        "parentaccountid": {"accountnumber": "ACC-1", "name": "Acme", "emailaddress1": "a@b.example"},
    }
    return base | overrides


@pytest.mark.parametrize(
    "amount, expected",
    [(1234.565, 123457), (0.1 + 0.2, 30), (19.99, 1999), (1500, 150000), ("10.005", 1001)],
)
def test_minor_units_never_lose_pennies(amount, expected):
    # int(1234.565 * 100) == 123456 with floats - the classic bug this guards against.
    assert to_minor_units(amount) == expected


def test_to_invoice_maps_fields():
    payload = to_invoice(opp())
    assert payload["amount_minor"] == 10000
    assert payload["currency"] == "GBP"
    assert payload["customer"] == {"number": "ACC-1", "name": "Acme", "email": "a@b.example"}
    assert payload["source_reference"] == "abc"


@pytest.mark.parametrize(
    "overrides, category",
    [
        ({"actualvalue": None}, "MISSING_AMOUNT"),
        ({"transactioncurrencyid": {"isocurrencycode": None}}, "MISSING_CURRENCY"),
        ({"transactioncurrencyid": None}, "MISSING_CURRENCY"),
    ],
)
def test_to_invoice_rejects_bad_records(overrides, category):
    with pytest.raises(SyncError) as exc:
        to_invoice(opp(**overrides))
    assert exc.value.category == category


def test_to_invoice_detects_schema_drift():
    drifted = opp()
    drifted["actualvalue_base"] = drifted.pop("actualvalue")
    with pytest.raises(SyncError) as exc:
        to_invoice(drifted)
    assert exc.value.category == "SCHEMA_CHANGE"
    assert "actualvalue_base" in exc.value.detail["fields_received"]


def test_odata_filter_parser():
    where, params = parse_filter("statecode eq 1 and modifiedon gt 2026-01-01T00:00:00Z")
    assert where == "o.statecode = ? AND o.modifiedon > ?"
    assert params == [1, "2026-01-01T00:00:00Z"]


def test_odata_filter_rejects_unknown_fields_and_injection():
    with pytest.raises(ValueError, match="Could not find a property"):
        parse_filter("name eq 'x'")
    with pytest.raises(ValueError):
        parse_filter("statecode eq 1; DROP TABLE crm_accounts")


@pytest.mark.parametrize(
    "system, status, body, expected",
    [
        ("crm", 401, {"error": "invalid_client", "error_description": "AADSTS7000215"}, "AUTH_FAILED"),
        ("crm", 401, {"error": {"message": "The access token has expired"}}, "TOKEN_EXPIRED"),
        ("crm", 429, {}, "THROTTLED"),
        ("finance", 503, {}, "UPSTREAM_UNAVAILABLE"),
        ("finance", 422, {"errors": []}, "FINANCE_VALIDATION"),
        ("finance", 418, {}, "UNEXPECTED"),
    ],
)
def test_classify_http(system, status, body, expected):
    assert classify_http(system, status, body) == expected


def test_finance_validation_messages():
    errors = validate_invoice({"customer": {"number": "A", "name": "B"}, "amount_minor": 12.5, "currency": "JPY"})
    fields = {e["field"] for e in errors}
    assert {"customer.email", "amount_minor", "currency", "source_reference", "issued_on"} <= fields
