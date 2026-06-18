-- Masterclass Hub revamp — application data schema
-- Project: masterclass-automation-ik
-- Run once. Safe to re-run (uses IF NOT EXISTS).
--
-- Apply with:
--   bq query --use_legacy_sql=false --project_id=masterclass-automation-ik < schema.sql

-- ─────────────────────────────────────────────────────────────────────────────
-- Dataset: events  (one row per masterclass — created on launch)
-- ─────────────────────────────────────────────────────────────────────────────

-- Note: location must match `history` (which is US). All app-data datasets
-- live in US even though Cloud Run is in asia-south1; cross-region BQ joins
-- aren't supported.
CREATE SCHEMA IF NOT EXISTS `masterclass-automation-ik.events`;

CREATE TABLE IF NOT EXISTS `masterclass-automation-ik.events.event` (
  event_id          STRING NOT NULL,           -- slug, e.g. "ai-agents-claude-sk-jan22"
  title             STRING NOT NULL,
  topic             STRING,                    -- "ai_agents" | "system_design" | "behavioral" | ...
  event_type        STRING,                    -- "masterclass" | "launchpad"
  country           STRING,                    -- "India" | "US"
  webinar_type      STRING,                    -- e.g. "MASTERCLASS_INDIA"
  live_at           TIMESTAMP,                 -- when the event goes live
  day2_live_at      TIMESTAMP,                 -- Launchpad only
  go_live_date      DATE,                      -- marketing go-live (regs open)
  landing_url       STRING,
  zoom_url          STRING,
  instructor_name   STRING,
  instructor_role   STRING,
  summary           STRING,
  design_notes      STRING,
  goal_regs         INT64,                     -- registration target
  status            STRING,                    -- "upcoming" | "live" | "aired" | "archived"
  jira_design_key   STRING,
  jira_landing_key  STRING,
  created_at        TIMESTAMP NOT NULL,
  created_by        STRING,                    -- user email
  updated_at        TIMESTAMP
)
PARTITION BY DATE(live_at)
CLUSTER BY country, status;

-- ─────────────────────────────────────────────────────────────────────────────
-- Dataset: history (already exists)
-- Two tables: event_daily (per-day per-event) + event_snapshot (point-in-time
-- cumulative + cohort + post-event). Both append-only; reads filter to latest
-- per (event_id, registration_date) using QUALIFY ROW_NUMBER().
-- ─────────────────────────────────────────────────────────────────────────────

-- One row per (event_id, registration_date) per cron run.
-- Captures the day-by-day registration + spend curve, same granularity as the
-- old Slack daily snapshot. Cron appends fresh rows each run; reads pick the
-- latest snapshot per day.
CREATE TABLE IF NOT EXISTS `masterclass-automation-ik.history.event_daily` (
  event_id          STRING NOT NULL,
  registration_date DATE NOT NULL,            -- day within the registration window
  snapshot_at       TIMESTAMP NOT NULL,       -- when this row was written

  meta_regs         INT64,
  meta_spend        FLOAT64,
  crm_regs          INT64,
  other_regs        INT64,
  other_spend       FLOAT64,
  total_regs        INT64,
  total_spend       FLOAT64,
  cpiql             FLOAT64,                  -- meta_spend / meta_regs

  extras            JSON                      -- escape hatch
)
PARTITION BY registration_date
CLUSTER BY event_id;

-- One row per event per cron run.
-- Holds cumulative totals + cohort breakdown (role / work-ex) + post-event
-- engagement (attendance, calls, emails). Cumulative totals are also derivable
-- from event_daily but cached here so the detail page reads one row.
CREATE TABLE IF NOT EXISTS `masterclass-automation-ik.history.event_snapshot` (
  event_id          STRING NOT NULL,
  snapshot_at       TIMESTAMP NOT NULL,
  hours_to_live     FLOAT64,                   -- negative if post-event

  -- leads
  total_regs        INT64,
  meta_regs         INT64,
  meta_spend        FLOAT64,
  crm_regs          INT64,
  other_regs        INT64,
  other_spend       FLOAT64,
  cpiql             FLOAT64,

  -- post-event engagement
  attendees         INT64,
  attendance_pct    FLOAT64,
  email_sent        INT64,
  email_delivered   INT64,
  email_opened      INT64,
  email_clicked     INT64,
  calls_attempted   INT64,
  calls_connected   INT64,
  avg_talk_seconds  FLOAT64,

  -- quality breakdown
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
