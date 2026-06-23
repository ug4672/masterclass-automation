# Deferred optimization — Materialize qualified-leads CTE once per event

This was optimization **#3** in the top-5 list. It's the biggest BigQuery-cost saver (~60-70% scan reduction per India snapshot) but needs a more careful implementation than the others, so I've skipped it for now to avoid risk.

## What it would do

Today, for each India event, the snapshot cron runs **6 separate queries** that each independently rebuild the "qualified leads for this event" CTE — i.e., the same dedup-by-hubspot_id, filter by `dupe_logic=1`, `dupe_flag=0`, `gql_flag=0`, work-ex exclusions — against `ik-marketing-data.India_Leads.US_Domain_combined_view`:

| Function | What it returns | Re-dedups leads? |
|----------|-----------------|------------------|
| `_build_event_snapshot_india` lead+spend | Per-channel reg counts + spend | yes |
| `_query_cohort_india` | Role + work-ex buckets | yes |
| `_query_attendance_india` | Zoom attendance join | yes |
| `_query_calls_india` | Call lifecycle metrics | yes (in `base` CTE) |
| `_query_emails_india` | Email funnel | yes |
| `_query_sales_india` | Sales count + revenue | yes |

That's 6 full scans of the same wide table per event. With ~30 events on a backfill, that's 180 scans where 30 would do.

## Why I'm not doing it today

Two safe ways to consolidate, both have tradeoffs:

### Option A — BigQuery session + temp table

```python
from google.cloud import bigquery

src_client = bigquery.Client(project='ik-marketing-data')

# 1. Open a session
session_cfg = bigquery.QueryJobConfig(create_session=True)
create_temp = src_client.query("""
  CREATE TEMP TABLE qualified_leads AS
  SELECT hubspot_ID, role_domain, work_ex, channel, utm_campaign,
         formatted_date, event_start_date_time, lead_created_time,
         Sale_date, net_revenue, Channel AS sale_channel
  FROM (
    SELECT *, ROW_NUMBER() OVER (
      PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata")
      ORDER BY formatted_date ASC) AS rnk
    FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
    WHERE dupe_logic = 1
  )
  WHERE rnk = 1
    AND DATE(event_start_date_time, "Asia/Kolkata") IN UNNEST(@dates)
    AND webinar_type = @wt
    AND dupe_flag = 0 AND gql_flag = 0
    AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2','3-4')
""", job_config=session_cfg)
create_temp.result()
session_id = create_temp.session_info.session_id

# 2. Run each downstream query in the same session — they reference qualified_leads
conn_props = [bigquery.ConnectionProperty('session_id', session_id)]
job_cfg = bigquery.QueryJobConfig(connection_properties=conn_props,
                                  query_parameters=[bigquery.ScalarQueryParameter('wt', 'STRING', ev.webinar_type)])
# ... call _query_cohort_india with a rewritten SQL that selects FROM qualified_leads
```

**Why it's risky to ship today:**
- Every per-event query function has to be rewritten to read from `qualified_leads` instead of `US_Domain_combined_view`.
- BigQuery sessions have a 24h TTL and per-project concurrency limits; need to handle session-exhausted errors.
- The temp table doesn't survive across calls if the session dies; need to detect and rebuild.
- Should be tested with a few real events to confirm the rewritten queries produce identical numbers to today's.

### Option B — Materialized view in the source dataset

```sql
-- Run as ik-marketing-data admin
CREATE MATERIALIZED VIEW `ik-marketing-data.India_Leads.qualified_leads_per_event`
PARTITION BY web_scheduled_date
CLUSTER BY webinar_type, hubspot_id
AS
SELECT
  CAST(hubspot_ID AS INT64) AS hubspot_id,
  DATE(event_start_date_time, "Asia/Kolkata") AS web_scheduled_date,
  webinar_type, role_domain, work_ex, channel, utm_campaign,
  formatted_date, event_start_date_time, lead_created_time,
  Sale_date, net_revenue, Channel AS sale_channel
FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
WHERE dupe_logic = 1 AND dupe_flag = 0 AND gql_flag = 0
  AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2','3-4')
QUALIFY ROW_NUMBER() OVER (PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata") ORDER BY formatted_date ASC) = 1;
```

Then every per-event query reads from `qualified_leads_per_event WHERE web_scheduled_date IN UNNEST(@dates) AND webinar_type = @wt`.

**Why it's risky to ship today:**
- Needs write access to `ik-marketing-data` (the source project, not `masterclass-automation-ik`). Per memory note, the service account has `roles/bigquery.user` + `roles/bigquery.dataEditor` on `masterclass-automation-ik` only.
- A materialized view auto-refreshes on every source-table change → continuous low-level cost. Need to confirm refresh cadence is acceptable.
- Schema changes in the source table (`US_Domain_combined_view`) could break the view silently.

## When to revisit

Pick this up when:
- Snapshot cron time becomes a complaint (today after parallelization it should be ~1-2 min for 30 events, fine), OR
- BigQuery monthly bill becomes a complaint (compare to baseline now; revisit if it doubles).

The parallelization that just shipped (#1) gets the *latency* win. This optimization is purely a *cost* win.

## My recommendation

Wait 2-4 weeks. After partitioning the snapshot tables (migration_partition_snapshots.sql) and parallelizing the cron sub-queries, measure the BigQuery cost per snapshot run. If still high, do Option A in a follow-up branch with a backfill comparison test (run the new code, dump rows to `event_snapshot_test`, diff vs production rows for the same date).
