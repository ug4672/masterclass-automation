# Masterclass Automation — Project Context

> **To continue in a new conversation**: hand Claude this file at the start. Say "Read `/Users/utkarshgupta/Documents/MASTERCLASS_AUTOMATION.md` and we'll continue from there." Last updated: 2026-06-23.

---

## TL;DR

Internal marketing-automation tool for **Interview Kickstart**, owned by **Utkarsh Gupta (performance marketing)**. Tracks masterclass + launchpad events end-to-end — creates Jira tickets + Slack DMs on launch, snapshots BigQuery metrics daily, and surfaces it all in a card-based hub. Deployed on Cloud Run, gated by Google Sign-In for `@interviewkickstart.com`.

**Current state**: revamp is shipped and live. The original 3-tab tool (`Masterclass Automation.html`) still works for "Create Event"; the new card-based UI (`hub.html` + `event.html` + `compare.html` + `months.html`) is the default landing page. Schema is deployed, daily cron writes per-event snapshots automatically. As of 2026-06-21: snapshot cron sub-queries run in parallel (~5× faster), per-event Refresh button on the detail page lets users force a fresh snapshot on demand, and the legacy daily Slack DM path is disabled pending a spend-dedup fix.

---

## Live URLs

| URL | What |
|---|---|
| `https://masterclass-automation-1016538215063.asia-south1.run.app/` | Redirects to `/hub.html` |
| `…/hub.html` | New card grid: "Upcoming" / "Past" |
| `…/event.html?id=<event_id>` | Per-event detail page (with Refresh button) |
| `…/compare.html?ids=a,b,c` | Side-by-side comparison (up to 8 events) |
| `…/months.html` | Monthly performance overview (6 months, leaderboard, insights) |
| `…/Masterclass%20Automation.html` | Legacy 3-tab UI (Create Event / Leads / Webinar Attendance) |

The `1016538215063` in the URL is the GCP project number; can't be stripped from auto-generated `.run.app` URLs. Options to clean it up (custom domain / URL shortener / GitHub Pages redirect) discussed — user hasn't picked yet.

---

## File map (all in `/Users/utkarshgupta/Documents/`)

| File | Purpose |
|---|---|
| `server.py` | Python HTTP server. Routes, auth, BQ queries, snapshot writer. ~1850 lines. Uses `ThreadingHTTPServer` since 2026-06-21. |
| `Masterclass Automation.html` | Legacy 3-tab UI. Mobile-responsive as of 2026-06-17. |
| `hub.html` | Dynamic card grid. Fetches `/events?status=…`. |
| `event.html` | Dynamic detail page. Fetches `/events/:id`. Has per-event Refresh button (2026-06-21). |
| `compare.html` | Compare workspace — side-by-side up to 8 events. |
| `months.html` | Monthly aggregation page — 6-month rollup + leaderboard + auto-insights. |
| `styles.css` | Shared styles for new pages. |
| `mockup-upcoming.html`, `mockup-detail.html`, `hub_mock.html`, `months_mock.html`, `months_mock_v2.html` | Static design mockups (visual reference only). v2 (2026-06-23) proposes new Lead Quality trend + Call Efforts trend sections + dual revenue hero (event-attributed + sale-month). |
| `schema.sql` | DDL for BQ tables. Already applied. Safe to re-run. |
| `Dockerfile` | Selectively `COPY`s files — must add new HTML files explicitly. |
| `requirements.txt` | `google-cloud-bigquery`, `google-cloud-storage`, `openpyxl`. |
| `.gcloudignore` | Limits source upload size. |
| `REVAMP.md` | Detailed slice-by-slice progress log. |
| `MASTERCLASS_AUTOMATION.md` | This file. |
| `QA_REPORT.md` | Full QA audit (2026-06-21): 34 findings, 30 verified bugs with file:line, full test matrix. |
| `OPTIMIZATION_REPORT.md` | Ranked optimization recommendations (2026-06-21): 10 top wins, implementation order. |
| `OPTIMIZATION_DEFERRED.md` | Design note for deferred CTE-consolidation optimization (#3 from the report). |
| `migration_partition_snapshots.sql` | BQ DDL to partition + cluster `history.event_snapshot` and `history.event_daily`. **Not yet run.** |

**Git repo**: https://github.com/ug4672/masterclass-automation.git (local at `/Users/utkarshgupta/Documents`)

---

## Architecture

```
┌──────────────────────┐
│  Browser (sign-in    │
│  required, IK domain)│
└──────────┬───────────┘
           │
           ▼
┌──────────────────────────────────────────────────────┐
│  Cloud Run: masterclass-automation                   │
│  Project: masterclass-automation-ik (PERSONAL acct)  │
│  Region:  asia-south1                                │
│  server.py (http.server.ThreadingHTTPServer)         │
└───┬──────────────────────────────────────────────────┘
    │
    ├──► Jira REST API (creates tickets on Create Event)
    ├──► Slack API (manual "Send to Slack" on Leads tab only;
    │              daily DM in cron is DISABLED, see Decisions)
    │
    ├──► BigQuery: ik-marketing-data (READ — source data)
    │       India_Leads.US_Domain_combined_view      (India leads)
    │       India_Leads.Combined_India_Spend          (India spend)
    │       Marketing_data_new_logic.Bq_data_Alumni   (US leads)
    │       Google_Sheets.Combined_Spend_data         (US spend)
    │       Webinar_analytics.webinar_attendee_data_from_zoom
    │       Webinar_analytics.zoom_webinar_polls_view (Polls export)
    │       Webinar_analytics.zoom_webinar_qa_json_view (Q&A export)
    │       Marketing_data_new_logic.call_metadata
    │       Email.Marketing_Email_Data
    │
    ├──► BigQuery: masterclass-automation-ik (WRITE — app data)
    │       events.event              ← one row per masterclass
    │       events.fx_rates_monthly   ← USD/INR per month (for revenue conversion)
    │       history.event_daily       ← per (event, registration_date) per cron run
    │       history.event_snapshot    ← per event per cron run (cumulative)
    │
    ├──► GCS: masterclass-snapshot-config-ik (snapshot_config.json, currently unused by cron)
    └──► Secret Manager: bq-credentials (user ADC)

Cloud Scheduler: masterclass-daily-snapshot
  → POST /run-snapshot daily at 5:30 UTC (11 AM IST)
  → Writes per-event BQ snapshots (5 sub-queries per India event,
    2 per US event, all run in parallel via ThreadPoolExecutor)
  → Legacy daily Slack DM path disabled — see Decisions
```

---

## GCP setup

### Personal project — where everything is deployed

- **Project**: `masterclass-automation-ik`
- **Owner**: `ug4672@gmail.com` (personal account)
- **Billing**: `012FE8-171DE1-10CE33` (on personal account)
- **Region (Cloud Run)**: `asia-south1`
- **Region (BigQuery)**: **`US`** (intentional — see Gotchas)

### Workspace project — only hosts the OAuth client

- **Project**: `masterclass-499210` (project number `801560849080`)
- **Owner**: `utkarsh.gupta@interviewkickstart.com` — owns it under the `interviewkickstart.com` Workspace org
- **OAuth Client**: `801560849080-lgdgif7rjbgap4fqqp0cbcj7duc2ed0o.apps.googleusercontent.com`
- **Consent Screen**: User Type = **Internal** (confirmed by user 2026-06-17). Any `@interviewkickstart.com` user signs in with no IT review.

### Why two projects

`utkarsh.gupta@interviewkickstart.com` is Owner on `masterclass-499210` (in IK Workspace), but that project has **no billing account attached** — can't enable Cloud Run there. So:
- OAuth client lives in the workspace project (gives Internal sign-in)
- Cloud Run + BQ live in the personal project (has billing)

OAuth and Cloud Run hosting are fully decoupled. Works fine as long as `GOOGLE_CLIENT_ID` env var on Cloud Run matches the workspace OAuth client ID.

To migrate Cloud Run to the IK workspace project later: need IT to attach a billing account to `masterclass-499210`.

### Auth identity quirk

The `bq-credentials` Secret Manager secret holds **user ADC credentials** (type=`authorized_user`), authenticating as **`utkarsh.gupta@interviewkickstart.com`** — NOT `ug4672@gmail.com`.

That account has `roles/owner` on neither project by default. Granted on `masterclass-automation-ik` (2026-06-15):
- `roles/bigquery.user` (jobs.create + read)
- `roles/bigquery.dataEditor` (insert into events.event + history.*)
- `roles/storage.objectUser` (GCS snapshot config read/write)

Any future code that hits a new project needs an equivalent grant. Don't assume `ug4672@gmail.com`'s ownership transfers — it doesn't.

---

## Data model

All tables live in `masterclass-automation-ik`, US region. Schema source of truth: `schema.sql`.

### `events.event` — one row per masterclass

Created when "Launch Event" is clicked in the legacy form (slice 1 wired this).

```sql
event_id          STRING NOT NULL    -- slug: "<title-words>-<initials>-<mmm-dd>"
title             STRING NOT NULL
topic             STRING             -- ai_agents | system_design | behavioral | data | ml | distributed
event_type        STRING             -- masterclass | launchpad
country           STRING             -- India | US
webinar_type      STRING             -- MASTERCLASS_INDIA | MASTERCLASS_INDIA_3 | MASTERCLASS_INDIA_5 | MASTERCLASS_EVENT_AI
live_at           TIMESTAMP
day2_live_at      TIMESTAMP          -- Launchpad only
go_live_date      DATE
landing_url       STRING
zoom_url          STRING
instructor_name   STRING
instructor_role   STRING             -- "Principal Engineer · Stripe"
summary           STRING
design_notes      STRING
goal_regs         INT64
status            STRING             -- upcoming | live | aired | archived
jira_design_key   STRING
jira_landing_key  STRING
created_at        TIMESTAMP NOT NULL
created_by        STRING             -- user email
updated_at        TIMESTAMP
PARTITION BY DATE(live_at)
CLUSTER BY country, status
```

### `history.event_daily` — per (event_id, registration_date) per cron run

Captures the day-by-day spend/regs/CPIQL curve (same granularity as the old daily Slack snapshot). Append-only; reads pick latest per day via `QUALIFY ROW_NUMBER()`.

```sql
event_id          STRING NOT NULL
registration_date DATE   NOT NULL
snapshot_at       TIMESTAMP NOT NULL
meta_regs         INT64
meta_spend        FLOAT64
crm_regs          INT64
other_regs        INT64
other_spend       FLOAT64
total_regs        INT64
total_spend       FLOAT64
cpiql             FLOAT64
extras            JSON   -- escape hatch
PARTITION BY registration_date
CLUSTER BY event_id
```

### `history.event_snapshot` — per event per cron run (cumulative + cohort + post-event)

```sql
event_id          STRING NOT NULL
snapshot_at       TIMESTAMP NOT NULL
hours_to_live     FLOAT64       -- negative if post-event
-- leads (cumulative)
total_regs, meta_regs, meta_spend, crm_regs, other_regs, other_spend, cpiql
-- post-event engagement
attendees, attendance_pct
email_sent, email_delivered, email_opened, email_clicked
calls_attempted, calls_connected, avg_talk_seconds
-- cohort breakdown (qualified leads only)
role_sde, role_ml, role_fe, role_management, role_systems, role_null, role_other
we_0_2, we_3_5, we_6_10, we_10_15, we_15_20, we_20p, we_other, we_10p
-- US per-channel reg buckets
us_yt_regs, us_social_regs, us_l10x_email_regs, us_l10x_bot_regs, us_ni_base_regs, us_other_regs
-- call lifecycle stages (pre, p2, p7, p14, p14p)
call_total_leads, call_pre_*, call_p2_*, call_p7_*, call_p14_*, call_p14p_* (attempts/connects/talk_mins/covered)
-- sales
sales, revenue, paid_revenue, overall_roas, paid_roas
extras            JSON
PARTITION BY DATE(snapshot_at)
CLUSTER BY event_id
```

**Schema evolution rule of thumb**: `ALTER TABLE ADD COLUMN` is free + instant. Pick `FLOAT64` over `INT64` when ratios are possible. Use `extras JSON` for experimental metrics, promote later.

**Append vs upsert (2026-06-21 decision)**: Snapshots are append-only. Refresh writes a new row, doesn't update an existing one. UI reads latest via `QUALIFY ROW_NUMBER`. Reasoning: BQ favours append; streaming inserts are cheaper than MERGE; storage at our scale is effectively free (~3 MB/year for `event_snapshot`); historical snapshots preserve "trend over time" data we can use later. If table ever hits ~1 GB (years away), add a quarterly compaction job.

### Common read patterns

```sql
-- Latest per (event, day)
SELECT * FROM `…event_daily` WHERE event_id = @eid
QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id, registration_date ORDER BY snapshot_at DESC) = 1
ORDER BY registration_date;

-- Latest snapshot per event (for hub grid)
SELECT * FROM `…event_snapshot`
QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1;
```

---

## Endpoints (all routes on `server.py`)

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/` | — | 302 → `/hub.html` |
| `GET` | `/hub.html` | static | Card grid |
| `GET` | `/event.html?id=…` | static | Detail page (with Refresh button) |
| `GET` | `/compare.html?ids=a,b,c` | static | Compare workspace |
| `GET` | `/months.html` | static | Monthly overview |
| `GET` | `/Masterclass%20Automation.html` | static | Legacy UI |
| `GET` | `/auth/check` | — | Returns 200 + email if session valid, else 401 |
| `POST` | `/auth/verify` | — | Verifies Google ID token, sets `ik_session` cookie |
| `POST` | `/auth/logout` | — | Clears cookie |
| `POST` | `/proxy?url=…` | session | Jira forwarder (legacy Create Event flow) |
| `POST` | `/bigquery` | session | Arbitrary BQ query (legacy Leads / Attendance tabs) |
| `POST` | `/save-snapshot-config` | session | Updates GCS snapshot_config.json (currently unused by cron) |
| `POST` | `/events` | session | Persist a new event row (Launch Event flow) |
| `GET` | `/events?status=upcoming\|aired&country=…&ids=…&limit=…` | session | List events for hub / compare picker |
| `GET` | `/events/:id` | session | Event detail payload |
| **`POST`** | **`/events/:id/refresh`** | **session** | **Per-event re-snapshot. Added 2026-06-21.** |
| `GET` | `/months?country=…&series=…` | session | Server-side monthly aggregation (months.html doesn't yet consume) |
| `GET` | `/series?country=…` | session | List distinct event series |
| `GET` | `/event/poll-qna-export?event_id=…` | session | Downloads Polls + Q&A as XLSX |
| `POST` | `/run-snapshot` | **none (security gap)** | Triggers per-event BQ snapshots for all active events |

Session: HMAC-signed `ik_session` cookie, 7-day TTL. Server checks `hd` claim = `interviewkickstart.com`.

---

## Deploy

```bash
cd "/Users/utkarshgupta/Documents" && gcloud run deploy masterclass-automation \
  --source . --project=masterclass-automation-ik --region=asia-south1 \
  --allow-unauthenticated \
  --set-env-vars="GCS_BUCKET=masterclass-snapshot-config-ik,GOOGLE_APPLICATION_CREDENTIALS=/secrets/bq-creds.json,GOOGLE_CLIENT_ID=801560849080-lgdgif7rjbgap4fqqp0cbcj7duc2ed0o.apps.googleusercontent.com,SESSION_SECRET=8128cb8bcbf74b2376e522c5b7a07e74d2c48967e7272f85242f19640e0a584c" \
  --set-secrets="/secrets/bq-creds.json=bq-credentials:latest" \
  --memory=512Mi --min-instances=1 --quiet
```

Run as `ug4672@gmail.com`. Switch with `gcloud config set account ug4672@gmail.com`.

**Before deploying new HTML files**: add `COPY <file> .` to the `Dockerfile`. The current Dockerfile selectively copies — files not listed silently 404.

**Latest revision**: `masterclass-automation-00073-h2p` (deployed 2026-06-23). Includes `SPEND_CORRECTIONS` dict + ₹37,619 correction for `ai-launchpad-master-ai-may09` (source pipeline missing May 3).

---

## Decisions made

| Decision | Why |
|---|---|
| Slug `event_id` (`<title>-<initials>-<mmm-dd>`) over UUID | Readable in URLs, Slack, logs. Generated at create time, immutable. |
| Two snapshot tables (`event_daily` + `event_snapshot`) | Different query shapes — per-day curve vs. cumulative-now. Forcing both into one table wastes scan cost. |
| Append-only snapshot writes; read latest via `QUALIFY ROW_NUMBER()` | Simpler than DML MERGE. BQ partition pruning keeps it cheap. Storage at our scale is effectively free. Preserves trend history for future features. |
| `extras JSON` column on snapshot tables | Lets us add experimental metrics without schema migration. Promote to typed columns once they stabilize. |
| `role_fe` schema column always NULL | Source role mapping puts Front-end inside Software Engineer bucket — there's no separate FE bucket to populate it from. |
| `we_0_2` always 0 | Source query filters out work_ex IN ('0-2','3-4') and students — they're not target audience. Documented, not a bug. |
| Failure of one snapshot path doesn't kill the other | `_do_snapshot` wraps Slack DM and BQ writes independently. Cron job continues if one fails. |
| OAuth client in workspace project; Cloud Run in personal project | OAuth needs to be in IK Workspace for Internal sign-in (no IT review). Personal project has the only billing account I have access to. Decoupled. |
| `/` redirects to `/hub.html`; legacy URL still works | Soft cutover. Existing bookmarks unaffected. |
| Mobile responsive only added to legacy tool so far | User flagged it. Hub + Event pages still desktop-tuned. |
| **Snapshot cron sub-queries parallelized (2026-06-21)** | Each per-event snapshot was 5 sequential BQ queries (India) or 2 (US). Wrapping in `ThreadPoolExecutor` drops cron time from ~5-10 min to ~1-2 min for 20+ events. Output identical; queries are independent reads. |
| **ThreadingHTTPServer (2026-06-21)** | Stdlib `HTTPServer` is single-threaded → 2 concurrent users had the 2nd blocked for tens of seconds during a wide BQ query. Two-line change. Handlers are stateless so thread-safe. |
| **Per-event Refresh button (2026-06-21)** | Daily cron leaves snapshot data up to 24h stale. Users on the event detail page want fresh numbers on demand. New `POST /events/:id/refresh` endpoint runs the same `_build_event_snapshot` path for one event, writes a new snapshot row (~5-15s round-trip). |
| **Legacy `_snapshot_to_slack` disabled in cron (2026-06-21)** | Its spend-join uses naive `LIKE`; `_snapshot_events_to_bq` uses REGEXP-normalised matching. The two produced slightly different numbers for the same event. Commented in `_do_snapshot`; the manual "Send to Slack" button on the legacy Leads tab still works. Re-enable once dedup logic is ported from server.py:1024-1034. |
| **Months page CPIQL & call-totals fixed in place (2026-06-21)** | Page was using `total_regs` as CPIQL denominator (should be `meta_regs`) and `pre + p2 + p14p` for call totals (overlapping windows). Fixed client-side in `months.html`. Numbers will shift — CPIQL up ~30-50% (the new value is correct). |

---

## Gotchas — non-obvious things that bit me

1. **`history` dataset is in `US` region, not `asia-south1`** as memory originally claimed. Cloud Run is asia-south1, BQ data is US. Schema's `CREATE SCHEMA OPTIONS(location='asia-south1')` failed; cross-region BQ joins aren't supported. Fixed by removing the location option; everything lives in US.

2. **`bq-credentials` secret is `authorized_user` (user ADC), NOT a service account.** Authenticates as `utkarsh.gupta@interviewkickstart.com`. Means:
   - The old `_do_snapshot` worked against `ik-marketing-data` because that account has BQ access there.
   - New code targeting `masterclass-automation-ik` got 403 until I explicitly granted roles. Documented above.

3. **Dockerfile selectively `COPY`s.** `COPY hub.html .` had to be added explicitly when I created the file. First deploy after creating new HTMLs 404'd on the new pages until I noticed.

4. **`bq query` with multi-statement scripts** runs all statements in one job, but if the FIRST statement creates a dataset, dependent `CREATE TABLE` statements in the same job can't resolve it. Workaround: `bq mk` the dataset separately, then run the rest.

5. **Cloud Run URL contains project number `1016538215063`** — can't strip. Custom domain or shortener required.

6. **OAuth client confusion**:
   - `801560849080-…` lives in `masterclass-499210` (workspace project). User Type = Internal. **Use this.**
   - `1016538215063-…` lives in `masterclass-automation-ik` (personal project). External + Testing. Triggers IT review for non-test-user IK accounts. **Don't use.**

7. **`HEAD /` returns 200, not 302** unless `do_HEAD` is overridden — `SimpleHTTPRequestHandler`'s default `do_HEAD` doesn't go through my `do_GET` logic. Fixed.

8. **Streaming inserts vs DML INSERT in BQ**: I use `insert_rows_json` (streaming). Rows are immediately queryable via SELECT but not editable via DML for ~90 min. Fine for our writes; just don't try to `UPDATE` a just-inserted row.

9. **US event `live_at` is currently stored at `+05:30` offset (IST), regardless of country.** The legacy Create-Event form (MA HTML:1167-1168) hardcodes `+05:30` for everyone. For US events this misaligns the snapshot date filter — `_build_event_snapshot_us` converts `live_at` to PT to derive `web_scheduled_date`, and the IST-encoded UTC can land on the wrong PT calendar day. Listed in known limitations.

10. **Refresh button writes a NEW row to `history.event_snapshot`** rather than updating an existing one. That's the append-only design (see Decisions). UI reads via `QUALIFY ROW_NUMBER() OVER (... ORDER BY snapshot_at DESC)` so users always see the latest. Storage cost negligible at our scale.

11. **`insert_rows_json` failures are returned, not raised.** `client.insert_rows_json(...)` returns a list of per-row errors (empty on success); it does NOT raise on failure. If you don't log/inspect the return value, inserts can silently fail forever. Bit us on 2026-06-22: a typed-STRUCT vs. JSON mismatch on the `extras` column made every `event_snapshot` insert fail silently for several days, and the only signal was that the UI looked "stale". Lesson: every place that calls `insert_rows_json`, log the returned errors loudly (cron now does this; per-event refresh too).

12. **`/events/:id` has a 60-second HTTP cache.** Set via `Cache-Control: private, max-age=60` (server.py:535). After a per-event Refresh, the browser will happily serve the pre-refresh response for up to 60s unless the fetch passes `cache: 'no-store'`. The refresh button does — but if you ever add a new "fresh-now" code path, remember to bypass the cache.

---

## Known limitations / open items

| Item | Status | Notes |
|---|---|---|
| **Mar 18, 2026 MASTERCLASS_INDIA_5 event** | Untracked | Source has 390 IQLs + 189 attendees on that PT date but no matching row in `events.event`. To track, add the event row (title/instructor/live_at) and next snapshot picks it up. Causes ~3% under-count on India dashboards. |
| **Dual revenue display** (mockup approved → ship) | Pending decision | `months_mock_v2.html` shows the proposal. Wiring to live data ≈4-5 hours (one new endpoint for sale-month revenue + UI changes). |
| **Lead Quality / Call Efforts monthly trends** | Pending decision | Same mockup. Roll up existing `event_snapshot` columns by month — no new BQ work needed. |
| **US event TZ bug** (high priority) | Open | MA HTML:1167-1168 stores `live_at` with `+05:30` regardless of country. All US event metrics are misaligned by ~12.5h. Fix is small; existing US event rows would need a BQ UPDATE to backfill. |
| **`/run-snapshot` unauthenticated** | Security risk | Anyone on the internet can trigger BQ work. Should require Cloud Scheduler OIDC. See `QA_REPORT.md` BUG-01. |
| **`/save-snapshot-config` SQL injection** | Security risk (currently dormant, Slack DM path disabled) | If re-enabled, signed-in users can inject SQL into the cron's query. `QA_REPORT.md` BUG-02. |
| **`/bigquery` runs arbitrary SQL** | Security risk | Any signed-in user can query anything `ik-marketing-data` allows. `QA_REPORT.md` BUG-04. |
| **Slack + Jira tokens in browser localStorage** | Security risk | Legacy form persists tokens to `localStorage` and re-POSTs them on every action. `QA_REPORT.md` BUG-03. |
| **Cookie missing `Secure` flag** | Security hygiene | server.py:759. One-line fix. |
| **`/proxy` is an open SSRF proxy** | Security risk | server.py:210-243. Allow-list URLs. |
| **Partition migration not yet run** | Pending user action | `migration_partition_snapshots.sql` is ready to run in the BQ console. Until then, every snapshot read scans the full history table. |
| **CTE materialization deferred (Optimization #3)** | Deferred | Documented in `OPTIMIZATION_DEFERRED.md`. Pure cost-saver; revisit if BQ bill jumps. |
| **Months page does not use `/months` endpoint** | Functional | Page reaggregates client-side. Bug fixes shipped 2026-06-21 cover the worst, but full wiring to `/months` would be cleaner and would need `/months` to start aggregating revenue + sales (currently doesn't). |
| US event support in snapshot writer | Done | India + US both wired since slice 2 v2. |
| Reskin "Create masterclass" form to match new visual | Open | Hub's "New masterclass" button currently dumps users into legacy form. |
| Pacing logic on hub cards | Crude | Current rule: `hours_to_live < 72 && ratio < 0.5` → behind. Needs `go_live_date` elapsed-time math for proper "expected by now" comparison. |
| Search input on hub | Not wired | Static input in mockup; no backend support yet. |
| Mobile responsive — `hub.html`/`event.html`/`compare.html`/`months.html` | Open | Only legacy tool is mobile-tuned. New pages cramp on phones. |
| Past-event backfill | In progress | User is planning to run a query against source data + paste output for me to insert into `events.event`. Format spec given below. |
| Backfill snapshots for older events | Untouched | Current window is -14d to +60d of `live_at`. Older events won't get snapshotted automatically by the cron. Per-event Refresh button works for any age. |
| Cleaner URL (drop project number) | Discussed, not picked | Custom domain (IT touch) / bit.ly / GH Pages redirect. User hasn't chosen. |
| Settings page move | Open | Old Config card still lives at top of legacy Create Event. Should move to avatar dropdown. |
| US OAuth (US team sign-in) | Untested | Server checks `hd == interviewkickstart.com` only. If US team is on a different Workspace, would fail. |

---

## Conversation history summary

### 2026-06-14 — Mockup design
- Created `mockup-upcoming.html` (card grid hub) and `mockup-detail.html` (event detail page) as static design references.
- Discussed what to remove from legacy tool: SQL viewers, Configuration block visibility, redundant date+webinar_type re-entry, MASTERCLASS_INDIA_* enum exposure.
- Discussed data persistence: 2 BQ tables initially, then expanded to 3 (added `event_daily` to preserve day-by-day granularity).
- Confirmed schema evolution plan (ALTER TABLE ADD COLUMN + extras JSON escape hatch).

### 2026-06-15 — Implementation (all 4 slices + slice 2 v2)
- **Slice 1**: Created `events.event` + `history.event_*` tables in `masterclass-automation-ik`. Added `POST /events` to `server.py`. Wired Launch Event button to call it.
- **Slice 2**: Extended `_do_snapshot` to write per-event rows to `event_daily` + `event_snapshot`. Verified with seeded test event (1,154 regs, ₹181.53 CPIQL).
- **Slice 2 v2**: Added 3 more queries — cohort role+work_ex, attendance from Zoom roster, call efforts, email reminder funnel. All metrics populated.
- **Slice 3**: Added `GET /events` + `GET /events/:id`. Created dynamic `hub.html` + `event.html`.
- **Slice 4 (soft cutover)**: `/` redirects to `/hub.html`. Legacy URL still works. Avatar menu has "Old tabbed UI" link.
- **Gotchas hit + fixed**: BQ region, ADC identity, Dockerfile selective COPY.
- **OAuth**: Initially used `801…` client ID, then "fixed" to `1016…`, then user got audience-mismatch error → switched back to `801…` (correct).

### 2026-06-17 — OAuth migration discussion + mobile fix
- User asked to move deployment to work account to avoid IT review.
- Investigation: Owner on `masterclass-499210` (IK workspace) but no billing account attached → can't run Cloud Run there.
- Realized OAuth + Cloud Run are decoupled. Solution: OAuth client in workspace project (`801…`) + Cloud Run stays in personal project. Sign-in to Internal consent screen → no IT review.
- User verified consent screen is Internal.
- User flagged legacy tool isn't mobile-friendly. Added `@media (max-width: 640px)` + `@media (max-width: 380px)` blocks to legacy `Masterclass Automation.html`. Single-column form, stacked header, full-width buttons.

### 2026-06-18 — URL cleanup + CONTEXT.md
- User asked how to remove `1016538215063` from URL. Discussed 3 options (custom domain / shortener / GH Pages redirect). No decision yet.
- Created the original `CONTEXT.md` (predecessor to this file).

### 2026-06-23 — Audit: India IQL & attendance gaps + months v2 mockup
- **Cross-checked India spend** against marketing reference (`actual` $4,450 for May 9 Launchpad vs our $4,036). Diff was ₹37,619 — **May 3 missing from `Combined_India_Spend` source** for the 5 `Pilot_Meta_L10X_India_01_AI_Launchpad_9_10_May*` campaigns. User confirmed actuals (May1 ₹14,842 + May2 ₹9,783 + May3 ₹12,994). Patched via SPEND_CORRECTIONS dict (see entry below).
- **IQL audit** — user reference 26,782 vs our 25,827. Gap: 390 from a missing `MASTERCLASS_INDIA_5` event on **Mar 18, 2026** that isn't in `events.event` + 186 from upcoming Jun 25 event (excluded from "aired" filter) + stragglers. Confirmed scope: we count `MASTERCLASS_INDIA / _3 / _5` only (per user). `MASTERCLASS_INDIA_2`, `_4` are recurring weekly events explicitly outside our scope.
- **Attendance audit** — user reference 10,452 vs our 9,963. Gap: 189 from Mar 18 untracked event + ~300 unexplained (within ~3% source-pipeline noise; user OK with leaving).
- **Launchpad day-1 + day-2 logic verified** — total IQLs sum both days (per-(hubspot, date) dedup), attendance dedupes by hubspot_id (so a person attending both days = 1 attendee). User confirmed this is intended behavior.
- **Dual-revenue concept surfaced** — there are two revenue definitions: (a) "event-attributed" (current, in `event_snapshot.revenue`, sale credited to the event whose leads converted) and (b) "by sale month" / P&L (not yet computed, sale credited to its own `Sale_date` month). User asked to design how to display both.
- **months_mock_v2.html created** (`/Users/utkarshgupta/Documents/months_mock_v2.html`) — static mockup with 3 new sections: dual-revenue hero (₹/Cr per month), Lead Quality Trend (stacked bars: role + work_ex), Call Efforts Trend (4 sparkline cards + multi-metric line chart). Performance Trend gets new "Revenue (P&L)" pill option. All numbers fabricated for the mockup; will wire to real data when approved.

### 2026-06-23 — Per-event spend corrections + May 9 AI Launchpad fix
- Added module-level `SPEND_CORRECTIONS` dict in `server.py` (next to `_RE_META`/`_RE_CRM`) — keyed by `event_id`, value is local-currency adjustment to add to snapshot's `meta_spend`.
- Applied in `_cumulative_snapshot_row` (server.py:2059-2064). Survives nightly cron overwrites — every snapshot run picks up the correction.
- **First entry: `ai-launchpad-master-ai-may09` += ₹37,619** — source pipeline `ik-marketing-data.India_Leads.Combined_India_Spend` dropped May 3 spend for the 5 `Pilot_Meta_L10X_India_01_AI_Launchpad_9_10_May*` campaigns. User cross-verified from Meta Ads Manager: May1 ₹14,842 + May2 ₹9,783 + May3 ₹12,994 ≈ ₹37,619. Brings event spend from ₹3,82,456 → ₹4,20,075.
- Other India events fully reconcile against source. Only this one event needs the correction.
- Adding future corrections is one-line in the dict; no schema change, no UI change.

### 2026-06-22 — Spend filters: also exclude `%holiday_offer%`; same exclusions on India
- Both India and US spend CTEs now exclude `%recorded_masterclass%` AND `%holiday_offer%`.
- Affected India campaigns: `Pilot_Meta_L10x_India_Recorded_Masterclass_Build_AI_Agents` (₹4,567 evergreen), `Pilot_Meta_L10X_India_Holiday_Offer_1` (₹89,315) and `_2` (₹48,404).
- Affected US campaigns (in addition to existing recorded_masterclass exclusion): `L10X_Meta_US_Holiday_Offer` ($2,298), `L10X_Meta_Canada_Holiday_Offer` ($2,110), `L10X_Meta_US_Holiday_Offer_New` ($1,974).
- Reason: these are funnel-wide promo/evergreen spend, not attributable to a single masterclass event.
- India structurally clean otherwise (no need to widen filter like US): all event-specific India campaigns follow `Pilot_Meta_L10X_<date>_India_<event>_Masterclass_*` pattern, captured by current `(l10x OR masterclass) AND (meta OR facebook OR l10x)` clause.

### 2026-06-22 — US spend filter: widen masterclass + exclude `recorded_masterclass`
- `_build_event_snapshot_us` spend CTE (server.py:1649-1654). Final filter:
  ```sql
  (LOWER(campaign_name) LIKE '%l10x%' OR LOWER(campaign_name) LIKE '%masterclass%')
  AND LOWER(campaign_name) NOT LIKE '%recorded_masterclass%'
  ```
- **Widened** (drop the secondary `AND (LIKE '%meta%' OR '%facebook%' OR '%l10x%')`) to capture event-specific paid campaigns like `Pilot_Taboola_US_46_Masterclass_AI_Reinvent` and `Pilot_YouTube_US_47_Masterclass_Mastering_RAG_*`.
- **Excluded** `recorded_masterclass` (Performance_Max_*_recorded_masterclass_*, Quora_*_Recorded_Masterclass_*) — those are the evergreen L10X funnel, not attributable to a specific event.
- Verified against IK's reference spreadsheet: Reinvent (Jan) Pilot_YouTube ($2,523) + Pilot_Taboola ($1,301) = $3,824 ✅. RAG (Feb) Pilot_YouTube ($5,214) + Pilot_Taboola ($1,913) + Pilot_Meta ×3 ($721) = $7,848 (slightly above user's ~$5,946 reference; difference is the Taboola_RAG inclusion which user OK'd as legitimate by parity with Reinvent's Taboola treatment).
- LinkedIn/remarketing/SwitchUp/Performance_Max/Quora_L10X campaigns still excluded — general lead-gen brand spend.

### 2026-06-22 — Fix US number formatting (was reading as INR)
- `fmtN()` in `hub.html`, `compare.html`, `months.html` — now branches on `isUS()`. India keeps Cr/L/k. US uses K/M/B. Was showing US revenue as e.g. `$3.5L` (Indian lakh suffix), which reads as INR.
- `formatRevHero()` in `months.html` — was rendering the Revenue Generated hero with no currency symbol at all (e.g., bare `3.5L`). Now prefixes `moneySym()` and uses K/M/B for US.
- Coverage Gap insight in `months.html` (hardcoded `~₹1.5 Cr` about MASTERCLASS_INDIA_2) — now gated to `!isUS()`. Was showing ₹ symbol + a series name irrelevant to US on the US dashboard.

### 2026-06-22 — US paid_revenue filter: L10X + Google YouTube
- Updated `_query_sales_us` (`server.py:~1965`) — paid_revenue filter changed from `LOWER(Channel) = 'youtube'` (no matches) to `Channel IN ('L10X', 'Google YouTube')`. Discovered by inspecting distinct Channel values in `Bq_data_Alumni`: L10X is 79% of US masterclass sales (the paid funnel), Google YouTube is the YT-ads channel; together they correspond to the spend already captured upstream (the US spend filter uses `LIKE '%l10x%'`/`'%meta%'`/`'%facebook%'`).
- Without this fix, US Paid ROAS would be 0 (since no row matched the old filter). Other channel values present in the data: Email, Organic, SMS — all treated as organic/CRM.

### 2026-06-22 — Added Call Efforts + Sales/Revenue for US events
- Added `_query_calls_us` and `_query_sales_us` in `server.py` (after `_query_attendance_us`). Wired both into the `_build_event_snapshot_us` parallel block (now `max_workers=4`: cohort + attendance + calls + sales).
- **Sales (US)**: reads `Sale_date`, `net_revenue`, `Channel` from `Bq_data_Alumni`. USD throughout (no INR conversion). Paid revenue filter = `LOWER(Channel) = 'youtube'` (covers any case spelling). Per user: "YouTube is the paid channel for US, not Facebook/Meta".
- **Calls (US)**: same `call_metadata` table as India, but lead base swapped to `Bq_data_Alumni`. PT timezone for event start. `leads_hubspot_id` (not `hubspot_ID`). 5 lifecycle windows identical to India (Pre / 0-2D / 0-7D / 0-14D / 14D+). The `+12.5h / +13.5h` Asia/Kolkata adjust on `call_metadata.timestamp` is kept as-is per user direction ("use same as India, swap the table") — flagged as a knob to turn if US call numbers look wrong.
- **Email funnel for US**: NOT shipped — user didn't have answers for the email-table questions.
- Email/sales/calls remain None on the snapshot row if the query fails (`_safe_query` defaults already cover all the new keys).

### 2026-06-22 — Unified favicons across all pages
- Replaced the 3-line `https://www.interviewkickstart.com/favicon.ico?v=20260621` block in `hub.html:7-9`, `event.html:7-9`, `compare.html:7-9`, `months.html:7-9` with the inline `data:image/x-icon;base64,…` favicon already used by the legacy `Masterclass Automation.html`. All 4 hash-match the original. Removes a CDN dependency, works offline, no flicker.

### 2026-06-22 — Refresh button: fixed silent snapshot failures + cache + alignment
- **Cache-bypass on Refresh** (`event.html:206-218`, `198-200`) — `/events/<id>` has `Cache-Control: private, max-age=60`, so after a refresh the browser served the stale cached payload. Added `load({bypassCache:true})` after a successful refresh; uses `fetch(url, { cache: 'no-store' })`. Deployed as revision `masterclass-automation-00063-ppr`.
- **Aligned daily section with cumulative snapshot** (`server.py:523-536`) — the bento KPI reads `total_regs` from the latest `event_snapshot` row, but the daily section was picking the latest `event_daily` row *per date* across ALL snapshot runs. If a fresh snapshot wrote fewer dates than a previous run, daily sum > bento. Filter `event_daily` to the same `snapshot_at` as the latest snapshot row. Deployed as `masterclass-automation-00064-27h`.
- **Fixed silent `event_snapshot` insert failures** (`server.py:1404-1457`) — `_query_calls_india` was returning `extras: {'call_buckets': {...}}` as a Python dict, but the BQ `extras` column is typed STRUCT, not JSON. Every snapshot insert had been silently failing since the call-lifecycle code shipped, leaving the bento stuck on stale data while daily rows kept growing. Removed `extras` from the call query return; instead wrote the lifecycle metrics directly to the existing top-level columns (`call_total_leads`, `call_pre_attempts`, …, `call_p14p_covered`) that the UI was already trying to read. Also expanded the `_safe_query` default for calls to include all the new keys. Added traceback dump in `_refresh_event` (server.py:794-799) so future failures show up in Cloud Run logs. Deployed as `masterclass-automation-00065-v5v`.

### 2026-06-21 — QA audit + optimization push + Refresh button
- **Deep QA audit**: Read all 5 HTML pages + server.py end to end. Produced `QA_REPORT.md` (782 lines): 34 prioritised findings, 30 verified bugs with file:line citations, full test matrix across auth / security / correctness / UX / ops.
- **Optimization report**: Produced `OPTIMIZATION_REPORT.md` (427 lines): 10 top wins ranked by impact, implementation order over 3 weeks.
- **Implemented 4 of top-5 optimizations**:
  - `ThreadingHTTPServer` swap (server.py:1814).
  - Parallelized 5 snapshot sub-queries for India (`_build_event_snapshot_india`) and 2 for US (`_build_event_snapshot_us`) via `ThreadPoolExecutor`.
  - Fixed `months.html` CPIQL denominator (BUG-21) and call-totals double-count (BUG-22).
  - Wrote `migration_partition_snapshots.sql` for partition + cluster (Optimization #2) — user runs in BQ console.
  - Deferred CTE-consolidation (Optimization #3) with design note in `OPTIMIZATION_DEFERRED.md`.
- **Added per-event Refresh button**: New `POST /events/:id/refresh` endpoint runs `_build_event_snapshot` for one event and writes new snapshot rows. UI button placed in the event hero card above "Goes live", with spinning icon while running.
- **Disabled legacy `_snapshot_to_slack`** in the daily cron — commented in `_do_snapshot`, function definition preserved. Whoever was on `slackSnapshotId` no longer receives the daily DM (manual "Send to Slack" on the legacy Leads tab still works).
- **Discussion about data model**: confirmed append-only snapshot is the right pattern (vs upsert) — BigQuery favours append, storage is effectively free at our scale, and historical snapshots preserve trend data for future features.
- **Deployed**: revision `masterclass-automation-00062-9xj` at 100% traffic. Smoke-tested: hub loads, refresh endpoint gated, new code paths live.

---

## Past-event backfill — format we agreed on

User will run their own query and paste output back. Acceptable shapes: CSV, markdown table, or free-form one-event-per-line. Required fields per event: `title`, `country`, `webinar_type`, `live_date`, `live_time_ist`, `instructor_name`. Optional: `topic`, `event_type`, `instructor_role`, `goal_regs`, `day2_date`.

Once data lands, I:
1. Generate slug `event_id`s
2. Write batched `INSERT INTO events.event`
3. Widen snapshot window if needed for older dates
4. Trigger `POST /run-snapshot`
5. Confirm rows show on `/hub.html` (Past tab)

---

## How to test the full pipeline end-to-end

1. Open `/` (lands on hub). Sign in with `@interviewkickstart.com`.
2. Click "New masterclass" → legacy form.
3. Fill out + click "Launch Event". Watch progress step "Save event to BigQuery" go green with an `event_id`.
4. Refresh `/hub.html`. Event card appears.
5. Click the card → `/event.html?id=…`.
6. Click the **Refresh** button (top-right of hero, above "Goes live"). Wait 5-15s. Page reloads with fresh numbers.
7. Or trigger the cron-style full snapshot:
   ```bash
   curl -X POST https://masterclass-automation-1016538215063.asia-south1.run.app/run-snapshot
   ```
8. Refresh `/hub.html`. Numbers update across all visible events.

---

## User profile

- Name: Utkarsh Gupta
- Role: Performance marketing at Interview Kickstart
- Email: `utkarsh.gupta@interviewkickstart.com` (work), `ug4672@gmail.com` (personal — owns the GCP project)
- Constraints: no GCP admin access at IK; prefers free / low-friction solutions; can't get IT involved easily.
- Working style: trusts Claude to take action ("I'll test later"); fine with autonomous deploys; wants progress documented as we go.
