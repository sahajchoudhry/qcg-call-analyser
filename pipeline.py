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

def get_8x8_token():
    """
    Build auth token for 8x8 Call Recordings and Storage API.
    This API uses Base64-encoded key:secret as a Bearer token.
    """
    log("Building 8x8 auth token...")
    combined = f"{EIGHT_BY_EIGHT_KEY}:{EIGHT_BY_EIGHT_SECRET}"
    token    = base64.b64encode(combined.encode('utf-8')).decode('utf-8')
    log("8x8 auth token built")
    return token

def get_8x8_auth_headers(token):
    """Return headers for 8x8 Call Recordings API requests."""
    return {
        'Authorization': f'Basic {token}',
        'Accept':        'application/json',
    }

# ── FETCH CALL RECORDS ────────────────────────────────────────────────────

def fetch_call_records(token):
    """Fetch call recording files from 8x8 Call Recordings API."""
    now   = datetime.now(timezone.utc)
    start = now - timedelta(days=DAYS_BACK)

    # Format dates for the recordings API
    start_str = start.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str   = now.strftime('%Y-%m-%dT%H:%M:%SZ')

    log(f"Fetching recordings from {start_str} to {end_str}...")

    # 8x8 Call Recordings and Storage API endpoint
    url = (
        f"https://storage.8x8.com/api/call-recordings"
        f"?startDate={urllib.parse.quote(start_str)}"
        f"&endDate={urllib.parse.quote(end_str)}"
    )
    if EIGHT_BY_EIGHT_PBX_ID:
        url += f"&pbxId={EIGHT_BY_EIGHT_PBX_ID}"

    headers = get_8x8_auth_headers(token)
    try:
        data    = http_get(url, headers)
        records = data if isinstance(data, list) else data.get('recordings', data.get('data', []))
        log(f"Found {len(records)} recordings")
        return records
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8')
        log(f"Recordings API error {e.code}: {body}")
        # Try alternate endpoint format
        url2 = f"https://api.8x8.com/storage/v1/call-recordings?startDate={urllib.parse.quote(start_str)}&endDate={urllib.parse.quote(end_str)}"
        try:
            data    = http_get(url2, headers)
            records = data if isinstance(data, list) else data.get('recordings', data.get('data', []))
            log(f"Found {len(records)} recordings (alternate endpoint)")
            return records
        except Exception as e2:
            log(f"Both endpoints failed: {e2}")
            return []

# ── FETCH RECORDING FILE ──────────────────────────────────────────────────

def fetch_recording(token, call_id):
    """Download recording audio for a specific call."""
    url = (
        f"https://storage.8x8.com/api/call-recordings/{call_id}/download"
    )
    headers = get_8x8_auth_headers(token)
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read(), resp.headers.get('Content-Type', 'audio/mpeg')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None  # No recording for this call
        raise

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
        call_id   = record.get('callId') or record.get('id', '')
        duration  = record.get('duration', 0)
        user_email= record.get('userId') or record.get('agentEmail', '')
        filename  = record.get('recordingFileName', f'{call_id}.mp3')
        date_str  = record.get('startTime', '')[:10] or datetime.now().strftime('%Y-%m-%d')
        time_str  = record.get('startTime', '')[11:16] or ''
        prospect  = record.get('remoteNumber') or record.get('calledNumber', '')

        log(f"Processing call {call_id} ({duration}s) from {user_email}")

        try:
            # Fetch recording
            audio, content_type = fetch_recording(token, call_id)
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
