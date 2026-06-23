# Optimization Report — Masterclass Automation Tool

Companion to `QA_REPORT.md`. Scope: speed up queries, reduce BigQuery cost, reduce page load time, simplify the code. Ranked by expected impact ÷ effort.

Severity in this doc means **expected savings**, not bugs:
- **H** = big win (>30% query time, >$/month, or noticeable UX speedup)
- **M** = meaningful win
- **L** = small win or hygiene

---

## TOP 10 wins (do these first)

| # | Sev | What | Where | Estimated win |
|---|-----|------|-------|---------------|
| 1 | **H** | Parallelize the 5 sub-queries inside `_build_event_snapshot_india` (and US) | server.py:1086-1121 | Snapshot cron drops from ~6× per-event latency to ~1× — for 20 events that's ~5× faster overall (minutes saved per cron run) |
| 2 | **H** | Partition + cluster `history.event_snapshot` and `history.event_daily` | BigQuery DDL | Every `/events`, `/event/<id>`, `/months` request currently QUALIFY-scans the entire snapshot history. After partition by `DATE(snapshot_at)` + cluster by `event_id`: 10–100× fewer bytes scanned (= 10–100× cheaper) |
| 3 | **H** | Materialize a per-event "qualified leads" CTE once, reuse across cohort/attendance/calls/emails/sales | server.py:1001-1112 | Every snapshot re-runs the same `dupe_logic=1 QUALIFY ROW_NUMBER()` over `US_Domain_combined_view` 6 times. Collapse to 1 → ~5× fewer bytes scanned in BQ per event |
| 4 | **H** | Wire `months.html` to the existing `/months` endpoint instead of re-aggregating client-side | months.html:324, server.py:388-493 | Server already does the right work; page just throws it away and refetches 200 events. Saves ~30-40 KB JSON per request and removes a class of correctness bugs (BUG-10, BUG-21, BUG-22 from QA report) |
| 5 | **H** | Remove the dead `ROW_NUMBER() OVER (PARTITION BY call_id)` in `_query_calls_india` | server.py:1288-1296 | `call_id` is already unique; the window function does nothing but make BQ shuffle. Drop the inner subquery → straight SELECT |
| 6 | **M** | Add `Cache-Control: private, max-age=30` to `/events` | server.py:364 | Hub refreshes within 30s reuse cache; `_get_event` and `/months` already do this. Cuts repeated-load BQ cost for active users |
| 7 | **M** | Cache the BigQuery `Client()` at module load instead of `bigquery.Client(project=…)` per request | server.py:337, 380, 434, 501, 615, 651, 938 | First request after cold start does discovery/auth; warm requests still re-instantiate. ~50-150 ms saved per request |
| 8 | **M** | Switch `HTTPServer` to `ThreadingHTTPServer` | server.py:1814 | Currently every request blocks the server. With 2 users hitting `/event/<id>` simultaneously, the second waits ~3s for the first. ThreadingHTTPServer is a 2-line change |
| 9 | **M** | Switch all `Tailwind CDN` pages to a pre-built CSS file (`styles.css` already exists) | All 5 HTML files | The runtime CDN compiles classes in the browser; FCP drops 200-500ms. Also removes a cdn.tailwindcss.com outage risk and a CSP soft-spot |
| 10 | **M** | Fold the 3 separate reads of `Marketing_Email_Data` into one with conditional aggregation | server.py:1444-1488 | Currently SCANs the email table 3× per event (sent + delivered + engagement). Fold to 1 read = ~3× less BQ scan on the largest source table |

If you only do 5 of these: 1, 2, 3, 4, 9.

---

## Query optimizations (BigQuery)

### Q-01 — Parallelize sub-queries inside snapshot build
**Where:** `server.py:1086-1121` (India), `server.py:1628-1644` (US)

Today each event runs the queries **serially**: leads → cohort → attendance → calls → emails → sales. Each query is independent (no shared CTEs in scope between them). The pattern in `_get_event` (server.py:507-526) already uses `ThreadPoolExecutor(max_workers=3)` — apply the same here with `max_workers=5`. BigQuery has very high parallelism budget; the bottleneck is request RTT (Cloud Run asia-south1 ↔ BQ US is ~150-300ms each way).

Estimated time savings: snapshot for a single India event drops from `5 × (~2-5s query)` to `~max(2-5s)`. For 20 events the cron drops from ~5-10 minutes to ~1-2 minutes.

Cost-neutral (same bytes scanned, just in parallel).

### Q-02 — Partition + cluster snapshot tables
**Where:** BigQuery DDL on `masterclass-automation-ik.history.event_snapshot` and `event_daily`

Both tables are append-only. Every `/events` list issues:
```sql
QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1
```
which **reads every row ever written** to pick the latest per event. After a year of daily snapshots × 30 events = ~11,000 rows — small now but unbounded.

Migrate:
```sql
CREATE OR REPLACE TABLE masterclass-automation-ik.history.event_snapshot
PARTITION BY DATE(snapshot_at)
CLUSTER BY event_id AS
SELECT * FROM masterclass-automation-ik.history.event_snapshot;
```

Then rewrite the QUALIFY to filter the partition (e.g., last 30 days):
```sql
WHERE DATE(snapshot_at) >= CURRENT_DATE - 30
QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1
```

Bytes scanned drops by ~30× for daily reads after a year of history.

### Q-03 — Materialize the "qualified leads" CTE once per event
**Where:** server.py:1001-1112 (India), 1524-1644 (US)

Every per-event sub-query re-runs the same dedup logic against `US_Domain_combined_view` (~big table):

```sql
SELECT … FROM `US_Domain_combined_view`
WHERE dupe_logic = 1 AND <event filters>
QUALIFY ROW_NUMBER() OVER (PARTITION BY hubspot_ID, …) = 1
```

This appears in:
- `_build_event_snapshot_india` leads/spends query (~lines 1001-1043)
- `_query_cohort_india` (~1138-1173)
- `_query_attendance_india` (~1209-1241)
- `_query_calls_india` (~1258-1337) — uses similar `base` CTE
- `_query_emails_india` (~1424-1488)
- `_query_sales_india` (~1378-1409)

That's 6 reads of the same source table per event. Options:

**Option A (simplest):** Create a session-scoped table for the dedup:
```sql
CREATE TEMP TABLE qualified_leads_<event_id> AS
SELECT hubspot_ID AS hubspot_id, role_domain, work_ex, channel, utm_campaign, formatted_date, event_start_date_time
FROM `…US_Domain_combined_view`
WHERE dupe_logic = 1 AND <event date filters>
QUALIFY ROW_NUMBER() OVER (…) = 1;
```
Then run each downstream query against the temp table. BQ scans the source once, ~100× cheaper for downstream selects.

**Option B (most maintainable):** Materialize a view `ik-marketing-data.India_Leads.qualified_leads_per_event` that does the dedup and is partitioned by `web_scheduled_date`. Then every per-event query filters by `web_scheduled_date IN (…)`.

Either option turns 6 source scans into 1, often 60-80% BQ cost reduction on India snapshots.

### Q-04 — Dead ROW_NUMBER in `_query_calls_india`
**Where:** server.py:1283-1296

```sql
ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY <activity_datetime> DESC) AS rn
```
followed by `WHERE rn = 1`. But `call_id` is the call primary key — there's never more than 1 row per `call_id` in `call_metadata`. The PARTITION + WHERE rn=1 is a no-op that adds a shuffle stage to the BQ execution plan.

**Fix:** Drop the inner subquery; SELECT directly from `call_metadata` with the date-shifted activity_datetime.

### Q-05 — Email funnel: 3 scans → 1 scan
**Where:** server.py:1444-1488

Today:
```sql
WITH email_sent AS (SELECT … FROM Marketing_Email_Data WHERE event_name='SENT' …),
delivered_events AS (SELECT … FROM Marketing_Email_Data WHERE event_name='DELIVERED' …),
engagement_events AS (SELECT … FROM Marketing_Email_Data WHERE event_name IN ('OPEN','CLICK') …)
```
Three full scans of `Marketing_Email_Data` (filtered by hubspot_id IN registered_leads).

**Fix:** Single conditional aggregation:
```sql
WITH events AS (
  SELECT CAST(hubspot_id AS INT64) AS hubspot_id, email_campaign_id, email_name, event_name, event_timestamp
  FROM `…Marketing_Email_Data`
  WHERE CAST(hubspot_id AS INT64) IN (SELECT hubspot_id FROM registered_leads)
    AND event_name IN ('SENT','DELIVERED','OPEN','CLICK')
),
per_lead_campaign AS (
  SELECT hubspot_id, email_campaign_id, ANY_VALUE(email_name) AS email_name,
    COUNTIF(event_name = 'SENT')      > 0 AS is_sent,
    COUNTIF(event_name = 'DELIVERED') > 0 OR COUNTIF(event_name IN ('OPEN','CLICK')) > 0 AS is_delivered,
    COUNTIF(event_name IN ('OPEN','CLICK')) > 0 AS is_opened,
    COUNTIF(event_name = 'CLICK')     > 0 AS is_clicked
  FROM events
  GROUP BY hubspot_id, email_campaign_id
)
SELECT COUNTIF(is_sent), COUNTIF(is_delivered), COUNTIF(is_opened), COUNTIF(is_clicked)
FROM per_lead_campaign;
```

3× less bytes scanned on the largest source table.

### Q-06 — `IN ({date_literal})` strings → parameterized DATE arrays
**Where:** server.py:992, 1519 — date_literal is a comma-joined string of `DATE 'YYYY-MM-DD'`.

Today:
```python
date_literal = ', '.join(f"DATE '{d.isoformat()}'" for d in live_dates)
# query: WHERE web_scheduled_date IN ({date_literal})
```

**Fix:** Use `@dates` array parameter:
```python
params.append(bigquery.ArrayQueryParameter('dates', 'DATE', live_dates))
# query: WHERE web_scheduled_date IN UNNEST(@dates)
```
Why it helps: BQ can use the parameter for **query caching**. Today every event's date set produces a unique query string → no cache hit. With parameters, BQ caches plan + sometimes results.

Also fixes the SQL injection in BUG-02 (QA report).

### Q-07 — `_list_events` heavy LEFT JOIN to `latest_snap`
**Where:** server.py:303-334

Every list of events JOINs to a `latest_snap` CTE that QUALIFY-scans the entire snapshot table. Combined with Q-02 (partition), this gets faster. Further, the JOIN pulls 35 snapshot columns; the Hub displays maybe 8 of them.

**Fix:** Add a SELECT list that names only the columns the frontend actually uses. Skip `extras`, `call_p7_*`, `call_p14_*` etc. on the list endpoint — the event detail page already fetches the full snapshot. Drops payload from ~5 KB/event to ~1.5 KB/event, JSON serialization time ~3× faster.

### Q-08 — `_snapshot_to_slack` (legacy) is unparameterized + redundant
**Where:** server.py:880-924

This path mirrors what `_snapshot_events_to_bq` already does. Consider deprecating it; the cron can derive a Slack snapshot directly from the per-event rows it just wrote. Eliminates one big query per day.

### Q-09 — Drop `webinar_type` from inner CTEs when filtered outside
**Where:** server.py:1158-1173 (India cohort) and similar

The inner subquery SELECTs `dupe_flag, gql_flag, webinar_type, dupe_logic` for the ROW_NUMBER → outer WHERE filters them. webinar_type is a tiny string, but more importantly the inner SELECT pulls every column. **Better:** project only the columns needed downstream of the window function. BigQuery is column-pruned via the SELECT, so this is small (BQ already prunes via storage), but it tidies the code and avoids surprises if you add a wide column later.

### Q-10 — Two India spend tables = two scans
The India snapshot first runs the leads+spend join, then the cohort query also reads from `US_Domain_combined_view`. The leads CTE doesn't carry through. If you already have a temp table from Q-03, the cohort query becomes a tiny GROUP BY instead of a re-scan.

---

## Server (`server.py`) code optimizations

### S-01 — Cache the BigQuery `Client()` at module load
**Where:** server.py:337, 380, 434, 501, 546, 615, 651, 938

Today every handler creates `bigquery.Client(project=BQ_APP_PROJECT)`. The first call after cold start does auth discovery (~200-500ms). Warm calls are cheaper but still create the object.

**Fix:**
```python
_BQ_CLIENT = None
_BQ_SRC_CLIENT = None
def _bq():
    global _BQ_CLIENT
    if _BQ_CLIENT is None:
        from google.cloud import bigquery
        _BQ_CLIENT = bigquery.Client(project=BQ_APP_PROJECT)
    return _BQ_CLIENT
def _bq_src():
    global _BQ_SRC_CLIENT
    if _BQ_SRC_CLIENT is None:
        from google.cloud import bigquery
        _BQ_SRC_CLIENT = bigquery.Client(project='ik-marketing-data')
    return _BQ_SRC_CLIENT
```

Use `_bq()` / `_bq_src()` throughout. Saves ~50-150ms per request.

### S-02 — Switch `HTTPServer` → `ThreadingHTTPServer`
**Where:** server.py:1814

```python
http.server.HTTPServer(('', PORT), Handler).serve_forever()
```

`HTTPServer` handles requests **sequentially**. With min-instances=1 and 2 users in the same minute, the second blocks until the first finishes — for a snapshot or wide BQ query, that's tens of seconds. Two-character fix:

```python
http.server.ThreadingHTTPServer(('', PORT), Handler).serve_forever()
```

Each request gets its own thread. The handlers are stateless (no shared mutables) so this is safe today. **Big** UX win at zero engineering cost.

### S-03 — Module-level imports for BQ + storage
**Where:** server.py imports `from google.cloud import bigquery` and `from google.cloud import storage` inside half a dozen handlers.

Move them all to the top. Saves per-request import overhead and clarifies what the file depends on. Combined with S-01 cleans up most of the handler bodies.

### S-04 — `_row_to_dict` allocates a new dict per row
**Where:** server.py:62-73

For `/events?limit=200` that's 200 small dicts × ~35 keys = ~7000 attribute lookups per request. Trivial today but if event counts grow, this matters. You can use a lighter row-projection by using `RecordBatch.to_pyarrow().to_pylist()` or simply iterate over `schema` once and use indexed access. Defer until profiler points here.

### S-05 — `/proxy` writes the response body via `r.read()` (full buffer)
**Where:** server.py:228-234

For a big Jira/Slack response this buffers everything before flushing. Use chunked relay (`shutil.copyfileobj(r, self.wfile)`). Small win unless Jira returns large bodies.

### S-06 — Combine `_query_attendance_india` and `_query_emails_india` lead-base
**Where:** server.py:1216-1240, 1424-1442

Both rebuild the "registered leads" CTE from scratch. Q-03 (temp table) handles this; if Q-03 isn't done, at least extract the subquery into a SQL string and reuse.

### S-07 — `_run_snapshot` is synchronous — Cloud Scheduler times out at 30 min
**Where:** server.py:729-736

If you ever backfill `?days_back=365`, the request blocks the Cloud Run instance until done. Better: spawn a background thread, return 202 immediately. Combined with idempotency this lets Scheduler retry cleanly.

### S-08 — JSON-encode in chunks for big responses
**Where:** server.py:200-208 — `json.dumps(data)` builds the whole body in memory.

The `/events` response can be 50KB+. Use a streaming JSON encoder (or compact format) if memory pressure becomes real.

### S-09 — Skip `_safe_query` defaults when source returns no data
The `_safe_query` wrapper passes a `default={}` dict. If the query succeeds and returns 0 rows, `_safe_query` returns the empty result without using the default — but the per-query lead-base subquery still scans the source table. With Q-03 this becomes free.

### S-10 — The legacy `_snapshot_to_slack` daily thread starts at every Cloud Run boot
**Where:** server.py:1797-1811

```python
def run_daily_snapshot():
    last_sent = None
    while True:
        now = datetime.datetime.now()
        if now.hour == 11 and now.minute < 2 and now.date() != last_sent:
            …
        time.sleep(60)
```

This burns 1 thread + a `time.sleep(60)` cycle forever. Cloud Scheduler already fires `/run-snapshot` at 5:30 UTC. **Delete** this thread (also fixes QA BUG-27 double-fire).

---

## Frontend optimizations

### F-01 — Replace runtime Tailwind CDN with pre-built CSS
**Where:** Every HTML page loads `https://cdn.tailwindcss.com` and runs JIT in the browser.

Pre-build with `npx tailwindcss -i ./styles.css -o ./dist/styles.css --minify`, commit the output, link it from `<head>`. Wins:
- FCP: -200 to -500ms (no runtime JIT)
- Reliability: no CDN outage risk
- Bundle: a single CSS file replaces the CDN script + per-class compile

`styles.css` already exists (8 KB). Extend it with the Tailwind output.

### F-02 — Wire `months.html` to `/months`
**Where:** months.html:324

Already covered in QA (BUG-10). Also a performance win: payload drops from ~30-40 KB of events to ~3 KB of monthly aggregates, and the page stops doing client-side aggregation across 200 rows.

### F-03 — Cache event list per country in the page
**Where:** hub.html, compare.html, months.html all hit `/events` fresh on every country switch.

Cache a per-country object in JS for the session lifetime; invalidate on tab focus or after 60s. Saves repeated cross-region BQ hits.

### F-04 — Compare page picker re-fetches 200 events every open
**Where:** compare.html:486-496

Cache `pickerEvents` once per session per country. Open/close picker becomes instant.

### F-05 — Avoid Tailwind dynamic class names
**Where:** hub.html:706 — `grid-cols-${Math.min(6, lastSix.length)}`

Tailwind JIT cannot statically prove `grid-cols-1`..`grid-cols-6` are used (they appear only in interpolation). With the CDN JIT it sometimes works, sometimes silently falls back to the inherited width. Replace with explicit map:
```js
const colsClass = ['', 'grid-cols-1','grid-cols-2','grid-cols-3','grid-cols-4','grid-cols-5','grid-cols-6'][Math.min(6, lastSix.length)];
```

Also relevant to F-01 (pre-built CSS needs the safelist).

### F-06 — Hub renders Past tab table with `innerHTML` in one shot — already optimal
No win here.

### F-07 — `<script src="https://cdn.tailwindcss.com">` is `defer`-equivalent but still blocking parse
On months.html add `defer` to Chart.js so it doesn't block the first paint. (It's last in the head — slight FCP win.)

### F-08 — Inline `<style>` blocks in each HTML are duplicated
hub.html, compare.html, months.html each redefine `.login-overlay`, `.login-card`, etc. Move shared styles into `styles.css`. Saves ~5-10 KB across pages and makes design changes one-touch.

### F-09 — `Chart.js` is loaded from CDN with no SRI
Add an integrity hash, or self-host. Small reliability/security win.

### F-10 — `_row_to_dict` returns ISO timestamps as strings; frontend re-parses with `new Date(iso)` every render
For long lists this re-parsing is a sub-millisecond cost per row but adds up. Negligible at current scale.

### F-11 — Avatar dropdown uses `document.addEventListener('click', …)` on every page
Three pages add the same listener. Fine, but consolidate when (F-08) shared-style refactor happens.

### F-12 — Polls/Q&A export downloads via blob URL — already correct (event.html:185-196)
No change.

---

## Architecture-level wins (bigger lifts)

### A-01 — Move Cloud Run to a US region
Both Cloud Run regions are an option. Today: Cloud Run = asia-south1, BQ = US. Every BQ query pays ~150-300ms cross-region RTT plus higher egress. Reasons to stay in asia-south1:
- Most users are in India → faster TTFB on static HTML.
- BQ egress to asia-south1 is cheaper than to outside-GCP egress.

Reasons to move to us-central1:
- BQ queries dominate page time; cross-region drops every BQ query by ~200ms.
- The HTML is small (CDN-able anyway).

Best of both: put a Cloud CDN in front of Cloud Run + move compute to us-central1 → static HTML cached at the edge near India users, BQ queries 200ms faster. Significant lift, only worth it once cross-region latency becomes the user complaint.

### A-02 — Add a `Cache-Control: public, max-age=300, s-maxage=300` to static HTML
Today every refresh re-downloads the full HTML. Set on the GET path in Handler:
```python
if path.endswith('.html'):
    self.send_header('Cache-Control', 'public, max-age=300, s-maxage=300')
```
Saves ~120 KB per hub refresh.

### A-03 — Add response compression (gzip)
`SimpleHTTPRequestHandler` does not gzip. The 124 KB legacy HTML compresses to ~25 KB. Easiest path: front Cloud Run with Cloud CDN or set up a gzip middleware (or just minify+compress before serving in Python). 80% bandwidth reduction.

### A-04 — Switch from `http.server` to `uvicorn`/`hypercorn` + ASGI handlers
The stdlib `http.server` is single-threaded (see S-02), no HTTP/2, no compression, no easy middleware. Migrating to FastAPI or Starlette is medium effort and unlocks: HTTP/2, gzip middleware, OIDC middleware (fixes QA BUG-01), proper async (free parallelism for I/O-bound BQ calls), proper auto-docs. Defer until the next big iteration.

### A-05 — Move snapshot logic out of the request handler
`/run-snapshot` does everything inline (server.py:729-736). For backfills (`days_back=365`) this blocks the Cloud Run instance. Move to a Cloud Run Job (or background task) and have `/run-snapshot` enqueue. Decouples runtime from the web tier.

### A-06 — Stream BigQuery result rows instead of fully materializing
`_bigquery` (server.py:265) builds `[list(row.values()) for row in result]` in memory. For very large result sets at 512Mi this is the OOM vector. Stream rows directly into the JSON response with a generator-based encoder.

---

## What NOT to optimize (yet)

- **Polls/Q&A export** (`_poll_qna_export`) — runs once per event, fast enough. Move the fuzzy match away from `LIKE` only if it produces wrong results (BUG-26 in QA).
- **Cohort dictionary mapping in Python** (`_query_cohort_india` lines 1180-1202) — looks heavy but iterates over ≤ 30 rows.
- **JSON serialization in `_json_response`** — `json.dumps` with `default=json_serial` is fine for typical payloads. Only revisit if responses grow above 1 MB.
- **localStorage reads** — these are microseconds, fine.

---

## Implementation order (suggested)

**Week 1 (quick wins, no schema change):**
1. S-02 ThreadingHTTPServer (2 lines)
2. S-01 cache BQ client (10 lines)
3. S-10 delete in-process daily thread (5 lines)
4. Q-04 drop dead ROW_NUMBER (5 lines)
5. Q-06 parameterize `IN (…)` dates (also fixes SQL injection from QA)
6. F-05 fix dynamic Tailwind classes (5 lines)

**Week 2 (medium lifts):**
7. Q-01 parallelize snapshot sub-queries (ThreadPoolExecutor, ~30 lines)
8. F-02 wire months.html to `/months` (replace render function)
9. Q-05 fold email funnel scans
10. Q-07 trim `/events` projection
11. F-01 pre-build Tailwind CSS
12. A-02 cache-control on HTML

**Week 3 (bigger):**
13. Q-02 partition + cluster snapshot tables (one-time DDL + rewrite queries)
14. Q-03 temp-table for qualified leads
15. F-04 cache picker events
16. A-03 add gzip

**Defer until pain:**
17. A-01 region move
18. A-04 framework migration
19. A-05 background snapshot jobs

---

## Expected aggregate impact

After Week 1+2 changes:
- **Snapshot cron run** for 20 events: ~5-10 min → ~1-2 min (mostly Q-01)
- **Page load** for hub.html: ~1.5s → ~600ms (S-01 + S-02 + F-01 + A-02)
- **BQ cost** per snapshot: down ~50-70% (Q-03 + Q-05 + Q-07)
- **Concurrent user latency**: serialized → parallel (S-02)

After Week 3:
- **`/events`, `/event/<id>`, `/months` warm latency**: ~200-300ms → ~80-150ms (Q-02 partition + warm cache)
- **Months page** correctness restored (F-02), payload 30 KB → 3 KB

---

*Companion to `QA_REPORT.md`. All file:line citations are from current code in `/Users/utkarshgupta/Documents/` as of audit date.*
