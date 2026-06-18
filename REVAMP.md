# Masterclass Hub — Revamp

Working doc tracking the rewrite of the Masterclass Automation tool into a card-based hub with persisted event records and a snapshot-driven detail view.

Last updated: 2026-06-15

---

## Context

- **Old tool**: `/Users/utkarshgupta/Documents/Masterclass Automation.html` — 3-tab UI (Create Event / Leads / Webinar Attendance). Forms scoped by date + webinar_type. Stateless: events are not persisted; metrics queried live against `ik-marketing-data` on every view.
- **Mockups** (visual target):
  - `mockup-upcoming.html` — card-based "On the way / Recently aired" grid with KPI strip and bento hero
  - `mockup-detail.html` — per-event page: hero, KPI tiles, lead funnel, quality, attendance, timeline, comparable past events
- **Goal**: replace the tabbed UI with the card grid + detail page, backed by persisted event metadata and daily metric snapshots in BigQuery.

---

## Architecture changes

| Today | After |
|---|---|
| Events are ephemeral (only Jira + Slack side-effects) | Events persisted to `events.event` |
| Metrics queried live on every page load | Daily snapshots in `history.event_snapshot`; live query only as fallback |
| User re-enters date + webinar_type on every tab | Event clicked → all data auto-scoped |
| Configuration shown on every page | Configuration moved behind avatar dropdown |
| 3 raw SQL viewers exposed in UI | Hidden behind a "View SQL" debug link per section |
| `MASTERCLASS_INDIA / _3 / _5` enum exposed | Set once at event creation, never shown again |

---

## Data model

Project: `masterclass-automation-ik`

### `events.event` — one row per masterclass

```sql
CREATE TABLE `masterclass-automation-ik.events.event` (
  event_id          STRING NOT NULL,    -- slug, e.g. "ai-agents-claude-sk-jan22"
  title             STRING NOT NULL,
  topic             STRING,             -- "ai_agents" | "system_design" | "behavioral" | "data" | "ml" | "distributed"
  event_type        STRING,             -- "masterclass" | "launchpad"
  country           STRING,             -- "India" | "US"
  webinar_type      STRING,             -- "MASTERCLASS_INDIA" etc.
  live_at           TIMESTAMP,          -- when event goes live
  day2_live_at      TIMESTAMP,          -- Launchpad only
  go_live_date      DATE,               -- marketing go-live (regs start)
  landing_url       STRING,
  zoom_url          STRING,
  instructor_name   STRING,
  instructor_role   STRING,             -- "Principal Engineer · Stripe"
  summary           STRING,
  design_notes      STRING,
  goal_regs         INT64,              -- registration target
  status            STRING,              -- "upcoming" | "live" | "aired" | "archived"
  jira_design_key   STRING,
  jira_landing_key  STRING,
  created_at        TIMESTAMP NOT NULL,
  created_by        STRING,             -- user email
  updated_at        TIMESTAMP
)
PARTITION BY DATE(live_at)
CLUSTER BY country, status;
```

### `history.event_daily` — one row per (event_id, registration_date) per cron run

Captures the day-by-day spend / regs / CPIQL curve — same granularity as the old Slack daily snapshot table. Append-only; reads filter to latest snapshot per day.

```sql
CREATE TABLE `masterclass-automation-ik.history.event_daily` (
  event_id          STRING NOT NULL,
  registration_date DATE NOT NULL,      -- day in the regs window
  snapshot_at       TIMESTAMP NOT NULL, -- when row was written

  meta_regs         INT64,
  meta_spend        FLOAT64,
  crm_regs          INT64,
  other_regs        INT64,
  other_spend       FLOAT64,
  total_regs        INT64,
  total_spend       FLOAT64,
  cpiql             FLOAT64,            -- meta_spend / meta_regs

  extras            JSON
)
PARTITION BY registration_date
CLUSTER BY event_id;
```

Reads always pick latest per `(event_id, registration_date)`:

```sql
SELECT * FROM `masterclass-automation-ik.history.event_daily`
WHERE event_id = @event_id
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY event_id, registration_date
  ORDER BY snapshot_at DESC
) = 1
ORDER BY registration_date;
```

### `history.event_snapshot` — one row per event per cron run

Cumulative totals + cohort breakdown (role / work-ex) + post-event engagement. Cumulative totals are derivable from `event_daily` but cached here so the detail page renders from a single row.

```sql
CREATE TABLE `masterclass-automation-ik.history.event_snapshot` (
  event_id          STRING NOT NULL,
  snapshot_at       TIMESTAMP NOT NULL,
  hours_to_live     FLOAT64,            -- negative if post-event

  -- leads
  total_regs        INT64,
  meta_regs         INT64,
  meta_spend        FLOAT64,
  crm_regs          INT64,
  other_regs        INT64,
  other_spend       FLOAT64,
  cpiql             FLOAT64,

  -- post-event
  attendees         INT64,
  attendance_pct    FLOAT64,
  email_sent        INT64,
  email_delivered   INT64,
  email_opened      INT64,
  email_clicked     INT64,
  calls_attempted   INT64,
  calls_connected   INT64,
  avg_talk_seconds  FLOAT64,

  -- quality
  role_sde          INT64,
  role_ml           INT64,
  role_fe           INT64,
  role_other        INT64,
  we_0_2            INT64,
  we_3_5            INT64,
  we_6_10           INT64,
  we_10p            INT64,

  -- escape hatch for experimental metrics
  extras            JSON
)
PARTITION BY DATE(snapshot_at)
CLUSTER BY event_id;
```

---

## Endpoints

| Method | Path | Status | Purpose |
|---|---|---|---|
| `POST` | `/events` | **slice 1** | Persist new event on create |
| `GET` | `/events` | slice 3 | List for card grid |
| `GET` | `/events/:id` | slice 3 | Detail page payload |
| `POST` | `/run-snapshot` | exists — **extend in slice 2** | Currently Slack only; add `event_snapshot` insert |

Auth on all new endpoints reuses the existing `ik_session` cookie (same gate as the current proxy endpoints).

---

## Rollout plan

| Slice | Status | What |
|---|---|---|
| 1 — Data layer | ✅ shipped (verify next event creation) | Tables + `POST /events` + wire Launch Event button |
| 2 — Snapshot writer | ✅ shipped + verified end-to-end | `_snapshot_events_to_bq` writes `event_daily` + `event_snapshot` per active event |
| 2 v2 — Post-event metrics + cohort | ✅ shipped + verified | Attendance, calls, email funnel, role + work-ex cohort. India only. |
| 3 — Read endpoints + dynamic pages | ✅ shipped | `GET /events`, `GET /events/:id`, `hub.html`, `event.html` |
| 4 — Cutover (soft) | ✅ shipped | `/` redirects to `/hub.html`; legacy tabbed UI still reachable at `/Masterclass%20Automation.html` |

---

## Decisions log

- **2026-06-15 — event_id format**: slug, format `{topic}-{instructor-initials}-{mmm-dd}` (e.g. `ai-agents-sk-jan22`). Generated at create time, immutable. User-editable only via settings panel later.
- **2026-06-15 — No backfill** of existing in-flight events. Tracking starts forward.
- **2026-06-15 — JSON `extras` column** on snapshot table for experimental metrics. Promote to typed columns once stable.
- **2026-06-15 — Day-by-day granularity preserved**: added `history.event_daily` separate from `event_snapshot`. `event_daily` holds per-day spend/regs/CPIQL (the table the old Slack snapshot showed); `event_snapshot` holds cumulative + cohort + post-event metrics. Reasoning: forcing both shapes into one table makes most reads scan rows they don't need.
- **2026-06-15 — Defaults applied** where user hasn't confirmed: any of these can be reversed; flag them in the next session if you disagree.

---

## Open questions

- _(none currently — speak up if any decision above feels wrong)_

---

## Progress log

### 2026-06-15 — OAuth client switched to the workspace one (revision -00020-vml)

The `1016538215063-…` client (which I had been using) lives in the personal project `masterclass-automation-ik` and is configured as External + Testing — so any IK user not on the test-user list would get blocked, which triggers IT review.

Switched all three HTML files + the Cloud Run env var to `801560849080-lgdgif7rjbgap4fqqp0cbcj7duc2ed0o.apps.googleusercontent.com`, which lives in `masterclass-499210` (inside the interviewkickstart.com Workspace org). Set to Internal, this works for any IK user with zero IT touch.

**Investigated but didn't migrate the deployment**: the work account has Owner on `masterclass-499210` but no billing accounts attached — can't enable Cloud Run there without IT. Decoupling OAuth client (in workspace project) from Cloud Run hosting (in personal project) is the pragmatic compromise — works correctly with no infrastructure changes.

**User action required**: verify consent screen for the `801560849080` client is "Internal" at https://console.cloud.google.com/apis/credentials/consent?project=masterclass-499210 . If it's External, one click to flip it.

### 2026-06-15 — Slices 2 v2 + 3 + 4 deployed (revision -00017-qlh)

**Slice 2 v2 — post-event + cohort metrics**

Extended `_build_event_snapshot` to run 4 queries per India event (wrapped in `_safe_query` so a single failure doesn't kill the snapshot row):
- `_query_cohort_india`: role category (SDE / ML / Other; FE folded into SDE per source bucket) + work-ex (3-5 / 6-10 / 10+; 0-2 always 0 because source pre-filters student/0-2/3-4 leads)
- `_query_attendance_india`: joins lead view with `Webinar_analytics.webinar_attendee_data_from_zoom`
- `_query_calls_india`: joins lead view with `Marketing_data_new_logic.call_metadata` (call_id-deduped, IST timezone handling for daylight savings)
- `_query_emails_india`: 6-CTE funnel against `Email.Marketing_Email_Data` covering reminder campaigns (SENT/DELIVERED/OPEN/CLICK)

Verified against `test-snapshot-jun11`: 1,156 regs, 539 SDE, 244 ML, 381 Other, work-ex split 0/276/888, 398 attendees @ 34.3%, emails 2962/1046/590/69, calls 335/49 @ 383.6s avg talk.

**Slice 3 — read endpoints + dynamic pages**

- `GET /events?status=upcoming|aired&country=<C>` — list events for the grid, joined with latest snapshot.
- `GET /events/:id` — full payload: event metadata, latest snapshot, daily timeline (latest per registration_date via QUALIFY ROW_NUMBER), 3 comparable past events (same country, status != archived, live_at < now).
- Added `_row_to_dict` helper (BQ Row → JSON-safe dict, timestamps→ISO).
- `hub.html` — dynamic upcoming/aired grid. Tabs toggle re-fetches. KPI strip aggregates total regs, goal %, avg attendance, "need attention" count. Hero card + 2 medium + N small cards. Empty state for fresh installs.
- `event.html` — full detail page. Hero with countdown + topic chip + Join Zoom / Landing actions. KPI tiles adapt for upcoming vs aired. Lead Funnel with Meta/CRM/Other bars + CPIQL per channel. Lead Quality role + work-ex bars. Attendance section shows placeholder tiles when not yet aired, real metrics when aired. Daily pacing table with bar chart. Right rail: event details, quick links, comparable past events.
- Both pages share the existing Google Sign-In overlay, session cookie, auth check pattern. Topic color theming via `--topic` CSS var on body.

**Slice 4 — soft cutover**

- Server `GET /` and `HEAD /` redirect (302) to `/hub.html`.
- Legacy tabbed UI still reachable at `/Masterclass%20Automation.html` (no breakage for existing bookmarks).
- Hub header: avatar menu has "Old tabbed UI" + "Settings (legacy)" links pointing back to legacy URL.

**Smoke tests (all pass on revision -00017-qlh):**
- `GET /` → 302 → /hub.html ✓
- `GET /hub.html` → 200 ✓
- `GET /event.html` → 200 ✓
- `GET /events` → 401 (auth gate works) ✓
- `GET /events/foo` → 401 ✓
- `GET /Masterclass%20Automation.html` → 200 (legacy still works) ✓

**Gotcha hit + fixed:**
- Dockerfile only `COPY`s specific files (`server.py` + `Masterclass Automation.html`). Forgot to add new pages → first deploy 404'd on `/hub.html`. Added `COPY hub.html .` + `COPY event.html .` and redeployed.

**Open items / explicit non-goals for v1:**
- US event support in the snapshot writer (different lead/spend tables — `Bq_data_Alumni`, `Combined_Spend_data`). Skipped, easy to add.
- `mockup-upcoming.html` + `mockup-detail.html` left untouched as visual design reference. Safe to delete later.
- "New masterclass" button on hub still points to legacy form. Reskinning the Create Event form is a future slice (call it 5).
- Search input in mockup wasn't ported (search needs server-side query).
- Pacing logic on hub cards is crude (`hours_to_live < 72 && ratio < 0.5` → behind). Refine when there's real cross-event data.

### 2026-06-15 — Slice 2 deployed + verified

**Files changed:**
- `server.py` — refactored `_do_snapshot` to call `_snapshot_to_slack` + new `_snapshot_events_to_bq` independently (failure of one doesn't block the other). Added `_build_event_snapshot()` which queries `ik-marketing-data.India_Leads.US_Domain_combined_view` + `Combined_India_Spend` per event, buckets per-day by channel class (Meta / CRM / Other), writes rows to `history.event_daily` (per-day) + `history.event_snapshot` (cumulative).
- Deployed: revision `masterclass-automation-00014-96x` serving 100%.

**Verification:**
Seeded test event `test-snapshot-jun11` matching the current Slack snapshot config (MASTERCLASS_INDIA_3 on 2026-06-11). Triggered `POST /run-snapshot`. Result:
- `event_daily`: 8 rows (May 26 → Jun 11), per-day Meta/CRM/Other split with spend + CPIQL populated.
- `event_snapshot`: 1 cumulative row — total_regs=1154, meta_spend=₹194,234, cpiql=₹181.53.

Test event then archived so it stops getting snapshotted by future cron runs. The 8 daily rows + 1 snapshot row remain as evidence.

**Gotcha hit + fixed (worth remembering):**

The `bq-credentials` secret authenticates as **`utkarsh.gupta@interviewkickstart.com`** (work account), NOT `ug4672@gmail.com` (project owner). That account had BigQuery access on `ik-marketing-data` (so the existing daily Slack snapshot worked, since the old code passed `project='ik-marketing-data'`) but no roles on `masterclass-automation-ik`. New code queries the personal project → 403.

Fix applied (project-wide, on masterclass-automation-ik, for `utkarsh.gupta@interviewkickstart.com`):
- `roles/bigquery.user` (jobs.create + read)
- `roles/bigquery.dataEditor` (insert into events.event + history.*)
- `roles/storage.objectUser` (GCS snapshot config read/write)

**v1 scope limits (deferred to v2):**
- India events only (US events skipped — different lead/spend tables)
- Lead funnel only (attendance / calls / email funnel / cohort role+work-ex columns written as NULL)
- Append-only writes; duplicate snapshots aren't deduped (Cloud Scheduler runs once/day so not an issue in practice)

**Operationally:**
- Daily cron at 11 AM IST (Cloud Scheduler `masterclass-daily-snapshot`) now writes per-event rows automatically — no manual trigger needed.
- Existing Slack DM still goes out (legacy `_snapshot_to_slack` path); will be replaced in slice 3+.

### 2026-06-15 — Slice 1 deployed

- Schema applied: `bq query --use_legacy_sql=false --location=US --project_id=masterclass-automation-ik < schema.sql`. All 3 tables created in US (matching `history` location).
- Cloud Run redeployed: `masterclass-automation-00013-xw4` is now serving 100%.
- Smoke test: `POST /events` returns 401 unauth (route registered + auth gate working).
- **Awaiting user-side verification**: create a throwaway event in the live tool → expect the "Save event to BigQuery" step to light up green with an `event_id`. Then check `SELECT * FROM masterclass-automation-ik.events.event ORDER BY created_at DESC LIMIT 5`.

**Gotchas hit during deploy (worth remembering for next slice):**

- `history` dataset is in **US**, not `asia-south1`. Cloud Run is `asia-south1`, but cross-region BQ joins aren't supported, so all app data lives in US. Schema's CREATE SCHEMA originally specified `asia-south1` → got rejected. Fixed.
- The `bq-credentials` secret holds **user ADC credentials** (type=`authorized_user`) — not a service account. Means Cloud Run queries run as the project owner (full access by default). No IAM grant step needed.
- `bq query` runs multi-statement scripts in a single execution; the first failed run created `events` dataset in the wrong region. Had to `bq rm -r -f -d events` and recreate.

### 2026-06-15 — Added `event_daily` table to schema (pre-deploy)

User flagged that we'd be losing the day-by-day spend/regs/CPIQL granularity from the old Slack daily snapshot. Added `history.event_daily` to `schema.sql` before slice 1 runs — keeps the schema migration to one go.

### 2026-06-15 — Slice 1 code complete (not yet deployed)

**Files changed:**
- `schema.sql` (new) — `CREATE TABLE` for `events.event` + `history.event_snapshot`, plus `CREATE SCHEMA events` in `asia-south1`.
- `server.py` — added `BQ_APP_PROJECT` constant, `_generate_event_id()` helper, `POST /events` handler routed inside `do_POST`. Imports updated (`re`).
- `Masterclass Automation.html` — added `save` step to the progress UI; `handleSubmit()` now calls `POST /events` after both Jira tickets succeed, before Slack DMs. Persistence failure is non-blocking (Slack DMs still go out).

**To finish slice 1, you need to (in order):**

1. **Run the schema** against `masterclass-automation-ik`:
   ```
   bq query --use_legacy_sql=false --project_id=masterclass-automation-ik < /Users/utkarshgupta/Documents/schema.sql
   ```
   (Or paste into the BQ console one CREATE at a time.)

2. **Grant the SA write permission** on the new dataset. The Cloud Run service uses creds from Secret Manager `bq-credentials`. That SA already reads `ik-marketing-data` (source data) but probably can't write to our app project. It needs `roles/bigquery.dataEditor` on dataset `masterclass-automation-ik.events` (and on `history` if it doesn't already have it).

   Grant via console: BigQuery → events dataset → Sharing → Add principal → the SA email → BigQuery Data Editor.

3. **Redeploy Cloud Run** with the canonical command from memory:
   ```
   cd "/Users/utkarshgupta/Documents" && gcloud run deploy masterclass-automation --source . --project=masterclass-automation-ik --region=asia-south1 --allow-unauthenticated --set-env-vars="GCS_BUCKET=masterclass-snapshot-config-ik,GOOGLE_APPLICATION_CREDENTIALS=/secrets/bq-creds.json,GOOGLE_CLIENT_ID=1016538215063-i20h0j7f5a5g42ktql1l5cs5q429c8vb.apps.googleusercontent.com,SESSION_SECRET=8128cb8bcbf74b2376e522c5b7a07e74d2c48967e7272f85242f19640e0a584c" --set-secrets="/secrets/bq-creds.json=bq-credentials:latest" --memory=512Mi --min-instances=1 --quiet
   ```

4. **Verify** by creating a throwaway event from the live tool. Expect:
   - 3rd step "Save event to BigQuery" lights up with an `event_id`.
   - `SELECT * FROM masterclass-automation-ik.events.event ORDER BY created_at DESC LIMIT 5` shows the row.

**Local test (optional, before deploy):**
```
cd "/Users/utkarshgupta/Documents" && python3 server.py
# in another shell, after signing in via http://localhost:8080/Masterclass%20Automation.html:
curl -X POST http://localhost:8080/events \
  -H "Content-Type: application/json" \
  -b "ik_session=<paste from browser devtools>" \
  -d '{"title":"Test event","instructorName":"Test User","liveAt":"2026-07-01T19:00:00+05:30","country":"India","webinarType":"MASTERCLASS_INDIA"}'
# expect: {"ok": true, "event_id": "test-event-tu-jul01"}
```

### 2026-06-15 — Slice 1 kickoff

- Created this doc
- Read current `server.py` (375 lines) — auth gate is `_session_email()`, routing in `do_POST` is a simple if/elif chain (easy to extend), BQ client uses ADC from Secret Manager mount
- Read `handleSubmit()` in HTML to find the insertion point — chose "after Jira ticket 2 succeeds, before Slack DMs" so the row has both Jira keys captured but Slack failures (later) can't lose the persisted event
