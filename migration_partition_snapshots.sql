-- Optimization #2 — Partition + cluster the snapshot history tables
--
-- WHAT THIS DOES
--   Today, history.event_snapshot and history.event_daily are unpartitioned.
--   Every UI request that needs "latest snapshot per event" runs a
--   QUALIFY ROW_NUMBER() that scans the entire history table (small today,
--   unbounded over months/years of daily snapshots × every event).
--
--   After this migration, both tables are PARTITIONed by date and CLUSTERed
--   by event_id. Existing reads still work unchanged. The cost win only kicks
--   in once server queries add a date filter (see "FOLLOW-UP" at the bottom).
--
-- HOW TO RUN
--   1. Open https://console.cloud.google.com/bigquery (project: masterclass-automation-ik)
--   2. Pause the Cloud Scheduler job that fires /run-snapshot
--      (so a snapshot doesn't write to the old table mid-migration).
--   3. Run Step 1 (backup). Verify it created backup tables.
--   4. Run Step 2 (recreate event_snapshot).
--   5. Run Step 3 (recreate event_daily).
--   6. Run Step 4 (verify row counts match the backup).
--   7. Re-enable the Cloud Scheduler job.
--
--   If anything goes wrong before Step 4, restore from the backup tables:
--     CREATE OR REPLACE TABLE `masterclass-automation-ik.history.event_snapshot`
--     AS SELECT * FROM `masterclass-automation-ik.history.event_snapshot_backup_YYYYMMDD`;
--
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- Step 1 — Backup (~30s each, free or near-free)

CREATE TABLE `masterclass-automation-ik.history.event_snapshot_backup_20260621`
AS SELECT * FROM `masterclass-automation-ik.history.event_snapshot`;

CREATE TABLE `masterclass-automation-ik.history.event_daily_backup_20260621`
AS SELECT * FROM `masterclass-automation-ik.history.event_daily`;

-- Step 2 — Recreate event_snapshot partitioned by DATE(snapshot_at), clustered by event_id

CREATE OR REPLACE TABLE `masterclass-automation-ik.history.event_snapshot`
PARTITION BY DATE(snapshot_at)
CLUSTER BY event_id
AS SELECT * FROM `masterclass-automation-ik.history.event_snapshot_backup_20260621`;

-- Step 3 — Recreate event_daily partitioned by registration_date, clustered by event_id

CREATE OR REPLACE TABLE `masterclass-automation-ik.history.event_daily`
PARTITION BY registration_date
CLUSTER BY event_id
AS SELECT * FROM `masterclass-automation-ik.history.event_daily_backup_20260621`;

-- Step 4 — Verify row counts match

SELECT
  (SELECT COUNT(*) FROM `masterclass-automation-ik.history.event_snapshot`)        AS new_snapshot_rows,
  (SELECT COUNT(*) FROM `masterclass-automation-ik.history.event_snapshot_backup_20260621`) AS backup_snapshot_rows,
  (SELECT COUNT(*) FROM `masterclass-automation-ik.history.event_daily`)           AS new_daily_rows,
  (SELECT COUNT(*) FROM `masterclass-automation-ik.history.event_daily_backup_20260621`)    AS backup_daily_rows;

-- The two pairs of counts MUST be equal. If not, restore from backup.

-- Step 5 — Once verified, delete backups to reclaim storage (optional, after a week):
-- DROP TABLE `masterclass-automation-ik.history.event_snapshot_backup_20260621`;
-- DROP TABLE `masterclass-automation-ik.history.event_daily_backup_20260621`;

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
--
-- FOLLOW-UP — to actually get the cost win
--
-- Partitioning alone doesn't prune anything; queries also need a date filter.
-- Once the migration above is done and stable, update server.py:
--
--   server.py:303-308  (_list_events `latest_snap` CTE)
--   server.py:413-417  (_list_months `latest` CTE)
--   server.py:510-514  (_get_event snap_fut subquery)
--
-- In each, add a WHERE on the partition column BEFORE the QUALIFY. Example:
--
--   WITH latest_snap AS (
--     SELECT *
--     FROM `masterclass-automation-ik.history.event_snapshot`
--     WHERE DATE(snapshot_at) >= CURRENT_DATE() - 30   -- ← add this
--     QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1
--   )
--
-- 30 days is a safe default — any event that hasn't been snapshotted in the
-- last 30 days is dormant. Bump to 90 if you want to show longer-tail events.
--
-- Expected cost reduction after both migration + query change: 10–30× fewer
-- bytes scanned on every event-list / event-detail / months page load.
