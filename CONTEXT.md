# Masterclass Hub — Project Context

> **To continue in a new conversation**: hand Claude this file at the start. Say "Read `/Users/utkarshgupta/Documents/CONTEXT.md` and we'll continue from there." Last updated: 2026-06-18.

---

## TL;DR

Internal marketing-automation tool for **Interview Kickstart**, owned by **Utkarsh Gupta (performance marketing)**. Tracks masterclass + launchpad events end-to-end — creates Jira tickets + Slack DMs on launch, snapshots BigQuery metrics daily, and surfaces it all in a card-based hub. Deployed on Cloud Run, gated by Google Sign-In for `@interviewkickstart.com`.

**Current state**: revamp is shipped and live. The original 3-tab tool (`Masterclass Automation.html`) still works for "Create Event"; the new card-based UI (`hub.html` + `event.html`) is the default landing page. Schema is deployed, daily cron writes per-event snapshots automatically.

---

## Live URLs

| URL | What |
|---|---|
| `https://masterclass-automation-1016538215063.asia-south1.run.app/` | Redirects to `/hub.html` |
| `…/hub.html` | New card grid: "On the way" / "Recently aired" |
| `…/event.html?id=<event_id>` | Per-event detail page |
| `…/Masterclass%20Automation.html` | Legacy 3-tab UI (Create Event / Leads / Webinar Attendance) |

The `1016538215063` in the URL is the GCP project number; can't be stripped from auto-generated `.run.app` URLs. Options to clean it up (custom domain / URL shortener / GitHub Pages redirect) discussed — user hasn't picked yet.

---

## File map (all in `/Users/utkarshgupta/Documents/`)

| File | Purpose |
|---|---|
| `server.py` | Python HTTP server. Routes, auth, BQ queries, snapshot writer. ~700 lines. |
| `Masterclass Automation.html` | Legacy 3-tab UI. Mobile-responsive as of 2026-06-17. |
| `hub.html` | New dynamic card grid. Fetches `/events?status=…`. |
| `event.html` | New dynamic detail page. Fetches `/events/:id`. |
| `mockup-upcoming.html` | Original static design mockup for hub (visual reference only). |
| `mockup-detail.html` | Original static design mockup for detail (visual reference only). |
| `schema.sql` | DDL for 3 BQ tables. Already applied. Safe to re-run. |
| `Dockerfile` | Selectively `COPY`s files — must add new HTML files explicitly. |
| `requirements.txt` | `google-cloud-bigquery`, `google-cloud-storage`. |
| `.gcloudignore` | Limits source upload size. |
| `REVAMP.md` | Detailed slice-by-slice progress log. |
| `CONTEXT.md` | This file. |

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
│  server.py (http.server.SimpleHTTPRequestHandler)    │
└───┬──────────────────────────────────────────────────┘
    │
    ├──► Jira REST API (creates tickets on Create Event)
    ├──► Slack API (DMs perf + crm leads; daily snapshot)
    │
    ├──► BigQuery: ik-marketing-data (READ — source data)
    │       India_Leads.US_Domain_combined_view (leads)
    │       India_Leads.Combined_India_Spend (spend)
    │       Webinar_analytics.webinar_attendee_data_from_zoom
    │       Marketing_data_new_logic.call_metadata
    │       Email.Marketing_Email_Data
    │
    ├──► BigQuery: masterclass-automation-ik (WRITE — app data)
    │       events.event           ← one row per masterclass
    │       history.event_daily    ← per (event, registration_date) per cron run
    │       history.event_snapshot ← per event per cron run (cumulative)
    │
    ├──► GCS: masterclass-snapshot-config-ik (snapshot config JSON)
    └──► Secret Manager: bq-credentials (user ADC)

Cloud Scheduler: masterclass-daily-snapshot
  → POST /run-snapshot daily at 5:30 UTC (11 AM IST)
  → Writes Slack DM + per-event BQ snapshots
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

All three tables live in `masterclass-automation-ik`, US region. Schema source of truth: `schema.sql`.

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
role_sde, role_ml, role_fe, role_other     -- role_fe always NULL (source SE bucket includes FE)
we_0_2, we_3_5, we_6_10, we_10p             -- we_0_2 always 0 (source filters 0-2/3-4/student)
extras            JSON
PARTITION BY DATE(snapshot_at)
CLUSTER BY event_id
```

**Schema evolution rule of thumb**: `ALTER TABLE ADD COLUMN` is free + instant. Pick `FLOAT64` over `INT64` when ratios are possible. Use `extras JSON` for experimental metrics, promote later.

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
| `GET` | `/hub.html` | static | New card grid |
| `GET` | `/event.html?id=…` | static | New detail page |
| `GET` | `/Masterclass%20Automation.html` | static | Legacy UI |
| `GET` | `/auth/check` | — | Returns 200 + email if session valid, else 401 |
| `POST` | `/auth/verify` | — | Verifies Google ID token, sets `ik_session` cookie |
| `POST` | `/auth/logout` | — | Clears cookie |
| `POST` | `/proxy?url=…` | session | Jira forwarder (legacy Create Event flow) |
| `POST` | `/bigquery` | session | Arbitrary BQ query (legacy Leads / Attendance tabs) |
| `POST` | `/save-snapshot-config` | session | Updates GCS snapshot_config.json |
| `POST` | `/events` | session | Persist a new event row (called from Launch Event flow) |
| `GET` | `/events?status=upcoming\|aired&country=…&limit=…` | session | List events for hub grid |
| `GET` | `/events/:id` | session | Event detail payload |
| `POST` | `/run-snapshot` | none (cron) | Slack DM + per-event BQ snapshots |

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

---

## Decisions made

| Decision | Why |
|---|---|
| Slug `event_id` (`<title>-<initials>-<mmm-dd>`) over UUID | Readable in URLs, Slack, logs. Generated at create time, immutable. |
| Two snapshot tables (`event_daily` + `event_snapshot`) | Different query shapes — per-day curve vs. cumulative-now. Forcing both into one table wastes scan cost. |
| Append-only snapshot writes; read latest via `QUALIFY ROW_NUMBER()` | Simpler than DML MERGE. BQ partition pruning keeps it cheap. |
| `extras JSON` column on snapshot tables | Lets us add experimental metrics without schema migration. Promote to typed columns once they stabilize. |
| Snapshot writer is India-only for now | US uses different lead/spend tables (`Bq_data_Alumni`, `Combined_Spend_data`). Deferred. |
| `role_fe` schema column always NULL | Source role mapping puts Front-end inside Software Engineer bucket — there's no separate FE bucket to populate it from. |
| `we_0_2` always 0 | Source query filters out work_ex IN ('0-2','3-4') and students — they're not target audience. Documented, not a bug. |
| Failure of one snapshot path doesn't kill the other | `_do_snapshot` wraps Slack DM and BQ writes independently. Cron job continues if one fails. |
| OAuth client in workspace project; Cloud Run in personal project | OAuth needs to be in IK Workspace for Internal sign-in (no IT review). Personal project has the only billing account I have access to. Decoupled. |
| `/` redirects to `/hub.html`; legacy URL still works | Soft cutover. Existing bookmarks unaffected. |
| Mobile responsive only added to legacy tool so far | User flagged it. Hub + Event pages still desktop-tuned. |

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
   - I bounced between them — `801…` is correct and is what's currently in all 3 HTML files + the env var.

7. **`HEAD /` returns 200, not 302** unless `do_HEAD` is overridden — `SimpleHTTPRequestHandler`'s default `do_HEAD` doesn't go through my `do_GET` logic. Fixed.

8. **Streaming inserts vs DML INSERT in BQ**: I use `insert_rows_json` (streaming). Rows are immediately queryable via SELECT but not editable via DML for ~90 min. Fine for our writes; just don't try to `UPDATE` a just-inserted row.

---

## Known limitations / open items

| Item | Status | Notes |
|---|---|---|
| US event support in snapshot writer | Open | Needs `Bq_data_Alumni` + `Combined_Spend_data` queries. India works. |
| Reskin "Create masterclass" form to match new visual | Open | Hub's "New masterclass" button currently dumps users into legacy form. |
| Pacing logic on hub cards | Crude | Current rule: `hours_to_live < 72 && ratio < 0.5` → behind. Needs `go_live_date` elapsed-time math for proper "expected by now" comparison. |
| Search input on hub | Not wired | Static input in mockup; no backend support yet. |
| Mobile responsive — `hub.html` + `event.html` | Open | Only legacy tool is mobile-tuned. Bento grid + 4-tile KPI strip will cramp on phones. |
| Past-event backfill | In progress | User is planning to run a query against source data + paste output for me to insert into `events.event`. Format spec given (CSV / markdown table / free-form). |
| Backfill snapshots for older events | Untouched | Current window is -14d to +60d of `live_at`. Older events won't get snapshotted automatically. Either widen window or add `?backfill=1` endpoint. |
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

### 2026-06-18 — URL cleanup + this doc
- User asked how to remove `1016538215063` from URL. Discussed 3 options (custom domain / shortener / GH Pages redirect). No decision yet.
- Created this CONTEXT.md.

---

## Past-event backfill — format we agreed on

User will run their own query and paste output back. Acceptable shapes: CSV, markdown table, or free-form one-event-per-line. Required fields per event: `title`, `country`, `webinar_type`, `live_date`, `live_time_ist`, `instructor_name`. Optional: `topic`, `event_type`, `instructor_role`, `goal_regs`, `day2_date`.

Once data lands, I:
1. Generate slug `event_id`s
2. Write batched `INSERT INTO events.event`
3. Widen snapshot window if needed for older dates
4. Trigger `POST /run-snapshot`
5. Confirm rows show on `/hub.html` (Recently aired)

---

## How to test the full pipeline end-to-end

1. Open `/` (lands on hub). Sign in with `@interviewkickstart.com`.
2. Click "New masterclass" → legacy form.
3. Fill out + click "Launch Event". Watch 3rd progress step "Save event to BigQuery" go green with an `event_id`.
4. Trigger snapshot:
   ```bash
   curl -X POST https://masterclass-automation-1016538215063.asia-south1.run.app/run-snapshot
   ```
5. Refresh `/hub.html`. Event card appears with real metrics.
6. Click the card → `/event.html?id=…` shows funnel, quality, daily pacing.

---

## User profile

- Name: Utkarsh Gupta
- Role: Performance marketing at Interview Kickstart
- Email: `utkarsh.gupta@interviewkickstart.com` (work), `ug4672@gmail.com` (personal — owns the GCP project)
- Constraints: no GCP admin access at IK; prefers free / low-friction solutions; can't get IT involved easily.
- Working style: trusts Claude to take action ("I'll test later"); fine with autonomous deploys; wants progress documented as we go.
