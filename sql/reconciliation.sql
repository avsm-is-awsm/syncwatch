-- Reconciliation checks. Each block is loaded by name from Python (syncwatch/sqlfile.py).
-- Inputs are the staging tables landed from the two APIs:
--   stg_crm_won      : every won opportunity in the CRM, amount already in minor units
--   stg_fin_invoices : every invoice in Finance
-- plus the integration's own sync_ledger and run_events.

-- name: won_not_invoiced
-- Won in CRM but no invoice in Finance. Joined to the latest logged error so
-- each gap comes with its reason (or none, which means it was never attempted).
SELECT c.opportunity_id,
       c.name,
       c.account_number,
       c.amount_minor,
       c.currency,
       (SELECT e.category FROM run_events e
         WHERE e.record_id = c.opportunity_id AND e.level = 'error'
         ORDER BY e.id DESC LIMIT 1)                       AS last_error_category,
       (SELECT e.message FROM run_events e
         WHERE e.record_id = c.opportunity_id AND e.level = 'error'
         ORDER BY e.id DESC LIMIT 1)                       AS last_error
FROM stg_crm_won c
LEFT JOIN stg_fin_invoices f ON f.source_reference = c.opportunity_id
WHERE f.invoice_id IS NULL
ORDER BY c.name;

-- name: invoiced_not_won
-- An invoice exists but the CRM no longer says the deal is won
-- (deal reopened or lost after invoicing -> probably needs a credit note).
SELECT f.invoice_id, f.source_reference, f.customer_number, f.amount_minor, f.currency
FROM stg_fin_invoices f
LEFT JOIN stg_crm_won c ON c.opportunity_id = f.source_reference
WHERE c.opportunity_id IS NULL
ORDER BY f.invoice_id;

-- name: amount_mismatch
-- Both sides exist but the money differs (e.g. deal value edited after invoicing).
SELECT c.opportunity_id,
       c.name,
       c.currency                         AS crm_currency,
       f.currency                         AS invoice_currency,
       c.amount_minor                     AS crm_amount_minor,
       f.amount_minor                     AS invoiced_amount_minor,
       c.amount_minor - f.amount_minor    AS difference_minor,
       f.invoice_id
FROM stg_crm_won c
JOIN stg_fin_invoices f ON f.source_reference = c.opportunity_id
WHERE c.amount_minor IS NOT f.amount_minor
   OR c.currency IS NOT f.currency
ORDER BY ABS(c.amount_minor - f.amount_minor) DESC;

-- name: duplicate_invoices
-- More than one invoice for the same opportunity. Idempotency keys should make this impossible.
SELECT source_reference,
       COUNT(*)                  AS invoice_count,
       GROUP_CONCAT(invoice_id)  AS invoice_ids,
       SUM(amount_minor)         AS total_minor
FROM stg_fin_invoices
GROUP BY source_reference
HAVING COUNT(*) > 1;

-- name: ledger_drift
-- Our ledger says we created an invoice that Finance does not have.
SELECT l.opportunity_id, l.invoice_id, l.amount_minor, l.currency, l.run_id
FROM sync_ledger l
LEFT JOIN stg_fin_invoices f ON f.invoice_id = l.invoice_id
WHERE f.invoice_id IS NULL;

-- name: totals_by_currency
-- Do the numbers match? CRM won total vs Finance invoiced total per currency.
-- The gap should be fully explained by deals that are known not to have synced yet;
-- anything left over is "unexplained" and needs investigating.
WITH crm AS (
    SELECT currency, SUM(amount_minor) AS total, COUNT(*) AS n
    FROM stg_crm_won WHERE currency IS NOT NULL GROUP BY currency
),
fin AS (
    SELECT currency, SUM(amount_minor) AS total, COUNT(*) AS n
    FROM stg_fin_invoices GROUP BY currency
),
unsynced AS (
    SELECT c.currency, SUM(c.amount_minor) AS total
    FROM stg_crm_won c
    LEFT JOIN stg_fin_invoices f ON f.source_reference = c.opportunity_id
    WHERE f.invoice_id IS NULL AND c.currency IS NOT NULL
    GROUP BY c.currency
),
currencies AS (SELECT currency FROM crm UNION SELECT currency FROM fin)
SELECT cur.currency,
       COALESCE(crm.n, 0)                                   AS crm_won_count,
       COALESCE(crm.total, 0)                               AS crm_won_total_minor,
       COALESCE(fin.n, 0)                                   AS invoice_count,
       COALESCE(fin.total, 0)                               AS invoiced_total_minor,
       COALESCE(crm.total, 0) - COALESCE(fin.total, 0)      AS gap_minor,
       COALESCE(unsynced.total, 0)                          AS explained_by_unsynced_minor,
       COALESCE(crm.total, 0) - COALESCE(fin.total, 0)
         - COALESCE(unsynced.total, 0)                      AS unexplained_minor
FROM currencies cur
LEFT JOIN crm      USING (currency)
LEFT JOIN fin      USING (currency)
LEFT JOIN unsynced USING (currency)
ORDER BY cur.currency;
