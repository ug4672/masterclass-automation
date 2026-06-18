#!/usr/bin/env python3
"""Local proxy server — serves the HTML and forwards API calls to Jira/Slack/BigQuery."""
import http.server, urllib.request, urllib.parse, urllib.error, json, os, re, ssl, decimal, datetime
import threading, time, hmac, hashlib

GCS_BUCKET = os.environ.get('GCS_BUCKET', '')
GCS_CONFIG_KEY = 'snapshot_config.json'

# Our own GCP project (separate from ik-marketing-data which holds source data).
# Hosts events.event + history.event_snapshot.
BQ_APP_PROJECT = os.environ.get('BQ_APP_PROJECT', 'masterclass-automation-ik')

# macOS Python from python.org often lacks system certs — use unverified context for localhost proxy
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

PORT = int(os.environ.get('PORT', 8080))

SESSION_SECRET  = os.environ.get('SESSION_SECRET', 'dev-only-change-in-prod')
ALLOWED_DOMAIN  = 'interviewkickstart.com'
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID', '')
SESSION_TTL     = 604800  # 7 days

def _make_session(email):
    ts  = str(int(time.time()))
    msg = email + '|' + ts
    sig = hmac.new(SESSION_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return email + '|' + ts + '|' + sig

def _verify_session(token):
    try:
        parts = token.split('|')
        if len(parts) != 3:
            return None
        email, ts, sig = parts
        msg      = email + '|' + ts
        expected = hmac.new(SESSION_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(time.time()) - int(ts) > SESSION_TTL:
            return None
        return email
    except Exception:
        return None

def _session_email(handler):
    for part in handler.headers.get('Cookie', '').split(';'):
        k, _, v = part.strip().partition('=')
        if k.strip() == 'ik_session':
            return _verify_session(v.strip())
    return None

def json_serial(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return str(obj)

def _row_to_dict(r):
    """BigQuery Row → JSON-safe dict. Timestamps → ISO strings, decimals → floats."""
    out = {}
    for k in r.keys():
        v = r.get(k)
        if isinstance(v, (datetime.date, datetime.datetime)):
            out[k] = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            out[k] = float(v)
        else:
            out[k] = v
    return out

def _generate_event_id(body):
    """Slug: <first-4-title-words>-<instructor-initials>-<mmm-dd>."""
    title      = (body.get('title') or '').strip()
    instructor = (body.get('instructorName') or '').strip()
    live_at    = body.get('liveAt') or ''

    words      = re.findall(r'[a-z0-9]+', title.lower())
    title_part = '-'.join(words[:4])[:40] or 'event'
    initials   = ''.join(w[0] for w in instructor.split() if w)[:3].lower()

    date_part = ''
    if live_at:
        try:
            dt = datetime.datetime.fromisoformat(live_at.replace('Z', '+00:00'))
            date_part = dt.strftime('%b%d').lower()
        except Exception:
            pass

    parts = [title_part]
    if initials:  parts.append(initials)
    if date_part: parts.append(date_part)
    return '-'.join(parts)

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_HEAD(self):
        if urllib.parse.urlparse(self.path).path == '/':
            self.send_response(302)
            self.send_header('Location', '/hub.html')
            self.end_headers()
            return
        super().do_HEAD()

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        if path == '/':
            self.send_response(302)
            self.send_header('Location', '/hub.html')
            self.end_headers()
            return
        if path == '/auth/check':
            email = _session_email(self)
            if email:
                self._json_response(200, {'ok': True, 'email': email})
            else:
                self._json_response(401, {'error': 'Not authenticated'})
            return
        if path == '/events' or path.startswith('/events/'):
            if not _session_email(self):
                self._json_response(401, {'error': 'Not authenticated'})
                return
            if path == '/events':
                self._list_events(p)
            else:
                self._get_event(path[len('/events/'):])
            return
        super().do_GET()

    def do_POST(self):
        # Public endpoints — no session required
        if self.path == '/auth/verify':
            self._auth_verify()
            return
        if self.path == '/auth/logout':
            self._auth_logout()
            return
        if self.path == '/run-snapshot':  # called by Cloud Scheduler, no browser session
            self._run_snapshot()
            return
        # All other endpoints require a valid session
        if not _session_email(self):
            self._json_response(401, {'error': 'Not authenticated'})
            return
        if self.path.startswith('/proxy'):
            self._proxy()
        elif self.path == '/bigquery':
            self._bigquery()
        elif self.path == '/save-snapshot-config':
            self._save_snapshot_config()
        elif self.path == '/events':
            self._create_event()
        else:
            self.send_response(404)
            self.end_headers()

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type, Accept')

    def _json_response(self, status, data):
        body = json.dumps(data, default=json_serial).encode()
        self.send_response(status)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        target = params.get('url', [''])[0]
        if not target:
            self.send_response(400)
            self.end_headers()
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else None

        headers = {}
        for h in ['Authorization', 'Content-Type', 'Accept']:
            if self.headers.get(h):
                headers[h] = self.headers[h]

        try:
            req = urllib.request.Request(target, data=body, headers=headers, method='POST')
            with urllib.request.urlopen(req, context=SSL_CTX) as r:
                data = r.read()
                self.send_response(r.status)
                self._cors()
                self.send_header('Content-Type', r.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _bigquery(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        query    = body.get('query', '')
        key_file = body.get('keyFile', '').strip()
        project  = body.get('project', '').strip() or None

        try:
            from google.cloud import bigquery
            from google.oauth2 import service_account

            if key_file:
                creds  = service_account.Credentials.from_service_account_file(key_file)
                client = bigquery.Client(credentials=creds, project=project or creds.project_id)
            else:
                client = bigquery.Client(project=project)

            job    = client.query(query)
            result = job.result()
            schema = [f.name for f in result.schema]
            rows   = [list(row.values()) for row in result]
            self._json_response(200, {'schema': schema, 'rows': rows})

        except ImportError:
            self._json_response(500, {'error': 'google-cloud-bigquery not installed. Run: pip install google-cloud-bigquery'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _list_events(self, parsed_url):
        from google.cloud import bigquery
        qs      = urllib.parse.parse_qs(parsed_url.query)
        status  = qs.get('status', ['upcoming'])[0]
        country = qs.get('country', [None])[0]
        try:
            limit = min(int(qs.get('limit', ['50'])[0]), 200)
        except ValueError:
            limit = 50

        where  = ["COALESCE(e.status, 'upcoming') != 'archived'"]
        params = []
        if status == 'upcoming':
            where.append("e.live_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 4 HOUR)")
        elif status == 'aired':
            where.append("e.live_at <= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 4 HOUR)")
        if country:
            where.append("e.country = @country")
            params.append(bigquery.ScalarQueryParameter('country', 'STRING', country))

        order = 'ASC' if status == 'upcoming' else 'DESC'
        query = f"""
WITH latest_snap AS (
  SELECT *
  FROM `{BQ_APP_PROJECT}.history.event_snapshot`
  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1
)
SELECT
  e.event_id, e.title, e.topic, e.event_type, e.country, e.webinar_type,
  e.live_at, e.day2_live_at, e.go_live_date, e.landing_url,
  e.instructor_name, e.instructor_role, e.goal_regs, e.status,
  s.total_regs, s.meta_regs, s.crm_regs, s.other_regs,
  s.meta_spend, s.cpiql, s.attendees, s.attendance_pct,
  s.hours_to_live, s.snapshot_at
FROM `{BQ_APP_PROJECT}.events.event` e
LEFT JOIN latest_snap s USING (event_id)
WHERE {' AND '.join(where)}
ORDER BY e.live_at {order}
LIMIT {limit}"""

        try:
            client = bigquery.Client(project=BQ_APP_PROJECT)
            rows   = list(client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params)).result())
        except Exception as e:
            self._json_response(500, {'error': str(e)})
            return

        events = []
        for r in rows:
            d = _row_to_dict(r)
            # Reshape: separate snapshot block from event metadata
            snap_keys = ('total_regs', 'meta_regs', 'crm_regs', 'other_regs', 'meta_spend',
                         'cpiql', 'attendees', 'attendance_pct', 'hours_to_live', 'snapshot_at')
            ev = {k: v for k, v in d.items() if k not in snap_keys}
            ev['snapshot'] = {k: d.get(k) for k in snap_keys}
            events.append(ev)
        self._json_response(200, {'events': events})

    def _get_event(self, event_id):
        from google.cloud import bigquery
        if not event_id or '/' in event_id:
            self._json_response(400, {'error': 'Invalid event_id'})
            return
        try:
            client = bigquery.Client(project=BQ_APP_PROJECT)
            param  = [bigquery.ScalarQueryParameter('eid', 'STRING', event_id)]

            ev_rows = list(client.query(
                f"SELECT * FROM `{BQ_APP_PROJECT}.events.event` WHERE event_id = @eid",
                job_config=bigquery.QueryJobConfig(query_parameters=param),
            ).result())
            if not ev_rows:
                self._json_response(404, {'error': 'Event not found'})
                return
            event = _row_to_dict(ev_rows[0])

            snap_rows = list(client.query(
                f"""SELECT * FROM `{BQ_APP_PROJECT}.history.event_snapshot`
                    WHERE event_id = @eid
                    ORDER BY snapshot_at DESC LIMIT 1""",
                job_config=bigquery.QueryJobConfig(query_parameters=param),
            ).result())
            snapshot = _row_to_dict(snap_rows[0]) if snap_rows else None

            daily_rows = list(client.query(
                f"""SELECT * FROM `{BQ_APP_PROJECT}.history.event_daily`
                    WHERE event_id = @eid
                    QUALIFY ROW_NUMBER() OVER (PARTITION BY registration_date ORDER BY snapshot_at DESC) = 1
                    ORDER BY registration_date""",
                job_config=bigquery.QueryJobConfig(query_parameters=param),
            ).result())
            daily = [_row_to_dict(r) for r in daily_rows]

            comp_param = param + [bigquery.ScalarQueryParameter('country', 'STRING', event.get('country') or '')]
            comp_rows = list(client.query(
                f"""WITH latest AS (
                  SELECT *
                  FROM `{BQ_APP_PROJECT}.history.event_snapshot`
                  QUALIFY ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY snapshot_at DESC) = 1
                )
                SELECT e.event_id, e.title, e.instructor_name, e.live_at, e.topic,
                       s.total_regs, s.attendance_pct
                FROM `{BQ_APP_PROJECT}.events.event` e
                LEFT JOIN latest s USING (event_id)
                WHERE e.event_id != @eid
                  AND e.country = @country
                  AND e.live_at < CURRENT_TIMESTAMP()
                  AND COALESCE(e.status, 'upcoming') != 'archived'
                ORDER BY e.live_at DESC LIMIT 3""",
                job_config=bigquery.QueryJobConfig(query_parameters=comp_param),
            ).result())
            comparable = [_row_to_dict(r) for r in comp_rows]

            self._json_response(200, {
                'event': event,
                'snapshot': snapshot,
                'daily': daily,
                'comparable': comparable,
            })
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _create_event(self):
        email  = _session_email(self) or 'unknown'
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length)) if length else {}

        try:
            from google.cloud import bigquery
            client   = bigquery.Client(project=BQ_APP_PROJECT)
            event_id = _generate_event_id(body)
            now_iso  = datetime.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'

            row = {
                'event_id':         event_id,
                'title':            body.get('title') or '',
                'topic':            body.get('topic') or None,
                'event_type':       body.get('eventType') or None,
                'country':          body.get('country') or None,
                'webinar_type':     body.get('webinarType') or None,
                'live_at':          body.get('liveAt') or None,
                'day2_live_at':     body.get('day2LiveAt') or None,
                'go_live_date':     body.get('goLiveDate') or None,
                'landing_url':      body.get('landingUrl') or None,
                'zoom_url':         body.get('zoomUrl') or None,
                'instructor_name':  body.get('instructorName') or None,
                'instructor_role':  body.get('instructorRole') or None,
                'summary':          body.get('summary') or None,
                'design_notes':     body.get('designNotes') or None,
                'goal_regs':        body.get('goalRegs'),
                'status':           'upcoming',
                'jira_design_key':  body.get('jiraDesignKey') or None,
                'jira_landing_key': body.get('jiraLandingKey') or None,
                'created_at':       now_iso,
                'created_by':       email,
                'updated_at':       now_iso,
            }

            table  = f'{BQ_APP_PROJECT}.events.event'
            errors = client.insert_rows_json(table, [row])
            if errors:
                self._json_response(500, {'error': 'BigQuery insert failed', 'details': errors})
                return

            self._json_response(200, {'ok': True, 'event_id': event_id})

        except ImportError:
            self._json_response(500, {'error': 'google-cloud-bigquery not installed'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _save_snapshot_config(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        try:
            save_snapshot_config(body)
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _run_snapshot(self):
        try:
            _do_snapshot()
            self._json_response(200, {'ok': True})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _auth_verify(self):
        length = int(self.headers.get('Content-Length', 0))
        body   = json.loads(self.rfile.read(length)) if length else {}
        credential = body.get('credential', '')
        if not GOOGLE_CLIENT_ID:
            self._json_response(500, {'error': 'GOOGLE_CLIENT_ID not configured on server.'})
            return
        try:
            from google.oauth2 import id_token
            from google.auth.transport import requests as grequests
            idinfo = id_token.verify_oauth2_token(credential, grequests.Request(), GOOGLE_CLIENT_ID)
            email  = idinfo.get('email', '')
            hd     = idinfo.get('hd', '')
            if not (hd == ALLOWED_DOMAIN or email.endswith('@' + ALLOWED_DOMAIN)):
                self._json_response(403, {'error': 'Only @interviewkickstart.com accounts are allowed.'})
                return
            token = _make_session(email)
            self.send_response(200)
            self._cors()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Set-Cookie',
                'ik_session=' + token + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=' + str(SESSION_TTL))
            self.end_headers()
            self.wfile.write(json.dumps({'ok': True, 'email': email}).encode())
        except Exception as e:
            self._json_response(403, {'error': str(e)})

    def _auth_logout(self):
        self.send_response(200)
        self._cors()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Set-Cookie', 'ik_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True}).encode())

    def log_message(self, fmt, *args):
        print(f"  {args[0]} {args[1]}")

SNAPSHOT_CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'snapshot_config.json')

def load_snapshot_config():
    if GCS_BUCKET:
        try:
            from google.cloud import storage
            blob = storage.Client().bucket(GCS_BUCKET).blob(GCS_CONFIG_KEY)
            return json.loads(blob.download_as_text())
        except Exception:
            return {}
    try:
        with open(SNAPSHOT_CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_snapshot_config(body):
    if GCS_BUCKET:
        from google.cloud import storage
        blob = storage.Client().bucket(GCS_BUCKET).blob(GCS_CONFIG_KEY)
        blob.upload_from_string(json.dumps(body, indent=2), content_type='application/json')
    else:
        with open(SNAPSHOT_CONFIG_FILE, 'w') as f:
            json.dump(body, f, indent=2)

def build_snapshot_message(rows, schema):
    col_map = {name: i for i, name in enumerate(schema)}
    date_idx  = col_map.get('registration_date', 0)
    chan_idx   = col_map.get('channel', 1)
    count_idx  = col_map.get('total_leads', 3)
    spend_idx  = col_map.get('total_spend', 4)

    buckets = {'facebook': {}, 'crm': {}, 'others': {}, 'overall': {}}
    spends  = {}
    seen    = set()

    def is_fb(ch):  return bool(__import__('re').search(r'facebook|fb', ch or '', flags=2))
    def is_crm(ch): return bool(__import__('re').search(r'email|whatsapp|whats.?app', ch or '', flags=2))

    for row in rows:
        d     = str(row[date_idx] or '')[:10]
        ch    = str(row[chan_idx] or '')
        count = float(row[count_idx] or 0)
        spend = float(row[spend_idx] or 0)
        if spend and d not in seen:
            seen.add(d)
            spends[d] = spends.get(d, 0) + spend
        buckets['overall'][d] = buckets['overall'].get(d, 0) + count
        if is_fb(ch):   buckets['facebook'][d] = buckets['facebook'].get(d, 0) + count
        elif is_crm(ch):buckets['crm'][d]      = buckets['crm'].get(d, 0) + count
        else:            buckets['others'][d]   = buckets['others'].get(d, 0) + count

    all_dates = sorted(buckets['overall'].keys())
    lines = ['*Registration Summary*', '```']
    hdr = f"{'Date':<12}{'Spends (₹)':>14}{'Meta':>7}{'CPIQL':>8}{'CRM':>6}{'Others':>8}{'Overall':>9}"
    lines.append(hdr)
    lines.append('-' * 64)
    t = {'spends': 0, 'meta': 0, 'crm': 0, 'others': 0, 'overall': 0}
    for d in all_dates:
        sp  = spends.get(d, 0)
        meta= buckets['facebook'].get(d, 0)
        crm = buckets['crm'].get(d, 0)
        oth = buckets['others'].get(d, 0)
        all_= buckets['overall'].get(d, 0)
        cpiql = f'{sp/meta:.2f}' if meta > 0 else '—'
        sp_fmt = f'{round(sp):,}' if sp else '—'
        lines.append(f"{d:<12}{sp_fmt:>14}{int(meta):>7}{cpiql:>8}{int(crm):>6}{int(oth):>8}{int(all_):>9}")
        t['spends'] += sp; t['meta'] += meta; t['crm'] += crm; t['others'] += oth; t['overall'] += all_
    lines.append('-' * 64)
    tc = f"{t['spends']/t['meta']:.2f}" if t['meta'] > 0 else '—'
    total_sp_fmt = f"{round(t['spends']):,}"
    lines.append(f"{'Total':<12}{total_sp_fmt:>14}{int(t['meta']):>7}{tc:>8}{int(t['crm']):>6}{int(t['others']):>8}{int(t['overall']):>9}")
    lines.append('```')
    return '\n'.join(lines)

def send_slack_dm(token, user_id, text):
    def post(url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
              headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, context=SSL_CTX) as r:
            return json.loads(r.read())
    ch = post('https://slack.com/api/conversations.open', {'users': user_id})
    if not ch.get('ok'): raise Exception(ch.get('error'))
    post('https://slack.com/api/chat.postMessage', {'channel': ch['channel']['id'], 'text': text})

def _do_snapshot():
    """Two independent paths: (1) legacy Slack DM from snapshot_config;
    (2) per-event BQ snapshot writes to history.event_daily + history.event_snapshot.
    Failure of one doesn't block the other."""
    errors = []
    try:
        _snapshot_to_slack()
    except Exception as e:
        errors.append(f'slack: {e}')
        print(f'  [snapshot/slack] error: {e}')
    try:
        _snapshot_events_to_bq()
    except Exception as e:
        errors.append(f'bq: {e}')
        print(f'  [snapshot/bq] error: {e}')
    if len(errors) == 2:
        raise Exception('; '.join(errors))

def _snapshot_to_slack():
    """Legacy daily Slack DM driven by snapshot_config (will be replaced in slice 3+)."""
    cfg = load_snapshot_config()
    if not cfg.get('slackToken') or not cfg.get('slackSnapshotId') or not cfg.get('webinarDates'):
        raise Exception('snapshot config missing')
    from google.cloud import bigquery
    date_literal = ', '.join(f"'{d}'" for d in cfg['webinarDates'])
    webinar_type = cfg.get('webinarType', '')
    query = f"""
WITH leads AS (
  SELECT DATE(formatted_date) AS registration_date, channel, utm_campaign, COUNT(*) AS total_leads
  FROM (
    SELECT formatted_date, channel, utm_campaign,
      DATE(event_start_date_time, "Asia/Kolkata") AS web_scheduled_date,
      hubspot_ID, dupe_flag, gql_flag, work_ex, webinar_type, dupe_logic,
      ROW_NUMBER() OVER (PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata") ORDER BY formatted_date ASC) AS rnk
    FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
    WHERE dupe_logic = 1
  )
  WHERE rnk = 1 AND web_scheduled_date IN ({date_literal}) AND webinar_type = '{webinar_type}'
    AND dupe_flag = 0 AND gql_flag = 0
    AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2', '3-4')
  GROUP BY registration_date, channel, utm_campaign
),
spends AS (
  SELECT DATE(campaign_date) AS spend_date, campaign_name, SUM(cost) AS total_spend
  FROM `ik-marketing-data.India_Leads.Combined_India_Spend`
  WHERE (LOWER(campaign_name) LIKE '%l10x%' OR LOWER(campaign_name) LIKE '%masterclass%')
    AND (LOWER(campaign_name) LIKE '%meta%' OR LOWER(campaign_name) LIKE '%facebook%' OR LOWER(campaign_name) LIKE '%l10x%')
  GROUP BY spend_date, campaign_name
)
SELECT l.registration_date, l.channel, l.utm_campaign, l.total_leads, SUM(s.total_spend) AS total_spend
FROM leads l
LEFT JOIN spends s ON l.registration_date = s.spend_date
  AND LOWER(l.utm_campaign) LIKE CONCAT('%', LOWER(s.campaign_name), '%')
GROUP BY l.registration_date, l.channel, l.utm_campaign, l.total_leads
ORDER BY l.registration_date, l.channel, l.utm_campaign"""

    client = bigquery.Client(project='ik-marketing-data')
    result = client.query(query).result()
    schema = [f.name for f in result.schema]
    rows   = [list(row.values()) for row in result]
    msg = build_snapshot_message(rows, schema)
    send_slack_dm(cfg['slackToken'], cfg['slackSnapshotId'], msg)
    print(f'  [snapshot/slack] sent to {cfg["slackSnapshotId"]}')

# ─── Per-event BQ snapshots ──────────────────────────────────────────────────

# Channel classifiers — match the existing Slack snapshot logic.
_RE_META = re.compile(r'facebook|fb', re.I)
_RE_CRM  = re.compile(r'email|whatsapp|whats.?app', re.I)

def _snapshot_events_to_bq():
    """For each active event, query lead funnel and write rows to
    history.event_daily (per registration_date) + history.event_snapshot (cumulative)."""
    from google.cloud import bigquery
    client  = bigquery.Client(project=BQ_APP_PROJECT)
    now     = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat().replace('+00:00', 'Z')

    # Active = within 14d past or 60d future of live_at, not archived, has webinar_type
    events = list(client.query(f"""
        SELECT event_id, country, webinar_type, live_at, day2_live_at
        FROM `{BQ_APP_PROJECT}.events.event`
        WHERE COALESCE(status, 'upcoming') != 'archived'
          AND webinar_type IS NOT NULL
          AND live_at IS NOT NULL
          AND live_at BETWEEN TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
                          AND TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 60 DAY)
    """).result())

    print(f'  [snapshot/bq] {len(events)} active event(s)')

    daily_rows = []
    snap_rows  = []
    for ev in events:
        try:
            result = _build_event_snapshot(client, ev, now, now_iso)
            if result is None:
                print(f'  [snapshot/bq] {ev.event_id}: country={ev.country} not supported yet, skipping')
                continue
            d, s = result
            daily_rows.extend(d)
            snap_rows.append(s)
            print(f'  [snapshot/bq] {ev.event_id}: {len(d)} daily row(s), total_regs={s["total_regs"]}')
        except Exception as e:
            print(f'  [snapshot/bq] {ev.event_id}: error: {e}')

    if daily_rows:
        errs = client.insert_rows_json(f'{BQ_APP_PROJECT}.history.event_daily', daily_rows)
        if errs:
            print(f'  [snapshot/bq] event_daily insert errors: {errs}')
    if snap_rows:
        errs = client.insert_rows_json(f'{BQ_APP_PROJECT}.history.event_snapshot', snap_rows)
        if errs:
            print(f'  [snapshot/bq] event_snapshot insert errors: {errs}')

def _build_event_snapshot(client, ev, now, now_iso):
    """Returns (daily_rows, snapshot_row) for one event, or None if country unsupported.

    v2 (India only): runs 4 queries per event — leads/spends, cohort role+work_ex,
    attendance from Zoom, call efforts, email reminder funnel. Each post-event query
    is wrapped in try/except so a single failure doesn't blank out the whole row."""
    from google.cloud import bigquery
    if (ev.country or '').lower() != 'india':
        return None

    live_dates = []
    for d in [ev.live_at, ev.day2_live_at]:
        if d is None: continue
        live_dates.append(d.date() if isinstance(d, datetime.datetime) else d)
    if not live_dates:
        return [], _cumulative_snapshot_row(ev.event_id, {}, ev.live_at, now, now_iso)

    date_literal = ', '.join(f"DATE '{d.isoformat()}'" for d in live_dates)
    src_client   = bigquery.Client(project='ik-marketing-data')
    wt_param     = [bigquery.ScalarQueryParameter('wt', 'STRING', ev.webinar_type)]

    # ─── 1. Leads + spends (per-day buckets) ──────────────────────────────────
    query = f"""
WITH leads AS (
  SELECT DATE(formatted_date) AS registration_date, channel, utm_campaign, COUNT(*) AS total_leads
  FROM (
    SELECT formatted_date, channel, utm_campaign,
      DATE(event_start_date_time, "Asia/Kolkata") AS web_scheduled_date,
      hubspot_ID, dupe_flag, gql_flag, work_ex, webinar_type, dupe_logic,
      ROW_NUMBER() OVER (PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata") ORDER BY formatted_date ASC) AS rnk
    FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
    WHERE dupe_logic = 1
  )
  WHERE rnk = 1 AND web_scheduled_date IN ({date_literal}) AND webinar_type = @wt
    AND dupe_flag = 0 AND gql_flag = 0
    AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2', '3-4')
  GROUP BY registration_date, channel, utm_campaign
),
spends AS (
  SELECT DATE(campaign_date) AS spend_date, campaign_name, SUM(cost) AS total_spend
  FROM `ik-marketing-data.India_Leads.Combined_India_Spend`
  WHERE (LOWER(campaign_name) LIKE '%l10x%' OR LOWER(campaign_name) LIKE '%masterclass%')
    AND (LOWER(campaign_name) LIKE '%meta%' OR LOWER(campaign_name) LIKE '%facebook%' OR LOWER(campaign_name) LIKE '%l10x%')
  GROUP BY spend_date, campaign_name
)
SELECT l.registration_date, l.channel, l.total_leads, SUM(s.total_spend) AS total_spend
FROM leads l
LEFT JOIN spends s ON l.registration_date = s.spend_date
  AND LOWER(l.utm_campaign) LIKE CONCAT('%', LOWER(s.campaign_name), '%')
GROUP BY l.registration_date, l.channel, l.total_leads
ORDER BY l.registration_date, l.channel"""

    job = src_client.query(query, job_config=bigquery.QueryJobConfig(query_parameters=wt_param))
    rows = list(job.result())

    # Bucket per (date, channel-class). Spend dedup per date matches old Slack logic.
    daily = {}
    spend_seen = set()
    for r in rows:
        d = r['registration_date'].isoformat() if r['registration_date'] else None
        if not d: continue
        ch    = r['channel'] or ''
        count = float(r['total_leads'] or 0)
        spend = float(r['total_spend'] or 0)

        b = daily.setdefault(d, {
            'meta_regs': 0.0, 'meta_spend': 0.0,
            'crm_regs': 0.0,
            'other_regs': 0.0, 'other_spend': 0.0,
        })
        if spend and d not in spend_seen:
            spend_seen.add(d)
            b['meta_spend'] += spend  # source query only pulls meta-flavored spends
        if   _RE_META.search(ch): b['meta_regs']  += count
        elif _RE_CRM.search(ch):  b['crm_regs']   += count
        else:                     b['other_regs'] += count

    daily_rows = []
    for d, b in sorted(daily.items()):
        total_regs  = b['meta_regs'] + b['crm_regs'] + b['other_regs']
        total_spend = b['meta_spend'] + b['other_spend']
        cpiql       = (b['meta_spend'] / b['meta_regs']) if b['meta_regs'] > 0 else None
        daily_rows.append({
            'event_id':          ev.event_id,
            'registration_date': d,
            'snapshot_at':       now_iso,
            'meta_regs':         int(b['meta_regs']),
            'meta_spend':        b['meta_spend'],
            'crm_regs':          int(b['crm_regs']),
            'other_regs':        int(b['other_regs']),
            'other_spend':       b['other_spend'],
            'total_regs':        int(total_regs),
            'total_spend':       total_spend,
            'cpiql':             cpiql,
            'extras':            None,
        })

    # ─── 2. Cohort (role category + work_ex) ──────────────────────────────────
    cohort = _safe_query(_query_cohort_india, src_client, date_literal, wt_param,
                         label=f'cohort/{ev.event_id}',
                         default={'role_sde': None, 'role_ml': None, 'role_fe': None, 'role_other': None,
                                  'we_0_2': None, 'we_3_5': None, 'we_6_10': None, 'we_10p': None})

    # ─── 3. Attendance from Zoom roster ───────────────────────────────────────
    attendance = _safe_query(_query_attendance_india, src_client, date_literal, wt_param,
                             label=f'attendance/{ev.event_id}',
                             default={'attendees': None, 'attendance_pct': None})

    # ─── 4. Call efforts ──────────────────────────────────────────────────────
    call_data = _safe_query(_query_calls_india, src_client, date_literal, wt_param,
                            label=f'calls/{ev.event_id}',
                            default={'calls_attempted': None, 'calls_connected': None, 'avg_talk_seconds': None})

    # ─── 5. Email reminder funnel ─────────────────────────────────────────────
    email = _safe_query(_query_emails_india, src_client, date_literal, wt_param,
                        label=f'emails/{ev.event_id}',
                        default={'email_sent': None, 'email_delivered': None, 'email_opened': None, 'email_clicked': None})

    snap_row = _cumulative_snapshot_row(ev.event_id, daily, ev.live_at, now, now_iso,
                                        cohort=cohort, attendance=attendance,
                                        call_data=call_data, email=email)
    return daily_rows, snap_row

def _safe_query(fn, *args, label, default, **kw):
    """Run a query function; on failure, log and return defaults so the cron continues."""
    try:
        return fn(*args, **kw)
    except Exception as e:
        print(f'  [snapshot/bq] {label} failed: {e}')
        return default

def _query_cohort_india(src_client, date_literal, wt_param):
    """Aggregate qualified IQLs by role category + work_ex bucket.
    Maps source 5-category role split into our 4-bucket schema (role_fe is always
    None — source 'Software Engineer' bucket already includes Front-end/Full-stack).
    we_0_2 is always 0 because the source query filters out 0-2 / 3-4 / student leads."""
    from google.cloud import bigquery
    q = f"""
SELECT
  CASE
    WHEN role_domain IN ('Data Science','Data Engineer','Machine Learning / AI','ML / AI','Data Engineer / Data Scientist','Business Intelligence Analyst','Data Analyst / Business Analyst','Data','Machine Learning/DeepLearning','Data Engineering') THEN 'Data'
    WHEN role_domain IS NULL OR role_domain IN ('No / Little coding experience','No Coding Experience','None of the above') THEN 'Null'
    WHEN role_domain IN ('Full Stack','Back-end','Other Software Engineers','iOS Developer','Android Developer','iOS / Android Developer','Front-end','Test Engineer / SDET / QE','QA / Testing','Software Engineer','Front-end / Full stack','Software Engineering','Software Engineering (Frontend, Fullstack, Backend, Test)','Mobile Engineering (iOS/Android)','Core Engineering') THEN 'Software Engineer'
    WHEN role_domain IN ('Engineering Manager - any domain','Product Manager (Tech)','Technical Program Manager','Engineering Manager / Director of Engineering','Project Manager / Product Manager','Engineering Manager','Growth Product Manager','Product Marketing Manager','Tech Product Manager','Management') THEN 'Management'
    WHEN role_domain IN ('SRE / DevOps','Cyber Security','Embedded Software Engineer','Cloud Engineer','Application Packaging Engineer','AWS Cloud Solutions Architect','Cyber Security/Security Engineering','Embedded Systems','DevOps Engineer','Site Reliability Engineer','Site Reliability Engineering') THEN 'Systems'
    ELSE 'Other'
  END AS category,
  CASE
    WHEN work_ex IN ('0-2','0-5','3-4') THEN 'a05'
    WHEN work_ex IN ('5-8','5-10')      THEN 'b510'
    WHEN work_ex IN ('9-15','10-15')    THEN 'c1015'
    WHEN work_ex IN ('16-20','15-20','15+') THEN 'd1520'
    WHEN work_ex = '20+'               THEN 'e20p'
    ELSE 'other'
  END AS we_bucket,
  COUNT(*) AS cnt
FROM (
  SELECT role_domain, work_ex,
    DATE(event_start_date_time, "Asia/Kolkata") AS web_scheduled_date,
    dupe_flag, gql_flag, webinar_type, dupe_logic,
    ROW_NUMBER() OVER (
      PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata")
      ORDER BY formatted_date ASC
    ) AS rnk
  FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
  WHERE dupe_logic = 1
)
WHERE rnk = 1
  AND web_scheduled_date IN ({date_literal})
  AND webinar_type = @wt
  AND dupe_flag = 0 AND gql_flag = 0
  AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2','3-4')
GROUP BY 1, 2"""
    rows = list(src_client.query(q, job_config=bigquery.QueryJobConfig(query_parameters=wt_param)).result())

    out = {'role_sde': 0, 'role_ml': 0, 'role_fe': None, 'role_other': 0,
           'we_0_2': 0, 'we_3_5': 0, 'we_6_10': 0, 'we_10p': 0}
    for r in rows:
        cat = r['category']
        we  = r['we_bucket']
        cnt = int(r['cnt'] or 0)
        if   cat == 'Software Engineer': out['role_sde']   += cnt
        elif cat == 'Data':              out['role_ml']    += cnt
        else:                            out['role_other'] += cnt
        if   we == 'a05':                out['we_3_5']  += cnt   # 0-2 / 3-4 filtered out; only 5 remains
        elif we == 'b510':               out['we_6_10'] += cnt
        elif we in ('c1015', 'd1520', 'e20p'): out['we_10p'] += cnt
    return out

def _query_attendance_india(src_client, date_literal, wt_param):
    """Returns {attendees, attendance_pct}. attendees = unique IQLs in Zoom roster."""
    from google.cloud import bigquery
    q = f"""
WITH attendance AS (
  SELECT CAST(hubspot_id AS INT64) AS hubspot_id
  FROM `ik-marketing-data.Webinar_analytics.webinar_attendee_data_from_zoom`
  WHERE DATE(webinar_start_time, "Asia/Kolkata") IN ({date_literal})
    AND hubspot_id IS NOT NULL
  GROUP BY hubspot_id
),
leads AS (
  SELECT CAST(hubspot_ID AS INT64) AS hubspot_id
  FROM (
    SELECT hubspot_ID, formatted_date,
      DATE(event_start_date_time, "Asia/Kolkata") AS web_scheduled_date,
      dupe_flag, gql_flag, work_ex, webinar_type, dupe_logic,
      ROW_NUMBER() OVER (
        PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata")
        ORDER BY formatted_date ASC
      ) AS rnk
    FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
    WHERE dupe_logic = 1
  )
  WHERE rnk = 1
    AND web_scheduled_date IN ({date_literal})
    AND webinar_type = @wt
    AND dupe_flag = 0 AND gql_flag = 0
    AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2','3-4')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY hubspot_ID ORDER BY formatted_date ASC) = 1
)
SELECT
  COUNT(*) AS total_leads,
  COUNTIF(a.hubspot_id IS NOT NULL) AS attendees
FROM leads l
LEFT JOIN attendance a ON l.hubspot_id = a.hubspot_id"""
    r = list(src_client.query(q, job_config=bigquery.QueryJobConfig(query_parameters=wt_param)).result())
    if not r:
        return {'attendees': None, 'attendance_pct': None}
    total    = int(r[0]['total_leads'] or 0)
    attended = int(r[0]['attendees'] or 0)
    pct      = (100.0 * attended / total) if total > 0 else None
    return {'attendees': attended, 'attendance_pct': pct}

def _query_calls_india(src_client, date_literal, wt_param):
    """Returns {calls_attempted, calls_connected, avg_talk_seconds} aggregated across channels."""
    from google.cloud import bigquery
    q = f"""
WITH base AS (
  SELECT
    lead_created_time AS Lead_created_time,
    DATE(DATETIME(Event_Start_Date_Time, 'Asia/Kolkata')) AS webinar_start_date,
    DATETIME(Event_Start_Date_Time, 'Asia/Kolkata') AS webinar_start_datetime,
    COALESCE(
      LEAD(lead_created_time) OVER (PARTITION BY lead_email ORDER BY lead_created_time),
      DATETIME_ADD(lead_created_time, INTERVAL 1 YEAR)
    ) AS next_lead_time,
    hubspot_id
  FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
  WHERE DATE(DATETIME(Event_Start_Date_Time, 'Asia/Kolkata')) IN ({date_literal})
    AND webinar_type = @wt
    AND dupe_logic = 1 AND dupe_flag = 0 AND gql_flag = 0
    AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2','3-4')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY hubspot_id ORDER BY lead_created_time ASC) = 1
),
base_call AS (
  SELECT activity_datetime, email_11, call_duration FROM (
    SELECT
      CASE WHEN EXTRACT(MONTH FROM DATE(DATETIME(timestamp, "Asia/Kolkata"))) IN (11,12,1,2,3)
           THEN DATETIME(timestamp, "Asia/Kolkata") + INTERVAL 13 HOUR + INTERVAL 30 MINUTE
           ELSE DATETIME(timestamp, "Asia/Kolkata") + INTERVAL 12 HOUR + INTERVAL 30 MINUTE
      END AS activity_datetime,
      hubspot_id AS email_11,
      duration AS call_duration,
      ROW_NUMBER() OVER (PARTITION BY call_id ORDER BY
        CASE WHEN EXTRACT(MONTH FROM DATE(DATETIME(timestamp, "Asia/Kolkata"))) IN (11,12,1,2,3)
             THEN DATETIME(timestamp, "Asia/Kolkata") + INTERVAL 13 HOUR + INTERVAL 30 MINUTE
             ELSE DATETIME(timestamp, "Asia/Kolkata") + INTERVAL 12 HOUR + INTERVAL 30 MINUTE
        END DESC) AS rn
    FROM `ik-marketing-data.Marketing_data_new_logic.call_metadata`
    WHERE DATETIME(timestamp, "Asia/Kolkata") >= DATETIME '2024-07-01'
      AND hubspot_id IN (SELECT hubspot_id FROM base)
  ) WHERE rn = 1
),
fin AS (
  SELECT bm.Lead_created_time, bm.hubspot_id AS lead_email, bm.next_lead_time,
    COALESCE(bm.webinar_start_datetime, DATETIME_ADD(bm.Lead_created_time, INTERVAL 1 YEAR)) AS webinar_start_datetime_ch,
    bc.activity_datetime, bc.call_duration
  FROM base bm
  LEFT JOIN base_call bc
    ON bm.hubspot_id = bc.email_11
    AND bm.Lead_created_time <= bc.activity_datetime
    AND bm.next_lead_time > bc.activity_datetime
)
SELECT
  COUNT(CASE WHEN activity_datetime < webinar_start_datetime_ch THEN 1 END) AS total_calls,
  COUNT(CASE WHEN activity_datetime < webinar_start_datetime_ch AND call_duration > 120 THEN 1 END) AS total_connected,
  SUM(CASE WHEN activity_datetime < webinar_start_datetime_ch THEN call_duration END) AS total_talk_sec
FROM fin"""
    r = list(src_client.query(q, job_config=bigquery.QueryJobConfig(query_parameters=wt_param)).result())
    if not r:
        return {'calls_attempted': None, 'calls_connected': None, 'avg_talk_seconds': None}
    attempted = int(r[0]['total_calls'] or 0)
    connected = int(r[0]['total_connected'] or 0)
    talk_sec  = float(r[0]['total_talk_sec'] or 0)
    avg_talk  = (talk_sec / connected) if connected > 0 else None
    return {'calls_attempted': attempted, 'calls_connected': connected, 'avg_talk_seconds': avg_talk}

def _query_emails_india(src_client, date_literal, wt_param):
    """Returns {email_sent, email_delivered, email_opened, email_clicked} for reminder campaigns."""
    from google.cloud import bigquery
    q = f"""
WITH registered_leads AS (
  SELECT CAST(hubspot_ID AS INT64) AS hubspot_id
  FROM (
    SELECT hubspot_ID, formatted_date,
      DATE(event_start_date_time, "Asia/Kolkata") AS web_scheduled_date,
      dupe_flag, gql_flag, work_ex, webinar_type, dupe_logic,
      ROW_NUMBER() OVER (
        PARTITION BY hubspot_ID, DATE(event_start_date_time, "Asia/Kolkata")
        ORDER BY formatted_date ASC
      ) AS rnk
    FROM `ik-marketing-data.India_Leads.US_Domain_combined_view`
    WHERE dupe_logic = 1
  )
  WHERE rnk = 1
    AND web_scheduled_date IN ({date_literal})
    AND webinar_type = @wt
    AND dupe_flag = 0 AND gql_flag = 0
    AND LOWER(work_ex) NOT LIKE '%student%' AND work_ex NOT IN ('0-2','3-4')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY hubspot_ID ORDER BY formatted_date ASC) = 1
),
email_sent AS (
  SELECT CAST(hubspot_id AS INT64) AS hubspot_id, email_campaign_id
  FROM `ik-marketing-data.Email.Marketing_Email_Data`
  WHERE CAST(hubspot_id AS INT64) IN (SELECT hubspot_id FROM registered_leads)
    AND DATE(event_timestamp, "Asia/Kolkata") IN ({date_literal})
    AND event_name = 'SENT'
    AND LOWER(email_name) LIKE '%reminder%'
    AND NOT REGEXP_CONTAINS(email_name, r'(?i)_eu_')
  GROUP BY hubspot_id, email_campaign_id
),
delivered_events AS (
  SELECT DISTINCT CAST(hubspot_id AS INT64) AS hubspot_id
  FROM `ik-marketing-data.Email.Marketing_Email_Data`
  WHERE CAST(hubspot_id AS INT64) IN (SELECT hubspot_id FROM email_sent)
    AND event_name = 'DELIVERED'
),
engagement_events AS (
  SELECT CAST(e.hubspot_id AS INT64) AS hubspot_id, s.email_campaign_id, e.event_name
  FROM `ik-marketing-data.Email.Marketing_Email_Data` e
  INNER JOIN email_sent s
    ON CAST(e.hubspot_id AS INT64) = s.hubspot_id AND e.email_campaign_id = s.email_campaign_id
  WHERE e.event_name IN ('OPEN','CLICK')
),
funnel AS (
  SELECT
    s.hubspot_id, s.email_campaign_id,
    1 AS is_sent,
    MAX(CASE
      WHEN d.hubspot_id IS NOT NULL THEN 1
      WHEN eng.event_name IN ('OPEN','CLICK') THEN 1
      ELSE 0
    END) AS is_delivered,
    MAX(CASE WHEN eng.event_name IN ('OPEN','CLICK') THEN 1 ELSE 0 END) AS is_opened,
    MAX(CASE WHEN eng.event_name = 'CLICK' THEN 1 ELSE 0 END) AS is_clicked
  FROM email_sent s
  LEFT JOIN delivered_events d ON s.hubspot_id = d.hubspot_id
  LEFT JOIN engagement_events eng ON s.hubspot_id = eng.hubspot_id AND s.email_campaign_id = eng.email_campaign_id
  GROUP BY s.hubspot_id, s.email_campaign_id
)
SELECT
  COUNT(*)                                 AS sent,
  SUM(is_delivered)                        AS delivered,
  SUM(is_opened)                           AS opened,
  SUM(is_clicked)                          AS clicked
FROM funnel"""
    r = list(src_client.query(q, job_config=bigquery.QueryJobConfig(query_parameters=wt_param)).result())
    if not r:
        return {'email_sent': None, 'email_delivered': None, 'email_opened': None, 'email_clicked': None}
    return {
        'email_sent':      int(r[0]['sent']      or 0),
        'email_delivered': int(r[0]['delivered'] or 0),
        'email_opened':    int(r[0]['opened']    or 0),
        'email_clicked':   int(r[0]['clicked']   or 0),
    }

def _cumulative_snapshot_row(event_id, daily, live_at, now, now_iso,
                              cohort=None, attendance=None, call_data=None, email=None):
    """Sum daily buckets to a single point-in-time snapshot row, merging in
    cohort + attendance + calls + email metrics if available."""
    meta_regs   = sum(b['meta_regs']   for b in daily.values())
    meta_spend  = sum(b['meta_spend']  for b in daily.values())
    crm_regs    = sum(b['crm_regs']    for b in daily.values())
    other_regs  = sum(b['other_regs']  for b in daily.values())
    other_spend = sum(b['other_spend'] for b in daily.values())
    total_regs  = meta_regs + crm_regs + other_regs

    hours_to_live = None
    if live_at is not None:
        live_at_aware = live_at if live_at.tzinfo else live_at.replace(tzinfo=datetime.timezone.utc)
        hours_to_live = (live_at_aware - now).total_seconds() / 3600.0

    row = {
        'event_id':         event_id,
        'snapshot_at':      now_iso,
        'hours_to_live':    hours_to_live,
        'total_regs':       int(total_regs),
        'meta_regs':        int(meta_regs),
        'meta_spend':       meta_spend,
        'crm_regs':         int(crm_regs),
        'other_regs':       int(other_regs),
        'other_spend':      other_spend,
        'cpiql':            (meta_spend / meta_regs) if meta_regs > 0 else None,
        'attendees': None, 'attendance_pct': None,
        'email_sent': None, 'email_delivered': None, 'email_opened': None, 'email_clicked': None,
        'calls_attempted': None, 'calls_connected': None, 'avg_talk_seconds': None,
        'role_sde': None, 'role_ml': None, 'role_fe': None, 'role_other': None,
        'we_0_2': None, 'we_3_5': None, 'we_6_10': None, 'we_10p': None,
        'extras': None,
    }
    for partial in (cohort, attendance, call_data, email):
        if partial:
            row.update(partial)
    return row

def run_daily_snapshot():
    last_sent = None
    while True:
        now = datetime.datetime.now()
        if now.hour == 11 and now.minute < 2 and now.date() != last_sent:
            try:
                _do_snapshot()
                last_sent = now.date()
            except Exception as e:
                print(f'  [snapshot] error: {e}')
        time.sleep(60)

if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    threading.Thread(target=run_daily_snapshot, daemon=True).start()
    print(f'Serving at http://localhost:{PORT}')
    print(f'Open: http://localhost:{PORT}/Masterclass%20Automation.html')
    http.server.HTTPServer(('', PORT), Handler).serve_forever()
