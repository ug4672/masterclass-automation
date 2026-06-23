# QA Audit — Masterclass Automation Tool

**Scope:** Production code at `/Users/utkarshgupta/Documents/` — `server.py` (1814 lines), `hub.html`, `event.html`, `compare.html`, `months.html`, `Masterclass Automation.html` (legacy, 2548 lines), `Dockerfile`, `requirements.txt`, `.gcloudignore`.
**Live URL:** https://masterclass-automation-1016538215063.asia-south1.run.app/
**Method:** Adversarial static read of every endpoint, every form field, every BigQuery query, every external API call. Severity bar: anything that lets a non-IK user sign in, read data, or trigger Jira/Slack = **Blocker**; numeric error >10% = **Blocker**; ₹1 rounding = **Med**.

⚠ This is a static-only audit. Items marked "verify live" need browser/network/BQ verification. Items marked "code-confirmed" are reproducible from the source.

---

## 1. Executive summary — top issues by severity

| # | Sev | Area | One-line repro |
|---|-----|------|----------------|
| 1 | **Blocker** | `/run-snapshot` is unauthenticated *and* calls expensive BQ work | `curl -X POST https://…/run-snapshot?days_back=365` from anywhere triggers a full snapshot run; no Cloud Scheduler OIDC check (server.py:176, 729) |
| 2 | **Blocker** | `/save-snapshot-config` overwrites a single global config; any signed-in user can hijack the daily cron's Slack DM target and webinar type, and inject SQL into the cron's query | Logged-in user A POSTs `{"webinarDates":["x' UNION SELECT …"], "webinarType":"y'; --", "slackToken":"…", "slackSnapshotId":"U-attacker"}` → next 5:30 UTC cron runs attacker SQL with the service account's BQ permissions and DMs attacker (server.py:880-924, MA HTML:1445) |
| 3 | **Blocker** | Slack bot token + Jira API token stored in `localStorage` and submitted on every action; readable by any XSS or shared browser | MA HTML:780-806 stores `jiraToken`, `slackToken` plaintext under key `ik_mc_launch_v1`; persists across sessions for any IK user using the same machine |
| 4 | **Blocker** | `/bigquery` accepts an **arbitrary SQL string** from the client; uses server-held service-account creds, scoped to `ik-marketing-data` — anyone signed in can run any read query, including PII tables outside the marketing scope of this tool | MA HTML:1411-1422 calls `/bigquery` with the textarea's current value; the textarea is editable. Server runs it as-is (server.py:245-271) |
| 5 | **Blocker** | `_snapshot_to_slack` interpolates user-controlled `webinarDates` and `webinarType` directly into SQL → SQL injection in the daily cron | server.py:886-902; the values come from `save_snapshot_config()` which trusts the JSON body |
| 6 | **High** | Time zone applied for *all* `live_at` rendering is **hardcoded `Asia/Kolkata`**, including US events. US masterclasses display an IST time labelled "IST", off by 12–13h | hub.html:284, hub.html:609, event.html:132 — `fmtTime`/`fmtDateTime` set `timeZone:'Asia/Kolkata'` regardless of `ev.country` |
| 7 | **High** | Create-Event form persists `liveAt` with hard-coded `+05:30` even for `country=US` — the event's stored UTC time will be wrong by the IST↔PT delta (~12.5h), corrupting every downstream snapshot date filter | MA HTML:1167-1168 — `(f.mcDate + 'T' + f.mcTime + ':00+05:30')` unconditionally |
| 8 | **High** | Launchpad event drops 3 of 4 instructors on save | MA HTML:1018-1024, 1171 — `instructorName: f.speaker` = `speakers[0]` only; speakers 2–4 vanish in BQ |
| 9 | **High** | `handleSubmit` flow partial-failure: Jira ticket 1 succeeds → Jira ticket 2 fails → user fixes config → resubmits → creates a *second* Jira ticket 1; no idempotency or rollback | MA HTML:1115-1126 returns on error without cleanup; ticket 1 is left orphan |
| 10 | **High** | Hub "Active spend (2w)" KPI / "Months" page CPIQL / month aggregation all sum cross-event spend without joining to the per-event date — uses event-level snapshot fields directly. For pipeline-open events this includes only what has been snapshotted so far, undercounting | hub.html:418-422, months.html:378 |
| 11 | **High** | `/proxy` is an open-ended HTTP proxy for any signed-in user — Authorization header is forwarded, no URL allow-list. SSRF: a user can hit `http://169.254.169.254/computeMetadata/v1/` (GCE metadata) or other internal endpoints if running on GCE, or any internet URL with the server as the source IP | server.py:210-243; the `url` query param is unrestricted |
| 12 | **High** | `Set-Cookie: ik_session=…` is missing the **`Secure`** flag — over HTTP a session cookie can leak; in cookie-policy hardening this is a baseline | server.py:759, 769 — only `HttpOnly; SameSite=Strict` are set |
| 13 | **High** | `_create_event` allows duplicate `event_id` writes because it uses `insert_rows_json` (streaming insert) — no UPSERT, no PK enforcement. Two events created the same day with same title + instructor + date → identical `event_id`, two rows in `events.event`, snapshot joins double-count | server.py:539-588 + `_generate_event_id`:87-108 |
| 14 | **High** | `/events/<id>` lets you exfiltrate any event by guessing/scraping its slug; no per-user authorisation. Every IK staffer sees every event, including drafts of past quarters. (Could be desired; flag for product) | server.py:495-538 — only `_session_email` check |
| 15 | **High** | Compare page silently drops the "winner" highlight when only 1 of N events has a value — that single value wins by default, misleading the reader into thinking it beat real competitors | compare.html:369-387 |
| 16 | **Med** | months.html computes its own monthly aggregation from `/events?status=aired` client-side instead of using the server's `/months` endpoint (which does proper TZ bucketing and blended CPL). Drift between Months page and the server-side aggregation | months.html:321-431 vs server.py:388-493 |
| 17 | **Med** | months.html "Coverage Gap" insight is a hard-coded sentence about `MASTERCLASS_INDIA_2` with a fabricated `~₹1.5 Cr` figure — not data-driven | months.html:548 |
| 18 | **Med** | event.html `linkRow(_,_,_, isKey=true)` (Jira links) sets `href="#"` with `target="_blank"` — clicking the Jira link opens about:blank instead of the ticket | event.html:776-789 (uses key like `MC-123` not URL; never resolved against `jiraBaseUrl`) |
| 19 | **Med** | "Avatar click = sign out" on event.html — single-click sign-out with no menu, no confirmation; easy accidental logout | event.html:54 `onclick="signOut()"` (other pages use a dropdown) |
| 20 | **Med** | Hub "What we learned" and table sorts treat null-ROAS events as 0 — events with no snapshot can appear at the bottom of the Bottom-3 list, falsely tagged as poor performers | hub.html:736-738, 833-838 |
| 21 | **Med** | event.html "Open Polls/Q&A" download falls back to fuzzy substring match on `webinar_topic LIKE '%keyword%'` with a ±1 day window; multiple events with similar titles on adjacent days will collide; ±1d window can pull next event's data | server.py:599-728 — keyword from first 40 chars of title before em-dash |
| 22 | **Med** | `requirements.txt` is unpinned (`google-cloud-bigquery\ngoogle-cloud-storage\nopenpyxl`) — every Cloud Run build pulls latest. Breaks reproducibility and exposes silent CVE drift | requirements.txt:1-3 |
| 23 | **Med** | `google-auth` is not declared in requirements.txt but the code imports `google.auth.transport.requests` (server.py:747) — works only because it's a transitive dep of `google-cloud-bigquery`. Bumping that lib could break sign-in | server.py:747-748 |
| 24 | **Med** | India spend table dedup logic in `_snapshot_to_slack` (legacy path) uses **date-only dedup** — keeps only the first matching campaign per date, drops the rest. Other India events' spends are silently lost when multiple campaigns ran on the same day | server.py:816-826 |
| 25 | **Med** | In‐browser `runQuery` re-POSTs the entire `slackToken` to `/save-snapshot-config` *every time the user just clicks Run* — even a read-only metrics check overwrites the cron's config. Two team members alternating runs will fight over whose Slack ID receives the next snapshot | MA HTML:1440-1454 |
| 26 | **Med** | `slackToken` written into GCS `snapshot_config.json` plaintext under bucket `masterclass-snapshot-config-ik`; anyone with read on that bucket can grab a working Slack bot token. Tokens are not rotated on user change | server.py:792-799 |
| 27 | **Med** | Hub.html `fmtN` uses Indian-style `Cr`/`L` suffix for **all** countries — US ROAS dashboards show `1.5L` of revenue instead of `$150k` | hub.html:298-304 |
| 28 | **Med** | Compare cohort denominator excludes `role_null` bucket; percentages don't sum to 100% and the "Null" cohort silently disappears from the comparison | compare.html:444-447 |
| 29 | **Med** | `/events?ids=…` accepts up to `len(id_list)` events but the route's `limit` minimum applies only to non-id path; an attacker can dump thousands of events by passing 5k IDs | server.py:286-292 |
| 30 | **Med** | `_get_event` rejects `/` in event_id (good) but allows `..`, `;`, query-string fragments — only PK match in BQ saves it; defense-in-depth missing | server.py:497 |
| 31 | **Low** | `_generate_event_id` is locale-naive: a title in Hindi/Chinese yields `event-` with no other slug part, so multiple events of the same instructor/date collide | server.py:87-108 |
| 32 | **Low** | All 5 HTML pages bundle Tailwind CDN (`cdn.tailwindcss.com`), Inter via Google Fonts/rsms.me, Chart.js (months) — no SRI hashes, no fallback. Tool is dead if any CDN is hijacked | hub.html:15, etc. |
| 33 | **Low** | Daily snapshot thread runs in a `while True / time.sleep(60)` loop *in addition* to the Cloud Scheduler hitting `/run-snapshot` — double-firing once a day on every Cloud Run instance ≥ 1 (min-instances=1, but if it scales to 2, you get 2 snapshots). Idempotency unverified | server.py:1797-1811 |
| 34 | **Low** | `runQuery` button label flips to "■ Stop" with `btn.classList.replace('btn-primary','btn-danger')` mid-run; on success the inverse runs — but if the page re-renders mid-flight (e.g., theme toggle), the button is stuck in danger state | MA HTML:1456-1640 |

---

## 2. Surface inventory

### 2.1 Server endpoints (`server.py`)

| Path | Method | Auth | Purpose | Risk hotspots |
|------|--------|------|---------|---------------|
| `/` `/hub.html` | GET | none (HTML) | Static HTML; `/` 302→`/hub.html` (server.py:117-131) | static; safe |
| `/auth/verify` | POST | none | Verifies Google ID token, sets `ik_session` cookie | server.py:738-763 — cookie missing `Secure` |
| `/auth/check` | GET | none | Returns `{ok, email}` if cookie valid, else 401 | safe |
| `/auth/logout` | POST | none | Clears cookie (`Max-Age=0`) | safe |
| `/proxy?url=…` | POST | session | Forwards POST to arbitrary URL with caller's headers | **SSRF risk** (#11) |
| `/bigquery` | POST | session | Executes arbitrary SQL with server-held creds | **Arbitrary SQL execution** (#4) |
| `/events` | GET | session | Lists events, status=upcoming\|aired, country=…, ids=csv | unbounded `ids` (#29) |
| `/events` | POST | session | Creates event row in `events.event` | duplicate event_id (#13), TZ bug from caller (#7) |
| `/events/<id>` | GET | session | Returns event + latest snapshot + daily history | event_id validation weak (#30) |
| `/months?country=…&series=…` | GET | session | Monthly aggregation w/ proper TZ bucketing | not used by months.html (#16) |
| `/series?country=…` | GET | session | Distinct series list | safe |
| `/event/poll-qna-export?event_id=…` | GET | session | Downloads XLSX from Zoom poll/Q&A views | fuzzy match (#21); writes stack trace fragments to JSON on error (server.py:727) |
| `/save-snapshot-config` | POST | session | Overwrites global `snapshot_config.json` in GCS | **last-writer-wins** + SQL inj (#2, #5) |
| `/run-snapshot[?days_back=N]` | POST | **none** | Triggers cron snapshot job | **public** (#1) |

### 2.2 Frontend pages

| Page | Purpose | Notable controls |
|------|---------|------------------|
| `hub.html` | Landing — Upcoming/Past masterclass cards + KPIs + Compare-select | Country switcher (IN/US), tab switcher, search, sort dropdown, Compare bar, sign-out dropdown |
| `event.html` | Per-event detail with funnel, quality, attendance, call lifecycle, daily pacing, quick links, polls/Q&A export | Polls download button, Compare-with picker, theme toggle, **avatar = direct sign-out** |
| `compare.html` | Side-by-side compare up to 8 events | Picker modal, chip bar, collapsible sections, country switcher (clears selection) |
| `months.html` | 6-month rollup — revenue hero, ROAS, KPIs, funnel, trend chart, leaderboard, insights | Trend metric pills (ROAS/Revenue/Sales/Spend/CPIQL), country switcher |
| `Masterclass Automation.html` (legacy) | 3 tabs: Create Event, Leads, Webinar Attendance | Config card (Jira/Slack/BQ), event form, raw SQL textareas, "Send to Slack" |

### 2.3 BigQuery dependencies (8 tables)

| Path | Read by | Write by |
|------|---------|----------|
| `ik-marketing-data.India_Leads.US_Domain_combined_view` | India leads, cohort, calls, emails, sales | — |
| `ik-marketing-data.Marketing_data_new_logic.Bq_data_Alumni` | US leads, US cohort, US attendance | — |
| `ik-marketing-data.India_Leads.Combined_India_Spend` | India spend | — |
| `ik-marketing-data.Google_Sheets.Combined_Spend_data` | US spend | — |
| `ik-marketing-data.Webinar_analytics.webinar_attendee_data_from_zoom` | attendance | — |
| `ik-marketing-data.Webinar_analytics.zoom_webinar_polls_view` | polls export | — |
| `ik-marketing-data.Webinar_analytics.zoom_webinar_qa_json_view` | Q&A export | — |
| `ik-marketing-data.Marketing_data_new_logic.call_metadata` | call efforts | — |
| `ik-marketing-data.Email.Marketing_Email_Data` | email funnel | — |
| `masterclass-automation-ik.events.event` | hub/event/compare/months | `/events` POST |
| `masterclass-automation-ik.events.fx_rates_monthly` | sales (USD→INR) | — |
| `masterclass-automation-ik.history.event_snapshot` | hub/event/compare/months | `/run-snapshot` |
| `masterclass-automation-ik.history.event_daily` | event page daily | `/run-snapshot` |

### 2.4 External integrations

| System | Direction | Auth carrier |
|--------|-----------|--------------|
| Google Sign-In | inbound | id_token verified server-side with `GOOGLE_CLIENT_ID` |
| Jira `/rest/api/3/issue` | outbound (via `/proxy`) | Basic auth — email + API token (from browser localStorage) |
| Slack `conversations.open` + `chat.postMessage` | outbound (via `/proxy`, also server-side for cron) | Bearer bot token (localStorage **and** GCS) |
| GCS bucket `masterclass-snapshot-config-ik` | read/write | Cloud Run service account |
| BigQuery (both projects) | read/write | Service account JSON mounted at `/secrets/bq-creds.json` |
| Cloud Scheduler | inbound POST `/run-snapshot` | none — **no OIDC token check** |

---

## 3. Full test matrix

Each test case format: **ID · Area · Preconditions · Steps · Expected · Sev · Verify**. Negative case for every positive case. Verify-only-with-running-tool items are flagged "live".

### A. Authentication & session

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| A-01 | Hit `/hub.html` while signed out → login overlay shown, no app data fetched | High | live |
| A-02 | Hit `/event.html?id=anything` signed out → overlay, `/events/<id>` returns 401 | High | live |
| A-03 | Hit `/auth/check` signed out → 401 `{error:"Not authenticated"}` | Low | code-confirmed (server.py:132-138) |
| A-04 | Hit `/run-snapshot` POST signed out → **200 OK runs job**. EXPECTED: should be 401 or require OIDC verification | **Blocker** | code-confirmed (server.py:176, 729). live test trivial |
| A-05 | Sign in with `someone@gmail.com` → 403, error "Only @interviewkickstart.com accounts" | High | code-confirmed (server.py:751-753) |
| A-06 | Sign in with `legit@interviewkickstart.com` but Google JWT `hd` claim absent (e.g., consumer account proxied) → falls through to `email.endswith('@interviewkickstart.com')` check; passes if email matches. Verify whether an attacker can forge `email` without `hd` | High | live; spoof a JWT with hd=null email=fake@ik |
| A-07 | Tamper with `ik_session` cookie last segment → `_verify_session` returns None → 401 | Med | code-confirmed (server.py:39-43) — uses `hmac.compare_digest` |
| A-08 | Set cookie `ik_session=foo|0|bar` (ts=0) → expired → 401 | Med | code-confirmed (server.py:42-43) |
| A-09 | Sign out → cookie cleared; back-button does not restore session (cookie really cleared, not just hidden) | Med | live |
| A-10 | Two tabs signed in. Tab A signs out. Tab B still has stale UI but next API call → 401 → relogin overlay | Med | live |
| A-11 | CSRF on `/save-snapshot-config`: third-party site POSTs with `<form>` → cookie has `SameSite=Strict` → browser does **not** send cookie → request unauthenticated → safe | Low | code-confirmed (server.py:759) |
| A-12 | CORS preflight `OPTIONS` on `/save-snapshot-config` returns `Access-Control-Allow-Origin: *` with `Allow-Headers: Authorization`. Cross-origin XHR can't include cookie under wildcard origin, but an attacker page that already has a stolen `ik_session` value could craft requests. Mitigate by setting explicit origin | High | code-confirmed (server.py:195-198) |
| A-13 | OAuth aud mismatch: present an id_token issued for a different client_id → `verify_oauth2_token` raises → 403 | Med | code-confirmed (server.py:748) |
| A-14 | Clock skew on iat/exp — library tolerance ~10s; not configurable. Document so operators know | Low | external dependency |
| A-15 | Session TTL 7 days; no rotation. User who left IK retains access until cookie expires | High | code-confirmed (server.py:24, 754-759) |

### B. Create Event tab (legacy MA.html)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| B-01 | Submit with all fields blank → alert lists every missing field; no API calls fire | High | code-confirmed (MA:1064-1073) |
| B-02 | Submit with Config collapsed and missing config fields → form auto-opens Config card | Low | code-confirmed (MA:1070-1072) |
| B-03 | Submit title containing `<script>alert(1)</script>` → reaches Jira ADF & Slack message body verbatim. ADF is `text` nodes only (no HTML); Slack accepts mrkdwn, so a title with `>` triggers blockquote formatting. Stored in BQ. UI uses `esc()` everywhere it round-trips → no XSS in UI. Slack/Jira render plain text | Med | code-confirmed (MA:1098, 1209, 1238); live for Slack/Jira |
| B-04 | Submit title with `'` (apostrophe) → safe in ADF JSON, safe in Slack body, safe in BQ row (uses `insert_rows_json` not SQL) | Med | code-confirmed |
| B-05 | Submit very long title (5000 chars) → Jira summary limit 255 → Jira 400; UI shows fail | Med | live |
| B-06 | Submit title with `&` `<` `>` `"` `\\n` → BQ stores raw chars; subsequent SQL queries that interpolate `webinar_type = '<title>'` are NOT a vector because title isn't used in SQL; OK | Low | code-confirmed |
| B-07 | Country=US + Event Type=Masterclass → webinar type dropdown = `MASTERCLASS_EVENT_AI`. Country=India → 3 options. Switching country **resets** dropdown to first option (might silently change submitted value if user already chose) | Med | code-confirmed (MA:824-831) |
| B-08 | Event date in past → form accepts it; no warning; row written with past `live_at`. Snapshot loop filters `BETWEEN -14d AND +60d` so very old events won't be re-snapshotted | Med | code-confirmed (server.py:948-950) |
| B-09 | Event date Feb 29 (leap day) on non-leap year → `<input type=date>` rejects in modern browsers; the value just doesn't bind. ✓ | Low | live |
| B-10 | Event time entered as `25:00` → `<input type=time>` rejects; OK | Low | live |
| B-11 | Country=US + Event Time entered as "9:00 AM IST" (label says IST) → payload `…T09:00:00+05:30` → stored `live_at = 2026-xx-xxT03:30:00Z` → for a US event meant to air at 9 AM PT, the stored time is wrong by ~12.5h. **All downstream attendance/snapshot/leaderboard data for US events will use the wrong date** | **Blocker** | code-confirmed (MA:1167-1168) |
| B-12 | Launchpad (4 instructors) → only `speakers[0]` reaches BQ; speakers 2-4 vanish | High | code-confirmed (MA:1024, 1171) |
| B-13 | Double-click Submit → `btn.disabled = true` between `getCfg()` and first await (MA:1075-1077); browsers fire one click → safe **but** rapid Enter-keypress on form bypasses the button-disable timing window. Verify keyboard behaviour | Med | live |
| B-14 | Jira ticket 1 succeeds → server returns key `MC-123` → ticket 2 errors (e.g., 401) → step shows fail, button re-enabled. User fixes config and resubmits → **second `MC-123`-style ticket created**, first is orphan | High | code-confirmed (MA:1115-1156) |
| B-15 | Save-to-BQ fails after both Jira tickets succeed → Slack DMs still fire (intentionally non-blocking, MA:1159), but event is missing from `/events` listing; instructor team gets DMed for an event the Hub will never show | High | code-confirmed (MA:1191) |
| B-16 | Slack DM to perfLead succeeds, DM to crmLead fails (deactivated user) → step shows fail; perfLead got DM, crmLead did not. No retry | Med | code-confirmed (MA:1241-1243) |
| B-17 | Reload page after submit → status card stays hidden (re-created each submit). Form data persists in DOM (not localStorage for event fields). User who reloads may double-fill | Low | code-confirmed |
| B-18 | Submit Launchpad with `mcDate2 = mcDate` (same day) → accepted; payload day2LiveAt equals liveAt → snapshot pulls two copies of same date → double-counts leads/spend | High | code-confirmed (server.py:986-992) |
| B-19 | Submit landing URL = `javascript:alert(1)` → stored in BQ; event page renders `<a href="${esc(ev.landing_url)}">` which escapes `"` only, not `javascript:` → **click triggers XSS** in event.html context | High | code-confirmed (event.html:274) — `esc()` escapes HTML special chars but does not block `javascript:` URLs |
| B-20 | Submit Zoom URL with embedded `"><script>` → `esc()` escapes `"` so HTML attr-escape is OK | Low | code-confirmed |

### C. Leads tab (legacy MA.html)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| C-01 | Date1 only, India webinar type → query runs; results render in 4 buckets (Meta/CRM/Others/Overall). Verify cell-by-cell against an independently written query | High | live + BQ |
| C-02 | Same date, US webinar type → query runs with US channel buckets (YT/L10X Base/Bot Calling/NI Base/Social/Others) and **separate Reg/GQL totals**. Verify cpiql = `total spend / YT regs` (currently labeled CPL, value is spend/YT, see #C-13) | High | live + BQ |
| C-03 | Date1 = future date → BQ returns 0 rows; UI renders empty table with "Total 0" — no error | Low | live |
| C-04 | Date2 < Date1 (inverted) → query runs with both dates in the `IN (…)` clause → behaves as "either date"; not strictly an error but UX hides the misuse | Low | live |
| C-05 | Editing the SQL textarea by hand → runQuery uses textarea value → **arbitrary SQL** executes against `ik-marketing-data` with the service account's grants. Try `SELECT * FROM ik-marketing-data.HR_data.payroll` (if such exists) — server has no allow-list | **Blocker** | code-confirmed (MA:1436) |
| C-06 | Manipulate `bqKeyFile` field to `/secrets/bq-creds.json` → server reads creds and runs the query with them. May allow read of other-project tables if creds grant it | High | code-confirmed (server.py:256-260, MA:1411-1416) |
| C-07 | Manipulate `bqKeyFile` to `/etc/passwd` → server raises in `from_service_account_file`, JSON 500 echoes the file-path error message → information disclosure | Med | code-confirmed (server.py:269-271) |
| C-08 | Click "Send to Slack" before running a query → alert "Run the query first" | Low | code-confirmed (MA:1644-1646) |
| C-09 | Run query → behind the scenes `/save-snapshot-config` is POSTed with current `slackToken`, `slackSnapshotId`, dates, webinarType → **overwrites global config**. Two users alternating queries will fight over whose Slack ID receives the daily cron's DM | High | code-confirmed (MA:1445-1454) |
| C-10 | Run query for **a date range from a different webinar type** than what's actually queued in the cron → cron starts sending Slack snapshots for the wrong event the next morning | High | code-confirmed |
| C-11 | India query with very wide range (1 year) → BQ returns large result; `_bigquery` puts entire row list in memory → 512Mi Cloud Run could OOM | Med | live; reads `client.query(...).result()` into `[list(row.values()) for row in result]` |
| C-12 | India SUM(s.total_spend) join LIKE: utm_campaign `homemade-l10x-vXY%2BZ` joined to campaign_name `Homemade L10X v.X+Z` — `+` vs `%2B` mismatch. The **server-side `_snapshot_events_to_bq` fix normalizes via REGEXP_REPLACE** (server.py:1024-1034) but **the legacy `runQuery` and `_snapshot_to_slack` do not** — UI shows different spend than the daily snapshot | High | code-confirmed (server.py:889-916 vs 1024-1034) |
| C-13 | US `buildUsTable` labels third column "CPL" but computes `sp / yt` where `sp` = total date spend (all meta-flavored campaigns) and `yt` = YT-channel registrations. So CPL = "total spend ÷ YT-only leads" — misleading metric | High | code-confirmed (MA:1568, 1576) |
| C-14 | Division by zero: meta=0, spend>0 → display `—`. spend=0, meta>0 → `0.00`. Verify both UI paths | Med | code-confirmed (MA:1604, 1608) |
| C-15 | NULL channel value from BQ → `String(row[chanIdx] || '')` empty string → falls into `Others` bucket (India) or `Organic & Other` (US). Acceptable | Low | code-confirmed (MA:1496) |
| C-16 | Query timeout (BQ default 6h, but Cloud Run request timeout default 60s; tool sets `min-instances=1` no max but request timeout is 5 minutes on HTTP/1.1 by default) → server's `client.query().result()` blocks until done → 504 from Cloud Run. UI receives generic error, no retry | Med | live; long-running test |
| C-17 | Press Run twice rapidly → first request still in flight, `_queryController` set → second click becomes "Stop" (aborts the first) → first reply is silently dropped. UX: looks like nothing happened | Med | code-confirmed (MA:1429-1433) |
| C-18 | Switch tab to Webinar Attendance mid-run → query keeps running in background, results still inject into hidden tab when done | Low | code-confirmed (no abort on tab switch) |
| C-19 | Lead Quality Metrics table: percentages computed against `grandTotal` (sum across all role/we values) — so a lead is counted twice (once in role, once in we) → grand total double-counts; pct is artificially halved | High | code-confirmed (MA:1817-1822) — adds to both catTotals and weTotals separately, but `grandTotal` += cnt happens once per row; **actually pct is correct since denominator is per-bucket sum**. Re-verify on a known dataset |
| C-20 | "Send to Slack" message hardcodes `₹` symbol — for US events, Slack message will say `₹` next to USD spend numbers | Med | code-confirmed (MA:1655, 1673) |
| C-21 | sendSnapshotToSlack uses `event.target` (global event) — works in Chrome/Firefox; depends on browser keeping the event in scope across the awaited call. Safari may discard | Low | live in Safari |
| C-22 | Slack message uses `padStart`/`padEnd` for column alignment; very large numbers (e.g., 100,000,000) overflow column width and break the monospace alignment | Low | code-confirmed (MA:1654-1692) |

### D. Webinar Attendance tab

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| D-01 | 3 simultaneous queries via `Promise.allSettled` → 1 fails → other 2 still render | Med | code-confirmed (MA:1974) |
| D-02 | All 3 fail → 3 error blobs in UI; Run button re-enables | Med | code-confirmed |
| D-03 | US webinar type → call + email queries set to `null`; UI shows "not available for US events" | Med | code-confirmed (MA:1965-1966) |
| D-04 | Attendance: rows for channels with 0 attendees still appear (US has fixed `US_ATT_ORDER` then filter `!b.iqls && !b.attendees`); empty channels hidden | Low | code-confirmed (MA:2000-2004) |
| D-05 | `r[2]` (attended_id) null vs empty-string check: `if (r[2] !== null && r[2] !== '')` — BQ returns `None` from LEFT JOIN, which JSON-serialises to `null` in response. ✓ | Low | code-confirmed |
| D-06 | Call query uses Asia/Kolkata DST adjust based on **calendar month**, with Nov-Mar getting +13.5h and Apr-Oct +12.5h. India has no DST. The +13.5h corresponds to UTC→IST (UTC+5:30) + 8h "device offset". Likely a hangover from US-local timestamps in `call_metadata`. Verify with a hand-written query | High | live + BQ (server.py:1282-1292) |
| D-07 | Call connectivity = `duration > 120` (2 minutes) — confirm with sales ops this is the cut-off | Med | product call |
| D-08 | Email funnel: opened=0, clicked=5 → click_rate = `5 / 0 * 100` → `SAFE_DIVIDE` returns NULL → renders "—". OK | Low | code-confirmed (MA:2454) |
| D-09 | Email funnel: `is_delivered` is "1 if explicit DELIVERED or OPEN/CLICK seen" — this means open/click implies delivered. Reasonable. But if delivered=0 explicit and clicked=10 → opened may also be 10 due to inference → delivery_rate = 10/sent; verify the implied counts don't double-count | Med | live + BQ |
| D-10 | Event with apostrophe in webinar type (not user-input today, but enum is hardcoded; if added later) → SQL injection at MA:1340, 1795, 1920, 2216, 2362 | High | code-confirmed |
| D-11 | Refresh page mid-run → AbortController cancels XHR; results never render; button must be re-clicked | Med | live |
| D-12 | Switch country mid-render → buckets are based on the `webinarType` at query time; rendering uses fixed buckets; OK | Low | code-confirmed |

### E. Hub (`hub.html`)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| E-01 | Hub loads `/events?status=upcoming&country=India&limit=200` on sign-in | Low | code-confirmed (hub:360) |
| E-02 | Toggle country → `setCountry` calls `load()` which re-fetches. selectedIds reset implicitly via switchTab not country | Med | code-confirmed |
| E-03 | Switch tab to Past with no aired events → `pastView` shown with empty `pastTableArea` "No past events match"; monthly area also empty | Low | code-confirmed (hub:730-732) |
| E-04 | "Live this week" KPI counts events with `live_at` in the next 7 days; `statusFor` classifies "live" if `-2h < hours_to_live < 24h`. Edge case: an event 8d away appearing as "this week" because Date.now sliced at oneWk = 7*86400000 includes 7d but excludes 8d boundary. Verify ms vs day boundary | Low | code-confirmed (hub:404-413) |
| E-05 | Active spend (2w) sums `meta_spend` for events with hours_to_live between 0 and 336h. **Past events also have meta_spend.** This adds up only future events' partially-snapshotted spend — not current ad spend. Misleading KPI | High | code-confirmed (hub:418-422) |
| E-06 | "Goals at risk" pacing < 70%; an event with `goal_regs = 0` returns `goalPct = null` → status = setup (not at_risk) → not counted. ✓ | Low | code-confirmed (hub:320-324, 333) |
| E-07 | Sort by ROAS treats null as 0 → events with no snapshot rank at bottom (not excluded). Combined with row-bot styling, they appear as "worst performers". Should be excluded | High | code-confirmed (hub:653-655) |
| E-08 | Search "Sharma" matches both title and instructor → filter works; case-insensitive. Empty search → all rows | Low | code-confirmed |
| E-09 | Compare: select 1 row → no bar. Select 2+ → bar appears. Select up to N events → URL grows; selecting >8 then clicking Compare goes to compare.html where it shows "Too many" warning | Med | code-confirmed |
| E-10 | "Best month" highlight: `Math.max(...monthlyData.filter(m => !m.isOpen).map(m => m.roas || 0))` — if all open, `Math.max(...[])` = `-Infinity`, then `m.roas === -Infinity` always false → no badge. ✓ | Low | code-confirmed (hub:699) |
| E-11 | `grid-cols-${Math.min(6, lastSix.length)}` (hub:706) uses **dynamic Tailwind class names**. Tailwind JIT cannot statically extract; CDN tailwind runs JIT at runtime but still requires the class to be matchable. Test for 1, 2, 3, 4, 5, 6 months — may render full-width single column for some N | Med | live; CDN Tailwind safelists differently |
| E-12 | `fmtTime` hardcoded `'Asia/Kolkata'` — US event in hub row shows IST time labelled with no TZ suffix → user thinks it's local | High | code-confirmed (hub:284, 609) |
| E-13 | XSS via event title `<img src=x onerror=…>` — every render path uses `esc()` → safe in DOM. Verify the `style="--topic: ${color}"` inline-style — `color` comes from `TOPIC_COLORS[t]` map lookup (hub:317), so always a hex literal. ✓ | Low | code-confirmed |
| E-14 | Avatar text "UG" hardcoded fallback (hub:109); after sign-in updates to email-derived initials at hub:233 — `(email[0]).toUpperCase() + (email.split('.')[1]?.[0] || '')` works for `first.last@…` but for `first@…` gives only first letter. Cosmetic | Low | code-confirmed |
| E-15 | `lastEvents` is global; mid-render API call mutating it could cause render glitches. No concurrent fetches today, safe | Low | code-confirmed |

### F. Event detail (`event.html`)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| F-01 | Hit `/event.html` with no `?id` → "Missing event id" message + back-to-hub link | Low | code-confirmed (event:208-211) |
| F-02 | Hit `/event.html?id=nonexistent` → server returns 404, UI shows "Event not found" | Low | code-confirmed (event:214) |
| F-03 | `?id=` containing `<script>` → escaped via `esc()` in error message — safe | Med | code-confirmed (event:209) |
| F-04 | US event renders KPI strip with "GQLs/CPGQL/$" labels via `leadLabel`/`cplLabel`/`moneySym(ev.country)` — ✓ | Low | code-confirmed (event:147-152) |
| F-05 | Funnel renders 6 lanes for US (YT/Social/L10X Email/L10X Bot/NI Base/Other), 3 for India (Meta/CRM/Others). All zeros → "No registration data yet" | Med | code-confirmed (event:408-424) |
| F-06 | `fmtDateTime` hardcodes `Asia/Kolkata` + ' IST' suffix — wrong for US events | High | code-confirmed (event:132) |
| F-07 | Avatar `onclick="signOut()"` (event:54) — **single click signs out** with no dropdown or confirm. Easy accidental logout. Other pages have a dropdown menu | Med | code-confirmed |
| F-08 | "Compare with…" link includes `&series=…` and `&open=picker` — but compare.html's `init()` ignores both params (compare.html:218-228). Picker doesn't auto-open; series filter not applied | Med | code-confirmed |
| F-09 | Jira link rows (jira_design_key, jira_landing_key) use `linkRow(label, value, color, isKey=true)` → href is `'#'`, target=`_blank` → clicks open about:blank | Med | code-confirmed (event:776-789) |
| F-10 | Polls/Q&A export: success → file downloads; rows count in `X-Poll-Rows`/`X-Qna-Rows` headers reflected in button label for 4s | Low | code-confirmed (event:171-204) |
| F-11 | Polls/Q&A export failure → button shows "Failed: <error>" for 5s. Server error path leaks last 6 lines of traceback to client (server.py:727) | Med | code-confirmed |
| F-12 | Polls/Q&A: event has live_at on Mar 5, ±1 day window pulls Mar 4-6; another event with similar title on Mar 5 → both events' polls returned mixed | High | code-confirmed (server.py:641-643) |
| F-13 | Polls/Q&A: title has em-dash split (server.py:648) — `"Build LLM Apps – with Vector DBs"` → keyword = `"Build LLM Apps"`. Title `"Build LLM Apps"` alone matches. Title `"AI Agent Workshop"` will not match the polls keyword `"Build LLM Apps"` — but for the right event, fuzzy `LIKE %build llm apps%` works | Med | live |
| F-14 | Call Efforts: `STAGES` array uses keys `pre,p2,p7,p14,p14p` and reads `s[`call_${k}_attempts`]` etc. snapshot row uses those exact column names → ✓ | Low | code-confirmed (event:549-555, server.py:325-328) |
| F-15 | Call Efforts SUM_IDX = [0, 3, 4] (Pre + 0-14D + 14D+) excludes p2 and p7 to avoid double-counting (since p7 ⊃ p2 and p14 ⊃ p7). ✓ | Low | code-confirmed (event:572-574) |
| F-16 | Coverage % computed per-stage as `covered / totalLeads * 100`. Overall coverage = `Math.max(...M.map(m => m.coverage))` — assumes Pre/p14/p14p covered leads overlap; uses max of any stage. Not strictly correct (a lead covered only in p7 might not show in pre or p14 or p14p) | Med | code-confirmed (event:577) |
| F-17 | "Auto Insights" — strings hardcoded to compare stages; if all values are 0 (no data) skips insights gracefully | Low | code-confirmed (event:706-733) |
| F-18 | Daily pacing table uses `Math.max(...daily.map(d => d.total_regs || 0), 1)` for bar width — prevents divide by zero. ✓ | Low | code-confirmed (event:752) |
| F-19 | Event with `live_at=null` (allowed if Create-Event saved without datetime) → fmtDateTime returns "—"; KPI strip shows attendance state as `fmtCountdown(null)` = "—". OK | Low | code-confirmed |
| F-20 | landingUrl = `javascript:alert(1)` → "Landing ↗" link executes JS | High | code-confirmed (event:274) |

### G. Compare (`compare.html`)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| G-01 | `?ids=a,b,c` → all 3 fetched via `/events?ids=…`; cache populated; rendered | Low | code-confirmed (compare:243-257) |
| G-02 | No `?ids` → empty state w/ "Add events" CTA | Low | code-confirmed |
| G-03 | `?ids=` more than 8 IDs → "Too many to compare cleanly"; Trim button | Med | code-confirmed (compare:294-302) |
| G-04 | Picker open from event.html via `?open=picker` query → ignored (init() doesn't read it) | Med | code-confirmed (compare:218-228) |
| G-05 | Series filter `?series=X` → ignored | Med | code-confirmed |
| G-06 | Pick events from different countries → not possible (picker is country-filtered) | Med | code-confirmed (compare:489) |
| G-07 | Country switch → `clearAll()` wipes selection + URL; reload of picker for new country | Low | code-confirmed (compare:182) |
| G-08 | Winner cell highlighted via `winner-cell` class. With 1 valid value of 5 events: that single value wins with all 4 losers → falsely implies superiority | High | code-confirmed (compare:369-387) |
| G-09 | Cohort denominator excludes `role_null` → percentages don't sum to 100% across the 5 displayed buckets (data total includes null role) | Med | code-confirmed (compare:444-447) |
| G-10 | Cohort hides rows where `hasRole` and `hasWe` are both falsy — uses bitwise OR on numbers `(s.role_sde || s.role_ml || …) > 0` which short-circuits on truthy. Works | Low | code-confirmed |
| G-11 | Sales/Revenue mixed currency: India event INR + US event USD compared side-by-side. Money sym uses `currentCountry`, but country switch wipes selection → never displays mixed. ✓ | Low | code-confirmed |
| G-12 | Coverage % uses `call_pre_covered / call_total_leads` — labeled "Coverage %" without "pre" qualifier; should be "Pre-call coverage" | Med | code-confirmed (compare:424-430) |
| G-13 | "Email Open %" formula: `100 * email_opened / email_sent`. Server query (server.py:1476) sets `is_opened = 1` for both OPEN and CLICK events — so opens count includes clicks even where no open recorded. Numerator > denominator possible if any malformed data | Med | code-confirmed |
| G-14 | Cache invalidation: edit a snapshot in BQ → compare page still shows cached → user must refresh hard | Low | code-confirmed (compare:168, 247) |

### H. Months (`months.html`)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| H-01 | Months page fetches `/events?status=aired&country=…&limit=200` — **does not use the server's `/months` endpoint** which does proper TZ bucketing and blended-CPL math | High | code-confirmed (months:324) |
| H-02 | Trend chart re-renders on metric pill click; Chart.js instance destroyed/recreated each time. Memory leaks tested via 100+ clicks | Low | live |
| H-03 | Trend chart Y-axis CPIQL formatter uses `fmtMoneyRaw(v)` which reads `currentCountry` — country switch updates `curLbl` label but the cached chart Y-axis still has old symbol until next render | Med | code-confirmed (months:489) |
| H-04 | Leaderboard sort by `overall_roas || 0` → null-ROAS events at bottom labeled "Bottom" badge | High | code-confirmed (months:497-499) |
| H-05 | Pipeline-open detection = 14 days from `live_at` — event 13d 23h ago = open, 14d 1m ago = closed. Boundary may flap on a per-second basis | Low | code-confirmed (months:315-319) |
| H-06 | Bars revenue: `Math.max(2, m.rev / maxRev * 100)` ensures min 2% height; with all-zero months bar visible but empty. ✓ | Low | code-confirmed (months:398) |
| H-07 | "Coverage Gap" insight string is hardcoded for `MASTERCLASS_INDIA_2 series` with fabricated `~₹1.5 Cr` figure — not data-driven | Med | code-confirmed (months:548) |
| H-08 | `aggCpiql = totalSpend / totalRegs` uses **total regs** (incl. CRM, Other) as denominator. True CPIQL is `meta_spend / meta_regs`. Significantly understates CPIQL (typically by 30-50%) | High | code-confirmed (months:378) |
| H-09 | Funnel sum `totalCalls = sum of (call_pre + call_p2 + call_p14p)` — double-counts because pre and p14p overlap with p2-7-14 windows (cumulative). Should be SUM_IDX=[0,3,4] like event.html | High | code-confirmed (months:419) |
| H-10 | No aired events → error area "No aired events for this country yet"; content hidden | Low | code-confirmed (months:329-334) |
| H-11 | Period subtitle `firstM – lastM` — when only 1 month, both equal, displays "Mar 2026 – Mar 2026". Cosmetic | Low | code-confirmed |
| H-12 | Drill-down link from event.html (`/months.html?month=2026-03&country=US`) — months page ignores both params; just loads the default 6 months | Med | code-confirmed |

### I. Snapshot / Scheduler

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| I-01 | POST `/run-snapshot` with no auth → 200 OK, runs full snapshot. Any internet citizen can spam BQ costs | **Blocker** | code-confirmed (server.py:176) |
| I-02 | POST `/run-snapshot?days_back=999` → backfill 999 days of events; BQ cost amplification | High | code-confirmed (server.py:732, 948) |
| I-03 | POST `/run-snapshot` body 100MB → server reads `Content-Length` for /save-snapshot-config but not /run-snapshot; should error gracefully | Low | live |
| I-04 | POST `/run-snapshot` malformed JSON → returns 200 (body unused) | Low | code-confirmed |
| I-05 | Daily snapshot 5:30 AM UTC: `_snapshot_to_slack` and `_snapshot_events_to_bq` run independently; one failing doesn't block the other (✓). If both fail, raises | Low | code-confirmed (server.py:861-878) |
| I-06 | Cloud Scheduler **and** the in-process `run_daily_snapshot` thread (server.py:1797-1811) both trigger snapshots at ~11 IST. Double-firing if both wired up. Should be one or the other | Med | live; check Cloud Run logs at 5:30 UTC for two runs |
| I-07 | Idempotency: rerun snapshot on same day → two rows in `history.event_snapshot` and `history.event_daily` for that event. `latest_snap` CTE picks max `snapshot_at`, so reads use latest. Daily appends grow forever — verify table partition strategy or cleanup | Med | live + BQ |
| I-08 | `_snapshot_to_slack` reads global config; if no config saved → raises with "snapshot config missing"; caught by outer `_do_snapshot` and logged. The BQ path still runs | Low | code-confirmed (server.py:882-884) |
| I-09 | SQL injection in `_snapshot_to_slack`: signed-in user POSTs `webinarType="' UNION SELECT … --"` to `/save-snapshot-config` → cron runs attacker SQL the next morning | **Blocker** | code-confirmed |
| I-10 | `webinarDates` interpolation: `, '.join(f"'{d}'" for d in cfg['webinarDates'])` (server.py:886). A date `2026-01-01', (SELECT …))` injects. Date format never validated | **Blocker** | code-confirmed |
| I-11 | GCS write contention: two users hit Save Snapshot Config simultaneously → GCS PUT is last-writer-wins. Last user's config wins | Med | live |
| I-12 | Cold start: min-instances=1 (per memory note) → no cold-start latency. If scale-up to 2 instances, the in-process thread runs twice — confirm Cloud Run is set to **min=1 max=1** if relying on the in-process thread | Med | live; check `gcloud run services describe` |
| I-13 | `_query_sales_india` (server.py:1372-1418) joins to `events.fx_rates_monthly` for USD→INR conversion. If a sale's month has no FX row, falls back to 84 INR/USD. Verify fx_rates_monthly is populated through to current month | Med | live + BQ |
| I-14 | US snapshot path stores `meta_spend` as USD but downstream UI labels render `$` or `₹` based on event country — verify event.html `moneySym(ev.country)` cascades to all daily/funnel currency renders | Med | code-confirmed (event:151) |
| I-15 | Event with `webinar_type=NULL` is skipped from snapshot (server.py:946). Create-Event form always sets webinar_type, so this should only happen for legacy/manual rows | Low | code-confirmed |
| I-16 | `_query_calls_india` DST-style timestamp adjustment (server.py:1282-1292) hardcodes "Nov-Mar = +13.5h, Apr-Oct = +12.5h". India does not have DST. This is a half-hour shift between months that doesn't match any India calendar event. Possibly compensating for the source `call_metadata` storing PT or different offset; verify with sales ops | High | code-confirmed |
| I-17 | `_build_event_snapshot_india` runs 6 sub-queries with `_safe_query` wrapping — one failure → default `None` values → snapshot row still written, but with partial nulls. ✓ | Low | code-confirmed |

### J. BigQuery correctness (most important)

For each metric, write a hand reference query, run against same date range UI used, and diff:

| ID | Table / Metric | Reference SQL | Vector |
|----|-----------|---------|--------|
| J-01 | `India_Leads.US_Domain_combined_view` — total IQLs for an event | `SELECT COUNT(*) FROM <dedup CTE> WHERE web_scheduled_date IN (...) AND webinar_type=...` | Compare to Leads tab "Overall" total |
| J-02 | India spend — total per date | `SELECT spend_date, SUM(cost) FROM Combined_India_Spend WHERE LOWER(campaign_name) LIKE …` | Compare to Daily IQL Summary "Spends" column |
| J-03 | India CPIQL — spend ÷ meta-regs | hand calc | Compare to UI cell |
| J-04 | India spend dedup: utm_campaign vs campaign_name with non-alphanumeric stripping | Use `REGEXP_REPLACE` normalization | Compare server-side `_build_event_snapshot_india` vs legacy `_snapshot_to_slack` |
| J-05 | US lead bucket attribution: `utm_campaign LIKE %l10x_social%` → Social, etc. Edge cases with `nibucket-l10x_social` | Hand check each CASE branch | Test event funnel UI |
| J-06 | US spend: meta-only filter `(LIKE %meta% OR LIKE %facebook% OR LIKE %l10x%)`. Will pick up L10X spend even if not Meta-paid | Hand inspect | Compare with raw Combined_Spend_data |
| J-07 | Attendance: zoom roster joined on hubspot_id (cast to INT64). What about leads whose hubspot_id has letters or null? | `WHERE hubspot_id IS NOT NULL AND SAFE_CAST(hubspot_id AS INT64) IS NOT NULL` | Verify SAFE_CAST behavior |
| J-08 | Call efforts pre-webinar: `activity_datetime < webinar_start_datetime_ch`. activity_datetime is shifted +12.5h or +13.5h per I-16; verify this is correct | Hand check sales ops timezone | Live + BQ |
| J-09 | Email funnel `is_delivered` inference: OPEN/CLICK implies delivered. Verify delivery count never exceeds sent | Hand check | Verify on a known dataset |
| J-10 | Sales revenue USD→INR using monthly FX. For sales in current month before FX row exists → fallback 84. Verify fallback rate is reviewed quarterly | Hand check fx_rates_monthly | Live + BQ |
| J-11 | Sales filter excludes `work_ex IN ('0-2', '3-4')` and `student` — matches lead filter. But sales table doesn't filter `gql_flag = 0` — check whether GQL filter should apply | Hand check | Product call |

### K. Security (additional to A & I)

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| K-01 | XSS: event title `<img src=x onerror=alert(1)>` → escaped in DOM via `esc()`; safe in UI. **But** title passes through Slack DM body unescaped → Slack renders mrkdwn → user can post fake messages | Med | code-confirmed |
| K-02 | XSS: event title in Polls XLSX filename — server.py:711 strips to alphanum + `._-` via regex, max 60 chars → filename safe | Low | code-confirmed |
| K-03 | SSRF via `/proxy?url=http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token` → Cloud Run blocks metadata for non-OIDC by default; verify | High | live |
| K-04 | SSRF via `/proxy?url=http://localhost:8080/auth/check` with attacker's cookie → loop-back; might bypass IP-restricted internal services if any | Med | live |
| K-05 | IDOR: `/save-snapshot-config` has no per-user partition — every IK staffer can read & overwrite anyone else's config (no read endpoint exposes it, but write affects all) | High | code-confirmed |
| K-06 | Secret leakage: `_bigquery` 500 echoes `str(e)` (server.py:271). BQ errors include table names, sometimes columns. Helps an attacker map the schema | Med | code-confirmed |
| K-07 | Stack trace echoed by `_poll_qna_export` error path (server.py:727) — last 6 lines of traceback returned to client | Med | code-confirmed |
| K-08 | No rate limiting on `/auth/verify` — brute-force unlikely (requires valid Google id_token) but no logs/alerts | Low | live |
| K-09 | No rate limiting on `/bigquery` — a logged-in user can exhaust BQ quota at $5/TB | High | code-confirmed (no quota wrapper) |
| K-10 | Dependency CVEs: `google-cloud-bigquery`, `google-cloud-storage`, `openpyxl` unpinned. Run `pip-audit` on the running image | Med | live |
| K-11 | Cookie flags: `HttpOnly; SameSite=Strict; Max-Age=604800` — missing `Secure` | High | code-confirmed |
| K-12 | CORS `Access-Control-Allow-Origin: *` with `Access-Control-Allow-Headers: Authorization`. Browsers won't send cookies; but combined with stolen Bearer tokens in localStorage, an attacker page can call the API. Mitigate by setting explicit origin | High | code-confirmed (server.py:195-198) |
| K-13 | Slack token stored in localStorage `ik_mc_launch_v1` (MA:780, 795). Persists indefinitely. Any XSS, browser extension, or shared laptop = full Slack bot impersonation | **Blocker** | code-confirmed |
| K-14 | Jira API token stored in localStorage with same exposure as K-13 | **Blocker** | code-confirmed |
| K-15 | Slack token written to GCS plaintext on every Run-click → bucket read access = token theft | High | code-confirmed |
| K-16 | `/proxy` allows arbitrary URL — any signed-in user can use the server as a forward proxy to send emails (via Mailgun etc.), DDoS targets, etc. | High | code-confirmed |

### L. Reliability & Ops

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| L-01 | Cold start with min-instances=1 → should be ≤1s. Verify with `time curl /hub.html` after 30 min idle | Med | live |
| L-02 | OOM at 512Mi: India leads query for 1 year → tens of MB of rows in memory; tractable. Verify with `ulimit` profiling | Med | live |
| L-03 | Cross-region latency: Cloud Run in asia-south1, BQ in US → 200-300ms per query baseline; events page issues 3 parallel queries → ~300ms total | Med | live |
| L-04 | Cloud Scheduler retry on 5xx: default 3 retries with backoff. If snapshot is non-idempotent (re-snapshots same event), retries cause duplicate rows | Med | live |
| L-05 | Logs: `_query_sales_india` exception path logs full SQL via `_safe_query`'s `print` — may include date ranges and webinar types but not PII | Low | code-confirmed (server.py:1129) |
| L-06 | `/bigquery` errors logged with `str(e)` — may include BQ table names and the user's raw query in the response body | Med | code-confirmed |
| L-07 | Snapshot thread runs forever; if Cloud Run instance is killed for scaling-to-zero, thread dies; min-instances=1 keeps it alive on instance 1 only | Low | code-confirmed |
| L-08 | `SESSION_SECRET` env var — if leaked, full session forgery. Confirm secret is rotated periodically | High | ops policy |
| L-09 | `bq-credentials` Secret Manager rotation policy — none default. If service-account key compromised, full BQ read on `ik-marketing-data` | High | ops policy |
| L-10 | Dockerfile copies `["Masterclass Automation.html", "."]` with array syntax to handle the space — works (server.py:1813 expects `/Masterclass%20Automation.html`) | Low | code-confirmed |
| L-11 | Dockerfile runs as `root` (default python:3.11-slim) — should drop to non-root user for defense-in-depth | Med | code-confirmed |
| L-12 | No HEALTHCHECK in Dockerfile → Cloud Run probes the port directly; OK | Low | code-confirmed |

### M. UX & accessibility

| ID | Test | Sev | Verify |
|----|------|-----|--------|
| M-01 | Tab key navigation through hub.html — country switcher buttons reachable; sort dropdown; search; row checkboxes | Low | live |
| M-02 | Error messages: "Failed to load: HTTP 500" — not actionable. Improve | Low | code-confirmed |
| M-03 | Mobile viewport: hub.html `max-w-[1400px]` with grid-cols-4 KPI strip; narrow viewport will overflow horizontally | Med | live |
| M-04 | Long Jira ticket key (e.g., `VERYLONGPROJECT-9999`) in event.html quick links → row overflow | Low | live |
| M-05 | Browser back/forward between Hub→Event→Hub → fresh fetch each time, no state preserved (selectedIds reset, country preserved via localStorage) | Low | code-confirmed |
| M-06 | Refresh on Past tab → preserves country (localStorage) but resets `currentStatus='upcoming'` (hub:188) → user lands back on Upcoming after every refresh | Med | code-confirmed |
| M-07 | Theme toggle persists in localStorage; pre-paint script in `<head>` adds `.dark` class before render → no flash | Low | code-confirmed |
| M-08 | Login overlay covers app; if Google Identity Services fails to load (e.g., adblock) → button area is empty placeholder, no fallback | Med | live |
| M-09 | Polls/Q&A download button: success message "Downloaded (5 polls · 3 Q&A)" replaces "Download XLSX" for 4s. User who clicks during those 4s gets no response (disabled). OK | Low | code-confirmed |
| M-10 | Compare page: removing the last chip → calls `showEmpty()` and `renderChipBar()` (compare:274-280). URL `?ids=` removed | Low | code-confirmed |
| M-11 | Avatar dropdown closes on outside click via document listener (hub:265-269) — does not close on Escape key | Low | live |
| M-12 | Color contrast: gradient text `gradient-text-em` (green) on light bg ≈ 4.5:1 — borderline AA | Low | live |

---

## 4. Verified bugs (static-analysis confirmed) with file:line + suggested fix

### BUG-01 — `/run-snapshot` is unauthenticated (Blocker)

**Where:** `server.py:176-177`

```python
if self.path.startswith('/run-snapshot'):  # called by Cloud Scheduler, no browser session
    self._run_snapshot()
    return
```

**Observed:** Anyone can POST `https://masterclass-automation-1016538215063.asia-south1.run.app/run-snapshot` and trigger a full snapshot job, burning BQ slots and rewriting `history.event_*`. Combined with BUG-02, this becomes a vehicle for SQL injection.

**Expected:** Require a verified OIDC token from Cloud Scheduler (Cloud Run supports Authorization: Bearer <id_token> with `aud=<service-url>` from a scheduler-bound service account). At minimum, require a shared-secret header set in the Cloud Scheduler job.

**Fix sketch:**
```python
if self.path.startswith('/run-snapshot'):
    if not _verify_scheduler_oidc(self):
        self._json_response(401, {'error': 'Unauthorized'})
        return
    self._run_snapshot()
    return
```
where `_verify_scheduler_oidc` validates the `Authorization: Bearer <id_token>` using `google.oauth2.id_token.verify_oauth2_token` against the Cloud Run service URL and the scheduler's service-account email.

---

### BUG-02 — SQL injection in `_snapshot_to_slack` via `save_snapshot_config` (Blocker)

**Where:** `server.py:886` and `server.py:899`

```python
date_literal = ', '.join(f"'{d}'" for d in cfg['webinarDates'])
webinar_type = cfg.get('webinarType', '')
# …
WHERE rnk = 1 AND web_scheduled_date IN ({date_literal}) AND webinar_type = '{webinar_type}'
```

**Observed:** Any IK-signed user POSTs to `/save-snapshot-config` with `{"webinarDates":["x'); SELECT * FROM …secrets… UNION SELECT 1, 2, 3, 4, 5; --"],"webinarType":"…"}`. Next morning's cron runs the attacker SQL with the service account's permissions on `ik-marketing-data`.

**Expected:** Use BQ parameterized queries (`@wt`, `@dates`).

**Fix sketch:** Replace `f"…{date_literal}…{webinar_type}…"` with a parameterized query, like `_build_event_snapshot_india` already does (server.py:992-1014).

---

### BUG-03 — Slack & Jira tokens in localStorage (Blocker)

**Where:** `Masterclass Automation.html:780, 792-796`

```js
const LS = 'ik_mc_launch_v1';
function saveConfig() {
  var d = {};
  document.querySelectorAll('[data-cfg]').forEach(function(el) { d[el.dataset.cfg] = el.value; });
  localStorage.setItem(LS, JSON.stringify(d));
}
```

Includes `jiraToken` (input id `jiraToken`, MA:484) and `slackToken` (MA:508). Both are functional secrets persistent forever.

**Observed:** Any XSS, browser extension, or shared laptop session = complete token theft. Tokens are also POSTed to `/save-snapshot-config` on every Run click and written to GCS plaintext.

**Expected:** Tokens stored server-side in Secret Manager; the browser holds no functional secrets.

**Fix sketch:** Move Jira & Slack credentials into the Cloud Run secret store. Frontend only triggers actions; server authenticates outbound calls. Remove the entire Config card.

---

### BUG-04 — `/bigquery` runs arbitrary SQL (Blocker)

**Where:** `server.py:245-271` reads `query` from request body and runs it.

**Observed:** The "Leads" tab textarea is editable. Any signed-in user can run any SQL against `ik-marketing-data` (or, via `keyFile`, any project the file authorizes). The service-account JSON is mounted from `/secrets/bq-creds.json`.

**Expected:** Either (a) remove the endpoint and only ship templated, parameterized queries from the server, or (b) restrict to a parsed allow-list of SELECTs against an allow-list of tables. Strip `keyFile` support entirely.

---

### BUG-05 — US events stored with `+05:30` UTC offset (Blocker for US data correctness)

**Where:** `Masterclass Automation.html:1167-1168`

```js
liveAt:     (f.mcDate  && f.mcTime) ? (f.mcDate  + 'T' + f.mcTime + ':00+05:30') : null,
day2LiveAt: (f.mcDate2 && f.mcTime) ? (f.mcDate2 + 'T' + f.mcTime + ':00+05:30') : null,
```

**Observed:** For a US masterclass at 9 AM PT, user enters "9:00 AM" (label says IST — itself wrong). Stored UTC = `T03:30:00Z` instead of `T16:00:00Z`. All snapshot date-bucketing, leaderboard ordering, polls/Q&A fuzzy date filtering will be off by ~12.5h, possibly placing the event on the wrong PT calendar date.

**Fix:** Build the ISO string based on `f.country`. For US: `…T<time>:00-08:00` (with PT DST handling). For India: `+05:30`. Also relabel the time input to "Event Time (local)" with a clear timezone affordance.

---

### BUG-06 — Launchpad drops 3 of 4 instructors (High)

**Where:** `Masterclass Automation.html:1017-1024` (collects all 4 into `speakers`) but `MA:1171` only sends `instructorName: f.speaker` (= `speakers[0]`).

**Fix:** Either (a) add `instructorNames: f.speakers` to the payload and a new BQ column, or (b) concatenate (`speakers.join(', ')`) into `instructor_name`. (b) is the minimal change.

---

### BUG-07 — `fmtTime`/`fmtDateTime` hardcoded to `Asia/Kolkata` (High)

**Where:** `hub.html:284`, `hub.html:609`, `event.html:132`

```js
function fmtTime(iso) {
  …
  return new Date(iso).toLocaleTimeString('en-US', { hour:'numeric', minute:'2-digit', timeZone: 'Asia/Kolkata' });
}
```

**Fix:** Pass `country` (or a derived `timeZone` string) into these helpers. Default to `Asia/Kolkata` for IN, `America/Los_Angeles` for US. Update the literal " IST" suffix in `event.html:132` to the matching abbreviation.

---

### BUG-08 — `_create_event` allows duplicate event_ids (High)

**Where:** `server.py:539-588` uses `client.insert_rows_json` (streaming insert, no PK).

**Fix:** Before insert, run `SELECT 1 FROM events.event WHERE event_id = @eid LIMIT 1`. If exists → 409 Conflict with existing event_id in payload, let the caller decide to redirect or amend.

---

### BUG-09 — Hub "Active spend (2w)" sums wrong window (High)

**Where:** `hub.html:418-422` includes any event with `hours_to_live > 0 AND < 336h`. Excludes past events entirely. Should be "live ad spend in the last 14 days" — which requires daily-pacing data, not the event-level `meta_spend`.

**Fix:** Either remove the KPI or compute from `event_daily.meta_spend WHERE registration_date >= CURRENT_DATE - 14`.

---

### BUG-10 — Months page does not call the `/months` endpoint (High)

**Where:** `months.html:324` fetches `/events?status=aired` and re-aggregates client-side, losing the server's blended-CPL math and TZ-correct bucketing (server.py:388-493).

**Fix:** Call `/months` and render the response.

---

### BUG-11 — Compare winner highlighted with N=1 valid value (High)

**Where:** `compare.html:369-387`

**Fix:** Add `if (valid.length < 2) winnerIdx = -1`.

---

### BUG-12 — landing_url, zoom_url `javascript:` XSS (High)

**Where:** `event.html:270, 274` render `<a href="${esc(ev.zoom_url)}" target="_blank">`. `esc()` only escapes `& < > "`. A stored `landing_url=javascript:alert(1)` triggers on click.

**Fix:** Add `safeUrl(s)` that returns the URL only if it starts with `https?://` (or rejects `javascript:`).

---

### BUG-13 — `/save-snapshot-config` is global, last-writer-wins (High)

**Where:** `server.py:792-799` + `Masterclass Automation.html:1445-1454`

**Fix:** Either (a) per-user partition under `snapshot_config/<email>.json`, or (b) only allow admins to write, or (c) move config into the `events.event` row keyed by event_id (the cron then reads per-event).

---

### BUG-14 — Cookie missing `Secure` flag (High)

**Where:** `server.py:759`

```python
self.send_header('Set-Cookie',
    'ik_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + str(SESSION_TTL))
```

**Fix:** Append `; Secure`. Cloud Run terminates TLS, so this is safe.

---

### BUG-15 — `/proxy` is an open SSRF/forward proxy (High)

**Where:** `server.py:210-243`

**Fix:** (a) Require the target URL host to be on an allow-list (`api.atlassian.net`, `*.atlassian.net`, `slack.com`). (b) Refuse private IPs and metadata addresses. (c) Strip the `Authorization` header forwarding entirely — server should attach its own secrets.

---

### BUG-16 — Event link to compare loses `series` / `open=picker` (Med)

**Where:** `event.html:385` builds the URL; `compare.html:218-228` `init()` doesn't consume those params.

**Fix:** In `compare.html init()`, read `open` and `series` params; if `open=picker`, call `openPicker()` after data loads; pre-set the picker's search box with `series`.

---

### BUG-17 — Jira "key" link points to `#` (Med)

**Where:** `event.html:776-789` — when `isKey=true`, `href = '#'`, but `target=_blank` still set.

**Fix:** Resolve the key to full Jira URL using a config-level `jiraBaseUrl` (would require server-side config since the browser no longer holds it after BUG-03 fix).

---

### BUG-18 — Avatar = sign-out on event.html (Med)

**Where:** `event.html:54`

**Fix:** Either match the other pages (avatar → dropdown menu → sign out) or change to a confirm dialog.

---

### BUG-19 — `runQuery` overwrites snapshot config on every click (Med)

**Where:** `Masterclass Automation.html:1445-1454`

**Fix:** Decouple snapshot config from query runs. Add explicit "Configure daily snapshot" action.

---

### BUG-20 — Hub sort treats null-ROAS as 0 (Med)

**Where:** `hub.html:653-655`, also `months.html:496`, `hub.html:736`

**Fix:** Exclude null-ROAS events from sort, top-3, bottom-3 and "Best/Bottom" badges. Or rank them in a separate "Awaiting data" bucket.

---

### BUG-21 — Months `aggCpiql` denominator wrong (High)

**Where:** `months.html:378` — `totalSpend / totalRegs` uses total regs (incl. CRM, Other).

**Fix:** Sum `meta_regs` separately and divide.

---

### BUG-22 — Months funnel double-counts calls (High)

**Where:** `months.html:419` — `call_pre + call_p2 + call_p14p`. p2 overlaps with p14, p14 is already counted in p14p? Actually p14 is "0-14D cumulative" and p14p is "14D+" exclusive. The 3-stage non-overlapping set is `pre + p14 + p14p` (excluding p2 and p7 which are subsets of p14). Using `pre + p2 + p14p` skips the 3-14D window's calls.

**Fix:** Use SUM_IDX `[0, 3, 4]` matching event.html:572 (`pre + p14 + p14p`).

---

### BUG-23 — Coverage Gap insight is fabricated (Med)

**Where:** `months.html:548`

**Fix:** Remove the hardcoded insight or replace with a data-driven one (e.g., "X% of events have no snapshot yet").

---

### BUG-24 — Lead Quality Metrics double-counts grandTotal (Re-verify)

**Where:** `Masterclass Automation.html:1817-1822` — appears OK on close read; each `cnt` is one lead-row; catTotals and weTotals sum independently to N (one bucket each), and grandTotal is also N (one row at a time). So pct = bucket / N. Correct. — Annotation: keep test J-04 to confirm against BQ.

---

### BUG-25 — India spend dedup mismatch between live UI and cron snapshot (High)

**Where:** Live UI `runQuery` (MA:1399-1402) uses LIKE join with no normalization. Server-side `_build_event_snapshot_india` (server.py:1024-1034) uses `REGEXP_REPLACE` normalization. Numbers will diverge.

**Fix:** Push the same normalization to the legacy UI query or, better, deprecate the Leads tab and serve numbers from the snapshot.

---

### BUG-26 — Polls/Q&A export ±1 day window collides events (Med)

**Where:** `server.py:641-643`

**Fix:** Tighten to ±0 days unless event title fuzz strictly matches; or use the snapshotted attendance hubspot_id list to constrain.

---

### BUG-27 — In-process daily snapshot thread duplicates Cloud Scheduler (Med)

**Where:** `server.py:1797-1811` starts a background thread that fires `_do_snapshot()` at 11 IST; Cloud Scheduler also fires `/run-snapshot` at 5:30 UTC (also 11 IST).

**Fix:** Pick one. If Cloud Scheduler is canonical, delete the in-process thread (server.py:1810-1811).

---

### BUG-28 — Cohort denominator excludes role_null (Med)

**Where:** `compare.html:444-447`

**Fix:** Include `s.role_null` in `roleDen(ev)`, or label the rendered total as "% of typed roles".

---

### BUG-29 — `requirements.txt` unpinned + missing `google-auth` (Med)

**Where:** `requirements.txt:1-3`

**Fix:**
```
google-auth==2.x.y
google-cloud-bigquery==3.x.y
google-cloud-storage==2.x.y
openpyxl==3.x.y
```
and rebuild image.

---

### BUG-30 — CORS wildcard with credential headers (High)

**Where:** `server.py:195-198`

**Fix:** Either remove CORS entirely (everything is same-origin) or set the origin to the Cloud Run URL explicitly.

---

## 5. Data-correctness diffs (cannot run from this session)

This audit cannot execute BigQuery. The following should be done with a target date range and an existing event:

For each pair (UI metric, BigQuery reference query) listed in §3.J, run both and record:

| Metric | UI value | Reference SQL value | Δ | Δ% | Investigation |
|--------|----------|---------------------|---|-----|---------------|
| Total IQLs (India, one event) | | | | | |
| Total IQLs (US, one event) | | | | | |
| Meta spend (India, per date) | | | | | |
| Meta spend (US, per date) | | | | | |
| CPIQL (India) | | | | | |
| CPGQL (US) | | | | | |
| Attendees (India) | | | | | |
| Attendees (US) | | | | | |
| Email sent / delivered / opened / clicked | | | | | |
| Call attempts / connects / coverage / connectivity | | | | | |
| Sales count (India) | | | | | |
| Revenue INR (India) | | | | | |
| ROAS overall | | | | | |
| ROAS paid | | | | | |

Drivers of expected drift (already identified static-analysis):

- **Hub "Active spend (2w)" vs. real ad-spend in last 14d** — currently incorrect (BUG-09); expect drift of 30-100%.
- **Months page CPIQL vs. server `/months` CPIQL** — denominator differs (BUG-21); expect ~30-50% understated.
- **Leads tab India spend (live UI) vs. snapshot Slack DM** — normalization mismatch (BUG-25); expect a few % drift on events with multi-campaign spend on the same date.
- **US events liveAt** — stored with wrong TZ offset (BUG-05); expect attendance/snapshot to land on adjacent PT calendar date.

---

## 6. Open questions for the product owner

1. **Tool scope.** Is `/bigquery` (arbitrary SQL textarea) meant to remain? It's the single biggest exposure (BUG-04). If retired, can we hard-delete the textareas?
2. **Snapshot ownership.** Should `/save-snapshot-config` be per-user, per-event, or admin-only? (BUG-13)
3. **Currency display.** For US events shown on India-default UI (hub.html country=IN), should they appear at all, or only on country=US?
4. **Per-stage call coverage.** Is "Overall coverage" = `max(per-stage coverage)` (current behavior) acceptable? The strict definition is `unique covered leads across any stage / total leads`.
5. **Launchpad instructor schema.** Add a JSON column `instructors` to `events.event` or stay with single `instructor_name`? (BUG-06)
6. **FX rate fallback.** 84 INR/USD is hardcoded — review cadence? Who owns the `fx_rates_monthly` table?
7. **Pipeline-open window.** 14 days assumed in 4+ places (hub/months/event); is this codified by sales? Should it be a config?
8. **Call activity timestamp offset.** `+12.5h` / `+13.5h` based on calendar month (server.py:1282-1292) is mysterious. Is this compensating for a US-stored timestamp? Why does it flip by month?
9. **Drill-down query string** (`?month=…&country=…&open=picker&series=…`) — should they actually work, or were they aspirational placeholders? (BUG-16, H-12)
10. **Hub avatar vs. event avatar UX inconsistency** — which is the intended pattern? (BUG-18)
11. **`MASTERCLASS_INDIA_2` series insight** is hard-coded with a fake number (BUG-23). Was this a placeholder waiting for real backfill numbers?
12. **Polls/Q&A title fuzz** — what's the canonical join key from event to Zoom polls? `webinar_topic LIKE %first-40-chars%` is fragile.

---

## 7. Priorities — what to fix first

**This week (Blockers + security):**
1. BUG-01 (`/run-snapshot` unauthenticated)
2. BUG-02 (snapshot SQL injection)
3. BUG-04 (`/bigquery` arbitrary SQL)
4. BUG-03 (tokens in localStorage)
5. BUG-12 (`javascript:` URL XSS)
6. BUG-14 (cookie `Secure` flag)
7. BUG-15 (`/proxy` SSRF)
8. BUG-30 (CORS wildcard)

**Next sprint (data correctness):**
9. BUG-05 (US event TZ offset)
10. BUG-07 (TZ rendering)
11. BUG-09 (Hub active spend KPI)
12. BUG-10 (months ↔ /months wiring)
13. BUG-21 (months CPIQL denominator)
14. BUG-22 (months call double-count)
15. BUG-25 (India spend dedup mismatch)
16. BUG-08 (duplicate event_ids)
17. BUG-20 (null-ROAS treated as 0)

**Polish (UX + correctness):**
18. BUG-06 (Launchpad instructors)
19. BUG-11 (compare winner edge case)
20. BUG-16, BUG-17, BUG-18, BUG-19, BUG-23, BUG-26, BUG-27, BUG-28

**Hygiene:**
21. BUG-29 (pin requirements)
22. Add health check, non-root Docker user, structured logging
23. Document open questions and close them with product owner

---

*End of audit. Tool was read end-to-end; no live verification was performed. "Live" items in the matrix are reproduction steps for the operator; "code-confirmed" items are reproducible from the source above.*
