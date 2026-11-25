-- Queries behind the dashboard and the MCP tools.

-- name: open_issues
-- Records whose most recent failure has not been resolved (they are still not in the ledger),
-- with how many separate runs they have failed in. A record failing run after run with a
-- data-quality category is a "fix the data" job, not a "press retry" job.
WITH record_errors AS (
    SELECT e.record_id, e.record_label, e.category, e.message, e.step, e.run_id,
           ROW_NUMBER() OVER (PARTITION BY e.record_id ORDER BY e.id DESC) AS rn
    FROM run_events e
    WHERE e.level = 'error' AND e.record_id IS NOT NULL
)
SELECT r.record_id,
       r.record_label,
       r.category,
       r.message,
       r.step,
       r.run_id                                                     AS last_seen_run,
       (SELECT COUNT(DISTINCT x.run_id) FROM run_events x
         WHERE x.record_id = r.record_id AND x.level = 'error')     AS runs_failed,
       (SELECT MIN(x.run_id) FROM run_events x
         WHERE x.record_id = r.record_id AND x.level = 'error')     AS first_seen_run
FROM record_errors r
WHERE r.rn = 1
  AND r.record_id NOT IN (SELECT opportunity_id FROM sync_ledger)
ORDER BY runs_failed DESC, r.category, r.record_id;

-- name: error_breakdown
-- Errors and warnings per category since :since.
SELECT category,
       SUM(level = 'error')    AS errors,
       SUM(level = 'warning')  AS warnings,
       COUNT(DISTINCT run_id)  AS runs_affected,
       MAX(ts)                 AS last_seen
FROM run_events
WHERE category IS NOT NULL AND ts >= :since
GROUP BY category
ORDER BY errors DESC, warnings DESC;

-- name: run_stats
-- Health over the last 20 completed runs.
SELECT COUNT(*)                         AS runs,
       COALESCE(SUM(status = 'succeeded'), 0) AS succeeded,
       COALESCE(SUM(status = 'partial'), 0)   AS partial,
       COALESCE(SUM(status = 'failed'), 0)    AS failed,
       COALESCE(SUM(records_synced), 0)       AS records_synced
FROM (SELECT * FROM sync_runs WHERE status <> 'running' ORDER BY id DESC LIMIT 20);
