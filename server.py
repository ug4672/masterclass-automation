#!/usr/bin/env python3
"""Local proxy server — serves the HTML and forwards API calls to Jira/Slack/BigQuery."""
import http.server, urllib.request, urllib.parse, urllib.error, json, os, ssl, decimal, datetime
import threading, time

GCS_BUCKET = os.environ.get('GCS_BUCKET', '')
GCS_CONFIG_KEY = 'snapshot_config.json'

# macOS Python from python.org often lacks system certs — use unverified context for localhost proxy
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

PORT = int(os.environ.get('PORT', 8080))

def json_serial(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    return str(obj)

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path.startswith('/proxy'):
            self._proxy()
        elif self.path == '/bigquery':
            self._bigquery()
        elif self.path == '/save-snapshot-config':
            self._save_snapshot_config()
        elif self.path == '/run-snapshot':
            self._run_snapshot()
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
    print(f'  [snapshot] sent to {cfg["slackSnapshotId"]}')

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
