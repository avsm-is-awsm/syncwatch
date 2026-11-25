# Investigation log

Real problems found while building and testing SyncWatch, written up the way
I'd hand them to a teammate: symptom → what I checked → root cause → fix → how
it's caught next time.

---

## 1. A renamed CRM column went unnoticed on quiet days

**Symptom.** With the `schema_drift` scenario on, run #4 reported
`seen 61, skipped 60`. It failed, but only because one record still needed
syncing. If every deal had already been invoiced, the run would have come back
`succeeded` even though the mapping was broken.

**What I checked.** I compared the run's timeline with the order of the code in
`sync.py`. The `SCHEMA_CHANGE` check lived inside `to_invoice()`, and that only
runs for records that *aren't* in the ledger yet. Already-synced records were
skipped before their shape was looked at.

**Root cause.** Validation was tied to the transform step rather than to reading
the data.

**Fix.** `check_shape()` now runs on every record straight after it's read,
before the skip. A renamed or removed column stops the run on the first record.

**Next time.** Test `test_schema_drift_caught_even_when_nothing_new_to_sync`.

---

## 2. The run API returned `error_summary` as a string

**Symptom.** A test asserting `error_summary == {"AUTH_FAILED": 1}` failed with
`'{"AUTH_FAILED":1}' == {...}`. The dashboard looked fine, because it decoded the
value itself.

**Root cause.** `POST /api/sync/run` returned the raw database row, where the
summary is stored as JSON text. `GET /api/runs/{id}` decoded it. The two
endpoints disagreed about the contract, so a Power Automate flow reading
`error_summary` would have received a string.

**Fix.** `RunRecorder.finish()` now returns the decoded structure. The alert
builder accepts either form.

**Next time.** The flow tests assert on `error_summary` as an object.

---

## 3. The OData `$filter` parser accepted garbage

**Symptom.** `statecode eq 1; DROP TABLE crm_accounts` was accepted. The bare
text after `eq` was treated as an ISO date string.

**Was it exploitable?** No. Values are always bound as SQL parameters, never
concatenated into the query. Accepting it silently is still wrong, though: a
typo in a filter would return zero rows instead of an error, which is worse to
debug.

**Fix.** A bare literal must now be an integer, a `'quoted string'`, `null`, or
match an ISO-8601 date pattern. Anything else returns `400` with a message that
names the bad clause.

**Next time.** `test_odata_filter_rejects_unknown_fields_and_injection`.

---

## 4. Patching records while paging through them (avoided by design)

**Risk.** The repair action reads won opportunities and patches the bad ones.
The CRM pages by `ORDER BY modifiedon`, and a PATCH updates `modifiedon`.
Patching during the loop would move a record from page 1 to the end, shifting
everything else back by one, so a record could be skipped.

**Decision.** Read all pages first, then patch. There's a comment in
`service.repair_crm_data()` explaining why.

---

## 5. Floats and money

**Risk.** `int(1234.565 * 100)` is `123456`, because the float is really
`1234.56499999…`. A naive conversion loses a penny, and reconciliation would
then show an "unexplained" 1p difference.

**Decision.** Convert through `Decimal(str(value))` with `ROUND_HALF_UP`, and
store integer minor units everywhere (Finance API, ledger, staging tables). A
seeded record with `1234.565` keeps this honest, and
`test_minor_units_never_lose_pennies` covers it.
