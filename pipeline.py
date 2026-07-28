#!/usr/bin/env python3
"""
QCG Call Intelligence — Nightly Automation Pipeline
Runs via GitHub Actions at 11pm every night.
Pulls recordings from 8x8 Work, processes through Whisper + GPT-4o,
writes results to Google Sheet via Apps Script.
"""

import os
import json
import time
import base64
import hashlib
import hmac
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────
# These are set as GitHub Secrets — never hardcode them here

EIGHT_BY_EIGHT_KEY    = os.environ.get('EIGHT_BY_EIGHT_KEY', '')
EIGHT_BY_EIGHT_SECRET = os.environ.get('EIGHT_BY_EIGHT_SECRET', '')
EIGHT_BY_EIGHT_PBX_ID = os.environ.get('EIGHT_BY_EIGHT_PBX_ID', '')  # fill in when known
OPENAI_KEY            = os.environ.get('OPENAI_KEY', '')
SHEETS_URL            = os.environ.get('SHEETS_URL', '')  # Apps Script /exec URL

# Reps — maps phone extension or user to rep name
# Add your reps' 8x8 email/extension here once you know them
REP_MAP = {
    # 'steve.taylor@qcaregroup.co.uk':    'Steve Taylor',
    # 'lucy.sandle@qcaregroup.co.uk':     'Lucy Sandle',
    # 'ryan.davenport@qcaregroup.co.uk':  'Ryan Davenport',
    # 'cameron.montrose@qcaregroup.co.uk':'Cameron Montrose',
    # 'sara.bosworth@qcaregroup.co.uk':   'Sara Bosworth',
}
REP_NAMES = ['Steve Taylor','Lucy Sandle','Ryan Davenport','Cameron Montrose','Sara Bosworth']

# How many days back to pull recordings
DAYS_BACK = 2

# ── UTILITIES ─────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method='GET')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

def http_post(url, data, headers=None):
    body = json.dumps(data).encode('utf-8')
    req  = urllib.request.Request(url, data=body, headers=headers or {}, method='POST')
    req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode('utf-8'))

# ── 8x8 AUTHENTICATION ────────────────────────────────────────────────────

# 8x8 Cloud Storage Service region — eu for UK accounts
EIGHT_BY_EIGHT_REGION = os.environ.get('EIGHT_BY_EIGHT_REGION', 'eu')

def get_8x8_token():
    """Get OAuth Bearer token for 8x8 Cloud Storage Service API.
    Uses Basic Auth with Base64-encoded key:secret in Authorization header.
    """
    log("Authenticating with 8x8 Cloud Storage Service...")
    url = 'https://api.8x8.com/oauth/v2/token'

    # Encode key:secret as Base64 for Basic Auth header
    credentials = base64.b64encode(
        f"{EIGHT_BY_EIGHT_KEY}:{EIGHT_BY_EIGHT_SECRET}".encode('utf-8')
    ).decode('utf-8')

    body = urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode('utf-8')

    req = urllib.request.Request(
        url, data=body,
        headers={
            'Content-Type':  'application/x-www-form-urlencoded',
            'Authorization': f'Basic {credentials}',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))

    token = data.get('access_token')
    if not token:
        raise Exception('Failed to get 8x8 OAuth token: ' + str(data))
    log(f"8x8 authentication successful — products: {data.get('api_product_list', 'unknown')}")
    return token

def get_8x8_auth_headers(token):
    """Return headers for 8x8 Cloud Storage Service requests."""
    return {
        'Authorization': f'Bearer {token}',
        'Accept':        'application/json',
    }

# ── FETCH CALL RECORDS ────────────────────────────────────────────────────

def fetch_call_records(token):
    """
    Fetch call recordings from 8x8 Cloud Storage Service.
    Response format: {"lastPage":bool,"pageKey":int,"pageSize":int,"content":[...]}
    NOTE: field is "content" not "data"
    """
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_BACK)

    start_ms = int(start.timestamp() * 1000)
    end_ms   = int(now.timestamp() * 1000)

    log(f"Fetching recordings from {start.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}...")

    headers = get_8x8_auth_headers(token)

    def parse_response(data):
        """Extract records from response — field is 'content' per 8x8 docs."""
        if isinstance(data, list):
            return data
        # Official format uses 'content'
        if 'content' in data:
            return data['content']
        # Fallback
        return data.get('data', data.get('items', data.get('recordings', [])))

    # Step 1: Region discovery
    my_regions = []
    for discovery_region in ['uk', 'us-east', 'eu']:
        try:
            resp = http_get(f"https://api.8x8.com/storage/{discovery_region}/v3/regions", headers)
            if isinstance(resp, list):
                my_regions = resp
                log(f"My regions: {my_regions}")
                break
        except Exception as e:
            log(f"Region discovery via {discovery_region}: {e}")

    if not my_regions:
        my_regions = [EIGHT_BY_EIGHT_REGION, 'uk', 'eu']
        log(f"Using default regions: {my_regions}")

    # Step 2: Search each region
    for region in my_regions:
        log(f"Searching region: {region}")

        # First: check what's in the bucket at all (no filter)
        try:
            url  = f"https://api.8x8.com/storage/{region}/v3/objects?limit=5&pageKey=0"
            data = http_get(url, headers)
            recs = parse_response(data)
            page_size = data.get('pageSize', len(recs)) if isinstance(data, dict) else len(recs)
            log(f"  No filter: pageSize={page_size}, records={len(recs)}, lastPage={data.get('lastPage','?') if isinstance(data,dict) else '?'}")
            log(f"  Full response keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
            if recs:
                log(f"  Sample record: {json.dumps(recs[0])[:300]}")
        except Exception as e:
            log(f"  No filter error: {e}")

        # Now try with callrecording filter + date
        try:
            filt = f"type==callrecording;createdTime=ge={start_ms};createdTime=le={end_ms}"
            url  = f"https://api.8x8.com/storage/{region}/v3/objects?filter={urllib.parse.quote(filt)}&limit=100&pageKey=0"
            data = http_get(url, headers)
            recs = parse_response(data)
            log(f"  callrecording in date range: {len(recs)} records")
            if recs:
                return recs
        except Exception as e:
            log(f"  callrecording filter error: {e}")

        # Try callcenterrecording
        try:
            filt = f"type==callcenterrecording;createdTime=ge={start_ms};createdTime=le={end_ms}"
            url  = f"https://api.8x8.com/storage/{region}/v3/objects?filter={urllib.parse.quote(filt)}&limit=100&pageKey=0"
            data = http_get(url, headers)
            recs = parse_response(data)
            log(f"  callcenterrecording in date range: {len(recs)} records")
            if recs:
                return recs
        except Exception as e:
            log(f"  callcenterrecording filter error: {e}")

    log("No recordings found")
    return []


def fetch_recording(token, record):
    """
    Download a recording using the 8x8 Cloud Storage Bulk Download API.
    Steps: POST to start bulk download → poll status → GET zip → extract mp3
    The 'id' field from the listing is the object ID to download.
    """
    import io, zipfile
    region   = EIGHT_BY_EIGHT_REGION
    headers  = get_8x8_auth_headers(token)
    obj_id   = record.get('id', '')
    obj_name = record.get('objectName', '')

    if not obj_id:
        log(f"  No object ID — skipping")
        return None, None

    log(f"  Bulk downloading object {obj_id} ({obj_name[:50]})")

    # Step 1: Start bulk download
    start_url = f"https://api.8x8.com/storage/{region}/v3/bulk/download/start"
    try:
        body = json.dumps([obj_id]).encode('utf-8')
        req  = urllib.request.Request(start_url, data=body, headers={
            **headers,
            'Content-Type': 'application/json',
        }, method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        zip_name = result.get('zipName')
        status   = result.get('status')
        log(f"  Bulk download started: zipName={zip_name}, status={status}")
        if not zip_name:
            log(f"  No zipName returned: {result}")
            return None, None
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')[:200]
        log(f"  Bulk start error {e.code}: {body}")
        return None, None

    # Step 2: Poll for completion
    status_url = f"https://api.8x8.com/storage/{region}/v3/bulk/download/status/{zip_name}"
    for attempt in range(10):
        try:
            req = urllib.request.Request(status_url, headers=headers, method='GET')
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            status = result.get('status')
            log(f"  Status check {attempt+1}: {status}")
            if status == 'DONE':
                break
            elif status in ('ERROR', 'FAILED'):
                log(f"  Bulk download failed: {result}")
                return None, None
            time.sleep(3)
        except Exception as e:
            log(f"  Status check error: {e}")
            time.sleep(3)
    else:
        log(f"  Bulk download timed out")
        return None, None

    # Step 3: Download the zip
    dl_url = f"https://api.8x8.com/storage/{region}/v3/bulk/download/{zip_name}"
    try:
        req = urllib.request.Request(dl_url, headers=headers, method='GET')
        with urllib.request.urlopen(req, timeout=120) as resp:
            zip_bytes = resp.read()
        log(f"  Downloaded zip: {len(zip_bytes)/1024:.0f}KB")
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')[:200]
        log(f"  Zip download error {e.code}: {body}")
        return None, None

    # Step 4: Extract mp3 from zip
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        for name in zf.namelist():
            if name.endswith('.mp3') or name.endswith('.wav') or name.endswith('.m4a'):
                audio = zf.read(name)
                content_type = 'audio/mpeg' if name.endswith('.mp3') else 'audio/wav'
                log(f"  Extracted {name}: {len(audio)/1024:.0f}KB")
                return audio, content_type
        log(f"  No audio file found in zip. Contents: {zf.namelist()}")
        return None, None
    except Exception as e:
        log(f"  Zip extraction error: {e}")
        return None, None


# ── TRANSCRIBE ────────────────────────────────────────────────────────────

def transcribe(audio_bytes, filename, content_type):
    """Send audio to OpenAI Whisper for transcription."""
    boundary = 'QCGboundary'
    # Build multipart form data
    body = (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: {content_type}\r\n\r\n'
    ).encode() + audio_bytes + (
        f'\r\n--{boundary}\r\n'
        f'Content-Disposition: form-data; name="model"\r\n\r\nwhisper-1'
        f'\r\n--{boundary}\r\n'
        f'Content-Disposition: form-data; name="language"\r\n\r\nen'
        f'\r\n--{boundary}\r\n'
        f'Content-Disposition: form-data; name="response_format"\r\n\r\ntext'
        f'\r\n--{boundary}--\r\n'
    ).encode()

    req = urllib.request.Request(
        'https://api.openai.com/v1/audio/transcriptions',
        data=body,
        headers={
            'Authorization': f'Bearer {OPENAI_KEY}',
            'Content-Type':  f'multipart/form-data; boundary={boundary}',
        },
        method='POST'
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read().decode('utf-8').strip()

# ── SCORE ─────────────────────────────────────────────────────────────────

def score_call(transcript):
    """Send transcript to GPT-4o for scoring."""
    flag_list = ','.join([
        'single_close_attempt','no_close_attempt','terminology_error',
        'missed_buying_signal','pain_point_not_leveraged',
        'failed_to_handle_objection','no_permission_to_talk',
        'generic_pitch','renewal_date_not_asked','current_broker_not_asked',
        'rapport_established','strong_questions_asked','effective_close',
        'buying_signal_identified','credibility_established',
        'specialism_established','claims_test_asked','consultative_approach',
    ])
    rep_list = ', '.join(REP_NAMES)

    prompt_lines = [
        'You are an expert sales coach analysing business development calls made by QCG Insurance to care home operators.',
        'QCG is a specialist care sector insurance broker — care-only specialism, in-house claims team, mock CQC inspections, bespoke cover.',
        'Be demanding. Most cold calls sit at 4-6. A 7 requires clear evidence. 8+ is rare. Never inflate.',
        '',
        'Return ONLY valid JSON — no preamble, no markdown:',
        '{',
        '  "detected_rep": "full name of QCG rep as heard",',
        '  "detected_outcome": "meeting_booked|follow_up_agreed|not_interested|no_answer|callback_requested",',
        '  "outcome_confidence": "high or low",',
        '  "overall": 0,',
        '  "dimensions": {',
        '    "opening": {"score":0,"rationale":"specific moment or quote"},',
        '    "objection_handling": {"score":0,"rationale":"specific moment or quote"},',
        '    "questions_asked": {"score":0,"rationale":"specific moment or quote","strong_questions":[],"weak_questions":[],"missed_questions":[]},',
        '    "closing": {"score":0,"rationale":"specific moment or quote"}',
        '  },',
        '  "prospect_intelligence": {',
        '    "current_broker":null,"renewal_date":null,"cqc_rating":"not mentioned",',
        '    "claims_mentioned":false,"claims_detail":null,"beds_mentioned":null,',
        '    "num_homes":null,"premium_increase_mentioned":false,"other_insurances":[],',
        '    "other_businesses":null,"biggest_challenge":null,"staff_retention_mentioned":false,',
        '    "expansion_plans":null,"external_hr_hs":false,"decision_factor":null,',
        '    "current_broker_complaint":null,"renewal_window":"unknown"',
        '  },',
        '  "rep_behaviours": {',
        '    "mentioned_care_specialism":false,"mentioned_claims_team":false,',
        '    "asked_renewal_date":false,"asked_claims_history":false,',
        '    "asked_current_broker":false,"asked_business_challenges":false,',
        '    "asked_staff_retention":false,"asked_expansion_plans":false,',
        '    "asked_other_insurances":false,"mentioned_cqc_support":false,',
        '    "consultative_approach":false,"generic_pitch":false',
        '  },',
        '  "flags": [],',
        '  "verbatim": [{"type":"closing_attempt|weak_close|effective_close|missed_signal|strong_question|weak_question|objection_handled|buying_signal|missed_follow_up|pain_point_surfaced|credibility_error|claims_test_question|specialism_established","quote":"exact words max 35 words"}],',
        '  "narrative": "Two sentences. First: what happened. Second: frank quality assessment.",',
        '  "strength": "One specific strength with direct quote.",',
        '  "focus": "Most important thing to fix with exact moment, quote, and concrete alternative."',
        '}',
        '',
        f'REP DETECTION: Reps are: {rep_list}',
        'SCORING: Opening(8-10:care specialism+permission+compelling reason|5-7:missed one element|3-4:generic|1-2:fumbled)',
        'Objections(8-10:reframed+kept going|5-7:accepted some deflections|3-4:could not navigate|1-2:capitulated)',
        'Questions(8-10:consultative+business challenges+follow-up|5-7:surface level|3-4:closed only|1-2:just pitched)',
        'Closing(8-10:clear ask+handled deflection+concrete outcome|5-7:asked once accepted deflection|3-4:vague|1-2:never asked)',
        f'FLAGS: {flag_list}',
        'VERBATIM: 5-6 moments. Every closing attempt verbatim. Most important missed opportunity. Exact words only.',
        '',
        'TRANSCRIPT:',
        transcript,
    ]
    prompt = '\n'.join(prompt_lines)

    data = http_post(
        'https://api.openai.com/v1/chat/completions',
        {
            'model':       'gpt-4o',
            'messages':    [{'role':'user','content':prompt}],
            'temperature': 0.2,
            'max_tokens':  2500,
        },
        headers={'Authorization': f'Bearer {OPENAI_KEY}'}
    )
    raw = data['choices'][0]['message']['content'].strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'): raw = raw[4:]
    return json.loads(raw.strip())

# ── WRITE TO SHEETS ───────────────────────────────────────────────────────

def write_to_sheets(payload):
    """Write scored call to Google Sheet via Apps Script."""
    body    = json.dumps(payload).encode('utf-8')
    req     = urllib.request.Request(
        SHEETS_URL, data=body,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            return result.get('ok', False)
    except Exception as e:
        log(f"Sheet write warning: {e}")
        return False

# ── MATCH REP ─────────────────────────────────────────────────────────────

def match_rep(detected, user_email=None):
    """Match detected rep name to known reps."""
    # Try email map first
    if user_email and user_email in REP_MAP:
        return REP_MAP[user_email], 'high'
    # Try name match
    if not detected:
        return '', 'low'
    dl = detected.lower().strip()
    for name in REP_NAMES:
        parts = name.lower().split(' ')
        if dl == name.lower() or all(p in dl for p in parts):
            return name, 'high'
        if any(p in dl for p in parts if len(p) > 3):
            return name, 'low'
    return detected, 'low'

# ── MAIN ──────────────────────────────────────────────────────────────────

def main():
    log("QCG Call Intelligence — nightly pipeline starting")

    # Validate config
    missing = []
    if not EIGHT_BY_EIGHT_KEY:    missing.append('EIGHT_BY_EIGHT_KEY')
    if not EIGHT_BY_EIGHT_SECRET: missing.append('EIGHT_BY_EIGHT_SECRET')
    if not OPENAI_KEY:            missing.append('OPENAI_KEY')
    if not SHEETS_URL:            missing.append('SHEETS_URL')
    if missing:
        raise Exception(f"Missing required secrets: {', '.join(missing)}")

    if not EIGHT_BY_EIGHT_PBX_ID:
        log("WARNING: EIGHT_BY_EIGHT_PBX_ID not set — will attempt to discover from API")

    # Authenticate with 8x8
    token = get_8x8_token()

    # Fetch call records
    records = fetch_call_records(token)

    if not records:
        log("No call records found for this period — exiting")
        return

    processed = 0
    skipped   = 0
    errors    = 0

    for record in records:
        # Cloud Storage Service object fields
        # objectName format: ipbx:qualitycareinsura:callrecording:users:EXT:TIMESTAMP-CALLID-EXT-NUMBER_E.mp3
        call_id   = record.get('id', '')
        filename  = record.get('objectName', f'{call_id}.mp3')
        created   = record.get('createdTime', '')
        date_str  = created[:10] if created else datetime.now().strftime('%Y-%m-%d')
        time_str  = created[11:16] if len(created) > 15 else ''

        # Extract metadata from tags
        tags      = {t.get('key'):t.get('value') for t in record.get('tags', [])}
        prospect  = tags.get('address') or tags.get('remotePartyNumber', '')
        user_email= record.get('userId', '') or tags.get('userId', '')

        # Parse extension and prospect from objectName if tags incomplete
        # Format: ipbx:tenant:callrecording:users:EXT:TIMESTAMP-CALLID-EXT-PROSPECT_E.mp3
        obj_name  = filename.replace('ipbx.', 'ipbx:').replace('.callrecording.', ':callrecording:')
        parts     = obj_name.split(':')
        if not prospect and len(parts) >= 6:
            # Last part contains prospect number
            last = parts[-1].replace('_E.mp3','').replace('.mp3','')
            sub  = last.split('-')
            if sub: prospect = sub[-1]

        # Duration from storedBytes estimate or tags
        duration  = int(tags.get('duration', 0)) if tags.get('duration') else 0

        log(f"Processing call {call_id} ({duration}s) from {user_email}")

        try:
            # Fetch recording
            audio, content_type = fetch_recording(token, record)
            if not audio:
                log(f"  No recording available — skipping")
                skipped += 1
                continue

            # Transcribe
            log(f"  Transcribing ({len(audio)/1024:.0f}KB)...")
            transcript = transcribe(audio, filename, content_type or 'audio/mpeg')
            if not transcript or len(transcript) < 20:
                log(f"  Transcript too short — skipping")
                skipped += 1
                continue

            # Score
            log(f"  Scoring with GPT-4o...")
            result = score_call(transcript)

            # Match rep
            rep, rep_confidence = match_rep(result.get('detected_rep',''), user_email)
            outcome     = result.get('detected_outcome', '')
            out_conf    = result.get('outcome_confidence', 'low')

            # Build payload
            d  = result.get('dimensions', {})
            pi = result.get('prospect_intelligence', {})
            rb = result.get('rep_behaviours', {})
            q  = d.get('questions_asked', {})

            payload = {
                'call_id':    f'auto_{call_id}_{date_str}',
                'rep':        rep,
                'rep_id':     rep[:2].upper() if rep else '',
                'manager':    'Josh',
                'date':       date_str,
                'time':       time_str,
                'duration':   duration,
                'filename':   filename,
                'prospect':   prospect,
                'outcome':    outcome,
                'overall':    result.get('overall', ''),
                'dimensions': d,
                'flags':      result.get('flags', []),
                'verbatim':   result.get('verbatim', []),
                'narrative':  result.get('narrative', ''),
                'strength':   result.get('strength', ''),
                'focus':      result.get('focus', ''),
                'transcript': transcript,
                'prospect_intelligence': pi,
                'rep_behaviours':        rb,
            }

            # Write to Sheet
            log(f"  Writing to Sheet (rep={rep}, outcome={outcome})...")
            ok = write_to_sheets(payload)
            if ok:
                log(f"  ✓ Saved — {rep} | {outcome} | overall={result.get('overall')}")
                processed += 1
            else:
                log(f"  Sheet write returned not-ok")
                errors += 1

            # Rate limiting — be gentle with APIs
            time.sleep(2)

        except Exception as e:
            log(f"  ERROR processing {call_id}: {e}")
            errors += 1
            continue

    log(f"\nPipeline complete — processed:{processed} skipped:{skipped} errors:{errors}")

if __name__ == '__main__':
    main()
