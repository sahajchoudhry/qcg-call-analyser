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
from zoneinfo import ZoneInfo

LONDON_TZ = ZoneInfo('Europe/London')

def is_call_drive_window(created_iso):
    """
    Tuesday 09:00-12:30 UK local time is the team's cold-call drive block —
    pure volume dialling, not consultative BD calls. Excluded from coaching
    scoring entirely (different goal for the call = not comparable data),
    but still counted toward daily call-volume KPI tracking upstream.
    """
    if not created_iso:
        return False
    try:
        # 8x8 createdTime is ISO 8601 UTC, e.g. 2026-06-21T13:43:13Z
        dt_utc = datetime.fromisoformat(created_iso.replace('Z', '+00:00'))
        dt_ldn = dt_utc.astimezone(LONDON_TZ)
    except Exception:
        return False
    if dt_ldn.weekday() != 1:  # Monday=0, Tuesday=1
        return False
    minutes_since_midnight = dt_ldn.hour * 60 + dt_ldn.minute
    return (9 * 60) <= minutes_since_midnight <= (12 * 60 + 30)

# ── CONFIG ────────────────────────────────────────────────────────────────
# These are set as GitHub Secrets — never hardcode them here

EIGHT_BY_EIGHT_KEY    = os.environ.get('EIGHT_BY_EIGHT_KEY', '')
EIGHT_BY_EIGHT_SECRET = os.environ.get('EIGHT_BY_EIGHT_SECRET', '')
EIGHT_BY_EIGHT_PBX_ID = os.environ.get('EIGHT_BY_EIGHT_PBX_ID', '')  # fill in when known
OPENAI_KEY            = os.environ.get('OPENAI_KEY', '')
SHEETS_URL            = os.environ.get('SHEETS_URL', '')  # Apps Script /exec URL

# Reps — maps phone extension or user to rep name
# Add your reps' 8x8 email/extension here once you know them
# Map extension numbers to rep names — filter BEFORE transcription
REP_EXTENSIONS = {
    '235': 'Lucy Sandle',
    '242': 'Ryan Davenport',
    '243': 'Sara Bosworth',
    '212': 'Steve Taylor',
    '246': 'Cameron Montrose',
}

REP_MAP = {
    # 8x8 userIds — fill in from pipeline logs once confirmed
    # 'WDMDY36gQEycqkgxSGNPjA': 'Steve Taylor',
}

REP_NAMES = ['Steve Taylor','Lucy Sandle','Ryan Davenport','Cameron Montrose','Sara Bosworth']

# Name variants for transcript detection
REP_NAME_VARIANTS = {
    'stephen taylor':   'Steve Taylor',
    'steve taylor':     'Steve Taylor',
    'stephen':          'Steve Taylor',
    'lucy sandle':      'Lucy Sandle',
    'lucy':             'Lucy Sandle',
    'ryan davenport':   'Ryan Davenport',
    'ryan':             'Ryan Davenport',
    'cameron montrose': 'Cameron Montrose',
    'cameron':          'Cameron Montrose',
    'sara bosworth':    'Sara Bosworth',
    'sara':             'Sara Bosworth',
    'sarah bosworth':   'Sara Bosworth',
    'sarah':            'Sara Bosworth',
}

# How many days back to pull recordings
DAYS_BACK = 1

# ── UTILITIES ─────────────────────────────────────────────────────────────

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {}, method='GET')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))

def http_post(url, data, headers=None, max_retries=4):
    """POST with JSON body. Retries on 429 with backoff — but distinguishes
    OpenAI's two different 429 causes: rate-limit-exceeded (transient, retry
    helps) vs insufficient_quota (billing/credit exhausted, retry NEVER
    helps and will just waste the retry budget while masking the real
    problem). Surfaces the actual response body on failure instead of just
    the bare urllib error text, since that body is the only place the two
    causes are distinguishable.
    """
    body = json.dumps(data).encode('utf-8')
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers or {}, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            err_body_raw = e.read().decode('utf-8', errors='replace')
            try:
                err_json = json.loads(err_body_raw)
            except Exception:
                err_json = {}
            err_type = (err_json.get('error') or {}).get('type', '')
            err_code = (err_json.get('error') or {}).get('code', '')
            err_msg  = (err_json.get('error') or {}).get('message', '')

            if e.code == 429 and (err_type == 'insufficient_quota' or err_code == 'insufficient_quota'):
                raise Exception(
                    f"OpenAI quota/billing exhausted (insufficient_quota) — "
                    f"this is NOT a rate limit and retrying will not help. "
                    f"Check billing at platform.openai.com. Detail: {err_msg}"
                )
            if e.code == 429 and attempt < max_retries:
                wait_s = min(3 * (2 ** (attempt - 1)), 30)
                log(f"    [diag] HTTP 429 (attempt {attempt}/{max_retries}, type={err_type or 'unknown'}) — waiting {wait_s}s before retry")
                time.sleep(wait_s)
                continue
            raise Exception(f"HTTP {e.code}: {e.reason} — {err_msg or err_body_raw[:200]}")
    raise Exception(f"HTTP request failed after {max_retries} attempts")

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

        # Paginate through ALL callrecording objects in date range
        try:
            filt      = f"type==callrecording;createdTime=ge={start_ms};createdTime=le={end_ms}"
            all_recs  = []
            page_key  = 0
            page_num  = 0
            while True:
                url  = (f"https://api.8x8.com/storage/{region}/v3/objects"
                        f"?filter={urllib.parse.quote(filt)}"
                        f"&limit=100&pageKey={page_key}"
                        f"&sortField=createdTime&sortDirection=DESC")
                data = http_get(url, headers)
                recs = parse_response(data)
                all_recs.extend(recs)
                page_num += 1
                last_page = data.get('lastPage', True) if isinstance(data, dict) else True
                next_key  = data.get('pageKey', page_key) if isinstance(data, dict) else page_key
                log(f"  Page {page_num}: {len(recs)} records (total so far: {len(all_recs)}, lastPage={last_page})")
                if last_page or next_key == page_key or not recs:
                    break
                page_key = next_key
            log(f"  Total callrecordings found: {len(all_recs)}")
            if all_recs:
                return all_recs
        except Exception as e:
            log(f"  callrecording pagination error: {e}")

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
    Download a recording from 8x8 Cloud Storage.
    Each recording has its own bucketId — try multiple download approaches.
    """
    import io, zipfile
    headers  = get_8x8_auth_headers(token)
    obj_id   = record.get('id', '')
    obj_name = record.get('objectName', '')
    bucket_id = record.get('bucketId', '')

    if not obj_id:
        return None, None

    log(f"  Downloading {obj_id[:20]}... bucket={bucket_id[:8]}...")

    # Try bulk download in both regions since each recording has its own bucket
    for region in ['uk', 'eu', 'us-east', 'us-west']:
        start_url = f"https://api.8x8.com/storage/{region}/v3/bulk/download/start"
        try:
            body = json.dumps([obj_id]).encode('utf-8')
            req  = urllib.request.Request(start_url, data=body, headers={
                **headers, 'Content-Type': 'application/json',
            }, method='POST')
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
            zip_name = result.get('zipName')
            status   = result.get('status')
            if not zip_name:
                continue
            log(f"  Region {region}: started {zip_name[:8]}... status={status}")

            # Poll status
            status_url = f"https://api.8x8.com/storage/{region}/v3/bulk/download/status/{zip_name}"
            final_status = None
            for attempt in range(8):
                req2 = urllib.request.Request(status_url, headers=headers, method='GET')
                with urllib.request.urlopen(req2, timeout=10) as resp2:
                    res2 = json.loads(resp2.read().decode('utf-8'))
                final_status = res2.get('status')
                if final_status == 'DONE':
                    break
                elif final_status in ('FAILED', 'ERROR'):
                    break
                time.sleep(2)

            log(f"  Region {region}: final status={final_status}")

            if final_status != 'DONE':
                continue

            # Download zip
            dl_url = f"https://api.8x8.com/storage/{region}/v3/bulk/download/{zip_name}"
            req3 = urllib.request.Request(dl_url, headers=headers, method='GET')
            with urllib.request.urlopen(req3, timeout=120) as resp3:
                zip_bytes = resp3.read()
            log(f"  Downloaded {len(zip_bytes)/1024:.0f}KB from region {region}")

            # Extract audio
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
            for name in zf.namelist():
                if any(name.endswith(ext) for ext in ['.mp3','.wav','.m4a','.ogg']):
                    audio = zf.read(name)
                    ct    = 'audio/mpeg' if name.endswith('.mp3') else 'audio/wav'
                    log(f"  Extracted: {name} ({len(audio)/1024:.0f}KB)")
                    return audio, ct
            log(f"  Zip contents: {zf.namelist()}")
            return None, None

        except urllib.error.HTTPError as e:
            log(f"  Region {region} HTTP {e.code}")
        except Exception as e:
            log(f"  Region {region} error: {str(e)[:60]}")

    log(f"  All regions failed for {obj_id[:20]}")
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

    max_retries = 4
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            'https://api.openai.com/v1/audio/transcriptions',
            data=body,
            headers={
                'Authorization': f'Bearer {OPENAI_KEY}',
                'Content-Type':  f'multipart/form-data; boundary={boundary}',
            },
            method='POST'
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode('utf-8').strip()
        except urllib.error.HTTPError as e:
            err_body_raw = e.read().decode('utf-8', errors='replace')
            try:
                err_json = json.loads(err_body_raw)
            except Exception:
                err_json = {}
            err_type = (err_json.get('error') or {}).get('type', '')
            err_code = (err_json.get('error') or {}).get('code', '')
            err_msg  = (err_json.get('error') or {}).get('message', '')

            if e.code == 429 and (err_type == 'insufficient_quota' or err_code == 'insufficient_quota'):
                raise Exception(
                    f"OpenAI quota/billing exhausted (insufficient_quota) — "
                    f"this is NOT a rate limit and retrying will not help. "
                    f"Check billing at platform.openai.com. Detail: {err_msg}"
                )
            if e.code == 429 and attempt < max_retries:
                wait_s = min(3 * (2 ** (attempt - 1)), 30)
                log(f"    [diag] Whisper HTTP 429 (attempt {attempt}/{max_retries}, type={err_type or 'unknown'}) — waiting {wait_s}s before retry")
                time.sleep(wait_s)
                continue
            raise Exception(f"HTTP {e.code}: {e.reason} — {err_msg or err_body_raw[:200]}")
    raise Exception(f"Transcription failed after {max_retries} attempts")

# ── SCORE ─────────────────────────────────────────────────────────────────

SECTOR_KNOWLEDGE = """
SECTOR CONTEXT (UK care sector — use this to judge whether the rep is actually
sector-fluent, or just running a generic broker script):

PROVIDER TYPES AND WHAT THEY IMPLY: Residential care homes (personal care, no
nursing — 10-30 beds often independent, 40-80+ bed homes often group-run).
Nursing homes (registered nurses on site, higher acuity, higher clinical
risk). Domiciliary/home care (carers visiting people at home — lone working,
driving between calls, NO premises exposure — a fundamentally different risk
conversation than a care home). Specialist care (dementia, LD, mental health,
autism — higher-risk, higher-value). Supported living (tenancy + separate
care — regulatory/liability lines blur here, good discovery territory).
Children's residential (separate CQC/Ofsted regulation).

OWNERSHIP STRUCTURE — this changes what "a good call" looks like, judge
accordingly: Independents (1-3 homes, owner-operator is usually the decision
maker, short relationship-driven sales cycle, price-sensitive but personal).
Small-to-mid groups (4-20 homes, often PE-backed, procurement sits with an
Ops Director or Group FD not the home manager, longer RFP-style cycle — a
rep spending one call qualifying rather than closing here is doing it right,
not failing to close). Large corporates (HC-One, Barchester, Care UK etc,
usually tied to national broker panels — rarely worth prospecting directly
unless there's a specific in; do not penalise a rep for a short call here).
Not-for-profit/charity providers (governance-heavy, trustee sign-off, slower
by nature, not a rep failure).

Buying committee — five roles, each with a different lens:
Registered Manager (CQC compliance, staffing, day-to-day risk, keeping their
registration clean — usually the best influence/referral node even when not
the final signer, since they see incidents first and are personally
accountable to CQC).
Owner/Director at independents (total cost, claims history, personal
liability, exit value of the business).
Group Operations Director (consistency across homes, risk standardisation,
CQC ratings portfolio-wide).
Finance Director/CFO at groups (premium spend, budget cycle timing, renewal
terms).
HR/People Director at groups (Employers' Liability claims, staff injury
trends, agency staff exposure — rarely the first contact but influences the
EL renewal; a rep who reaches this person and pivots to EL-specific
questions is showing real sector awareness).
A rep who never works out which of these they're speaking to is a red flag.

Regulatory drivers that create real urgency: CQC rating (Outstanding / Good /
Requires Improvement / Inadequate) and date of last inspection — a rating
change in either direction is a natural renewal-conversation trigger (a
downgrade creates urgency/fear, an upgrade is a moment to revisit terms and
use the improved rating as leverage in underwriting). CQC's single
assessment framework (rolled out 2023) replaced periodic inspections with an
ongoing evidence-based scoring model, raising compliance anxiety generally.
Notifiable events, safeguarding referral volume, and RIDDOR incidents
(reportable workplace injuries, relevant to EL) are genuine claims-risk
indicators, not small talk. DoLS/Liberty Protection Safeguards are relevant
in LD/dementia settings. Care records are special-category data under
ICO/data protection — a live, underused cyber-liability angle.

Insurance lines relevant to this sector: Employers' Liability (compulsory —
manual handling/back injuries from resident transfers is the common claim),
Public/Products Liability (falls, food safety), Medical Malpractice / Care
Liability (the sector-specific big one — negligence claims from pressure
sores, medication errors, missed care plan actions — this is QCG's
specialist differentiator vs generalist brokers, usually the largest and
most technical line), Management Liability/D&O (personal accountability of
registered managers and directors under CQC), Cyber (care records are
special-category data, ransomware against small providers is rising, still
under-bought — good cross-sell), Property/Buildings & Contents (fire risk is
elevated — older buildings, oxygen use, kitchens — often underinsured on
rebuild cost), Business Interruption (homes can't just "close" —
displacement of residents is a real, expensive scenario), Motor/Fleet
(mainly relevant to domiciliary care — named driver vs any driver, business
use classification), Legal Expenses/HR support (often bundled, relevant
given high staff turnover and tribunal risk).

Genuine buying signals to listen for: reliance on agency staff (insurers
price this as elevated risk, and it drives up EL claims frequency), National
Living Wage pressure squeezing margins (especially on LA-funded vs
self-funder beds), the gap between LA fee rates and actual cost of care
(providers feel undervalued, so anything reducing overhead lands well), a
bad recent renewal or slow claims experience with the incumbent (hardening
market, fewer specialist insurers — this is where QCG's claims-handling
positioning is the wedge), M&A/ownership change (new owners review all
supplier relationships — a real trigger event), a new home opening
(clean-slate decision, no incumbent inertia), a CQC rating change in either
direction.

Sector-specific objections and the doctrine-correct reframe for each:
"we're tied to a group panel" (local manager may have zero authority —
qualify this early, don't waste the call). "never claimed in years"
(untested territory — reframe around claims handling speed/quality, not
just price). "insurance is a headache" (opening for a "we handle the admin,
you keep running the home" pitch, not a price pitch). "CQC rating is
Requires Improvement, we're focused on fixing that, not insurance" (this is
actually a STRONG opening if reframed as "a broker who understands care can
help protect you while you fix it" — a rep who treats this as bad timing and
backs off has missed the read, not respected it). "we're too small for a
specialist broker" (specialist care brokers are often cheaper due to panel
access, not more expensive — worth countering directly with a proof point,
not just reassurance).

Sector terminology the model should recognise (for judging terminology_error
and rep sector-fluency): Registered Manager (legally responsible individual
at a location, registered with CQC). Service user (sector term for a
resident/client). Notifiable event (legally reportable incident to CQC).
KLOEs (CQC's old "Key Lines of Enquiry" — Safe/Effective/Caring/
Responsive/Well-led — now superseded by the single assessment framework's
quality statements but still used colloquially). DoLS/LPS (Deprivation of
Liberty Safeguards / Liberty Protection Safeguards). PIR (Provider
Information Return, a CQC self-assessment document). Fee rate/LA rate (what
a local authority pays per resident per week, often below true cost of
care). Voids (unoccupied beds — a financial pressure signal). Dependency
level/acuity (how much care a resident needs — drives staffing and claims
risk).
"""

def score_call(transcript):
    """Send transcript to GPT-4o-mini for scoring against QCG's BD doctrine."""
    rep_list = ', '.join(REP_NAMES)

    prompt_lines = [
        'You are a senior sales coach at Quality Care Group (QCG), a specialist UK care-sector insurance broker, reviewing a call made by one of the Business Development Executives (BDEs) on the team.',
        '',
        'CRITICAL FRAME — read before scoring anything:',
        'This is NOT a generic cold call. QCG runs a specific consultative doctrine (below) and the BDE is judged against THAT doctrine, not generic sales technique. A call can be technically smooth and still be a poor QCG call if it skipped qualifying, never created any tension with the incumbent broker, or let the prospect control the frame.',
        'The BDE is NOT expected to go deep on technical insurance detail — coverage specifics, policy wording, underwriting detail — that is the New Business (NB) Executive\'s job at the appointment stage, not the BDE\'s job on this call. Do not penalise a BDE for staying at a sector-fluency level rather than a technical-underwriting level. DO penalise generic broker language with zero sector specificity.',
        '',
        'QCG BD DOCTRINE (the actual rubric this call is judged against):',
        '1. QUALIFYING A LEAD — three things must be established before this is a good lead, in order of importance: (a) PRICE — what they paid last year / what they have received this year, (b) RISK PROFILE — CQC rating, claims history, service users/acuity, (c) CLIENT MENTALITY — are they genuinely engaging, or cagey/benchmarking us for a quote to leverage their current broker? A rep who never establishes at least two of these has not qualified the lead, regardless of how the call ends.',
        '2. "WE DON\'T JUST WANT TO QUOTE, WE WANT TO BE YOUR BROKER" — QCG\'s doctrine is explicitly to NOT compete on beating a price by a small margin. The correct move is to create a problem: "how does your broker support your business beyond the insurance itself?" Reps who position purely as "we might be cheaper" are doing it wrong even if they get an outcome. Reps who create genuine dissatisfaction/jeopardy with the incumbent broker (claims handling speed, lack of proactive support, no CQC help, no risk review) are doing it right — this is REWARDED behaviour, not pushy behaviour.',
        '3. IDENTIFYING THE REAL DECISION DRIVER — different buyers care about different things. A price-led approach on a buyer who actually cares about claims-handling speed or CQC support is a mismatch and should be flagged even if the call "went fine." The rep should be judged on whether they correctly identified what THIS buyer cares about most, not on whether they ran through a fixed script.',
        '4. RAPPORT AND CONSULTATIVE DISCOVERY — the QCG approach explicitly avoids "customer service voice" and generic pitching. Good reps get the prospect talking, ask about their business broadly (not just insurance), and use silence/curiosity rather than over-explaining. This matters as much as the close.',
        '5. SPECIFIC COMMITMENTS ONLY — a follow-up is only a real result if it has a specific day/time attached. "I\'ll call you sometime next month" or "in the near future" is a soft failure to close, not a result, even if the prospect sounded receptive. Always extract the literal commitment made and judge its specificity.',
        '',
        SECTOR_KNOWLEDGE,
        '',
        'SCORING PHILOSOPHY: score from the evidence in the transcript, not from an assumed distribution. Do not deflate scores to hit a target average. Give genuine partial credit — a rep who accomplishes 2 of 3 required elements should land mid-range, not be scored as if they accomplished nothing. A genuinely strong, doctrine-aligned call deserves an 8-9. A call with real, specific failures deserves a 2-3. Most calls will land in between — let the transcript decide, not a preset curve.',
        '',
        'SCORE-BAND ANCHORS — use these explicitly for every dimension. Partial credit is real: hitting most but not all elements of an 8-10 band should land in 5-7, not drop to 1-2.',
        '',
        'OPENING: 8-10 = introduced self and QCG as a care-sector specialist (not a generic broker), checked the prospect had time to talk, gave a specific reason for the call tied to the prospect\'s actual situation (renewal date, CQC event, etc). 5-7 = did most of this but missed one element, e.g. specialism mentioned but no permission check, or vice versa. 3-4 = generic broker-sounding intro, no sector specificity, but at least stated a reason for calling. 1-2 = confused/fumbled intro, wrong name, no clear reason given, or prospect visibly unsure who was calling or why.',
        '',
        'QUALIFYING: 8-10 = established genuine depth on at least 2 of price/risk profile/client mentality, with follow-up probing not just a single surface question. 5-7 = established at least 2 of the 3 elements at a basic level (e.g. got a premium figure AND a CQC rating, even without deep probing) — this is a solid, creditable outcome, not a failure. 3-4 = established exactly 1 element with some substance. 1-2 = established nothing, or only asked a token question that went nowhere (e.g. asked "any claims?" and accepted a one-word no).',
        '',
        'OBJECTION HANDLING: 8-10 = acknowledged the objection, reframed using a specific QCG proof point or sector-specific angle, and kept the conversation going afterward. 5-7 = acknowledged and made one genuine attempt to reframe, even if it didn\'t fully land. 3-4 = acknowledged but gave a generic or weak response with no real reframe attempt. 1-2 = ignored the objection entirely or capitulated immediately with no pushback at all.',
        '',
        'QUESTIONS ASKED: 8-10 = multiple genuinely consultative questions about the business (not just insurance), with real follow-up when the prospect opened up. 5-7 = asked at least 2-3 relevant questions and got real information back, even if follow-up was thin. 3-4 = asked only closed or surface-level questions (e.g. yes/no), minimal information gathered. 1-2 = asked essentially nothing, just pitched or gathered contact details.',
        '',
        'CLOSING: 8-10 = asked explicitly for a specific next step, handled any deflection by trying again differently, and secured a dated, concrete outcome. 5-7 = asked for a next step once and got a real (even if soft) commitment to continue the conversation. 3-4 = vague or passive close ("I\'ll send an email"), no explicit ask made. 1-2 = no attempt to close or move the conversation forward at all, call just ended.',
        '',
        'For EVERY dimension score, you must also state what SPECIFICALLY would move the score up one full band — not a generic "ask better questions" but the exact thing that was missing, anchored to the moment in the call where it should have happened.',
        '',
        'OVERALL SCORE: this must be the average of the five dimension scores (opening, qualifying, objection_handling, questions_asked, closing), rounded to the nearest whole number. Do not compute it holistically or independently — it should be arithmetically consistent with the five dimension scores you give. If you find yourself wanting to give an overall score that doesn\'t match the average, that\'s a signal one of your dimension scores is wrong, go back and fix the dimension score rather than making overall diverge.',
        '',
        'Return ONLY valid JSON — no preamble, no markdown:',
        '{',
        '  "detected_rep": "full name of QCG rep as heard",',
        '  "detected_outcome": "meeting_booked|follow_up_agreed|not_interested|no_answer|callback_requested",',
        '  "outcome_confidence": "high or low",',
        '  "call_type": "decision_maker|gatekeeper|no_meaningful_conversation|logistics_admin",',
        '  "overall": 0,',
        '  "dimensions": {',
        '    "opening": {"score":0,"rationale":"specific moment or quote","what_would_raise_it":"the exact thing that would move this up a band, tied to a moment in the call"},',
        '    "qualifying": {"score":0,"rationale":"did the rep establish price/risk/mentality per QCG doctrine — quote what was and was not established","what_would_raise_it":"..."},',
        '    "objection_handling": {"score":0,"rationale":"specific moment or quote","what_would_raise_it":"..."},',
        '    "questions_asked": {"score":0,"rationale":"specific moment or quote","what_would_raise_it":"...","strong_questions":["verbatim"],"weak_questions":["verbatim"],"missed_questions":[{"moment":"what prospect said","suggested":"what rep should have asked instead"}]},',
        '    "closing": {"score":0,"rationale":"specific moment or quote","what_would_raise_it":"..."}',
        '  },',
        '  "qualifying_assessment": {',
        '    "price_understood": "what the rep learned about current premium/spend, verbatim if possible, or null if never asked",',
        '    "risk_profile_understood": "what the rep learned about CQC rating, claims history, staffing/agency %, or null if never asked",',
        '    "client_mentality_read": "engaged | cagey_or_benchmarking | unclear — with the evidence that shows it",',
        '    "decision_driver": "price | risk_and_compliance | relationship_and_service | unclear",',
        '    "decision_driver_evidence": "the quote that reveals what this buyer actually cares about most",',
        '    "rep_approach_matched_driver": true/false,',
        '    "mismatch_note": "if false, specifically what the rep focused on instead and why that missed the mark, or null"',
        '  },',
        '  "broker_conflict": {',
        '    "attempted": true/false,',
        '    "evidence": "verbatim quote where the rep probed the incumbent broker relationship, or null",',
        '    "missed_opportunity": "if not attempted or weak, the specific moment and exact alternative question the rep should have asked to create genuine jeopardy with the incumbent, or null"',
        '  },',
        '  "rapport": {',
        '    "signals_built": [{"quote":"verbatim","what_worked":"why this built genuine rapport, not just politeness"}],',
        '    "missed_opportunities": [{"moment":"what the prospect said or revealed, verbatim","rep_response":"what the rep actually said or did, verbatim, or none","better_response":"a specific, concrete alternative line the rep could have used instead"}]',
        '  },',
        '  "follow_up": {',
        '    "specific": true/false,',
        '    "commitment_quote": "the exact verbatim commitment made by either party",',
        '    "note": "if not specific, state plainly that this is a soft/unmeasurable commitment and should not be treated as a real result"',
        '  },',
        '  "missed_opportunities": [{"quote":"exact prospect or rep words, max 40 words","category":"buying_signal|pain_point|rapport|qualifying|broker_conflict|follow_up|objection","suggested_response":"a specific, concrete alternative the rep could have said"}],',
        '  "prospect_intelligence": {',
        '    "current_broker":null,"renewal_date":null,"cqc_rating":"not mentioned",',
        '    "claims_mentioned":false,"claims_detail":null,"beds_mentioned":null,',
        '    "num_homes":null,"premium_increase_mentioned":false,"other_insurances":[],',
        '    "other_businesses":null,"biggest_challenge":null,"staff_retention_mentioned":false,',
        '    "expansion_plans":null,"external_hr_hs":false,"decision_factor":null,',
        '    "current_broker_complaint":null,"renewal_window":"unknown","ownership_structure":"independent|small_mid_group|large_corporate|not_for_profit|unclear"',
        '  },',
        '  "rep_behaviours": {',
        '    "mentioned_care_specialism":false,"mentioned_claims_team":false,',
        '    "asked_renewal_date":false,"asked_claims_history":false,',
        '    "asked_current_broker":false,"asked_business_challenges":false,',
        '    "asked_staff_retention":false,"asked_expansion_plans":false,',
        '    "asked_other_insurances":false,"mentioned_cqc_support":false,',
        '    "identified_decision_maker_correctly":false,"consultative_approach":false,"generic_pitch":false',
        '  },',
        '  "flags": [],',
        '  "verbatim": [{"type":"closing_attempt|weak_close|effective_close|missed_signal|strong_question|weak_question|objection_handled|buying_signal|missed_follow_up|pain_point_surfaced|credibility_error|claims_test_question|specialism_established|broker_conflict_created|vague_follow_up","quote":"exact words max 40 words"}],',
        '  "narrative": "Four to six sentences minimum. Cover: what actually happened on the call, whether the rep followed QCG doctrine (qualifying, broker-jeopardy, decision-driver matching) or just ran a generic pitch, the single biggest missed opportunity with a quote, and a frank assessment of whether this was a good use of the prospect\'s and rep\'s time.",',
        '  "strength": "One specific strength with direct quote and why it worked per QCG doctrine specifically, not generically.",',
        '  "focus": "The single most important thing to fix. Name the exact moment, quote what was said, give a concrete alternative line, and state what score improvement this would realistically produce."',
        '}',
        '',
        f'REP DETECTION: Reps are: {rep_list}',
        'CALL TYPE DETECTION — classify first, this determines how the call is scored:',
        'decision_maker = rep spoke directly with someone who has authority over insurance decisions (care home manager, owner, director, finance manager), in a genuine prospecting/qualifying conversation. Even if the call was short or ended badly.',
        'gatekeeper = rep spoke with reception, admin, PA, or anyone who cannot make insurance decisions. Includes calls where rep was asked to call back without reaching the DM.',
        'no_meaningful_conversation = voicemail, automated message, no answer, or under 20 seconds of actual conversation.',
        'logistics_admin = the ENTIRE call is pure administrative housekeeping with no qualifying opportunity, regardless of who is on the line. This covers: rescheduling/confirming an already-arranged meeting; the DM saying they are not available right now and asking for a callback at a specific later time with zero substantive discussion in between (e.g. "I am not in the office yet, call me back at 3"); a purely factual lookup with no decision-making content (e.g. "which of two renewal dates is correct?" answered by checking a certificate on a wall); sending/confirming a form or document with no discovery attached. The test is not "was a meeting involved" or "did they sound busy" — it is whether the call, as it actually happened, contained ANY point where a qualifying question could realistically have been asked and was not, versus a call that structurally never had that opportunity at all. If the entire transcript is scheduling logistics or a single factual lookup with no room for discovery, use this type even for a decision-maker. Do NOT use this just because a call was short or ended in a soft outcome — a short call where the rep chose not to probe is still decision_maker and should be scored (and coached) accordingly; this type is only for calls where there was structurally nothing to probe.',
        'When call_type is gatekeeper, no_meaningful_conversation, or logistics_admin: set all dimension scores to 0, qualifying_assessment fields to null/unclear, broker_conflict.attempted to false, rapport to empty arrays, missed_opportunities to empty array, and flags to empty array. Only extract prospect_intelligence (renewal date, contact name etc) and outcome — there is nothing to coach on a call that never reached a decision-maker, or that was never a prospecting call in the first place.',
        '',
        'OUTCOME DETECTION — be very precise:',
        'meeting_booked = a specific day AND time was agreed for a follow-up call or meeting. Must be explicit from both parties. E.g. "Monday at 2pm", "Thursday morning", "next Tuesday at 10". If this happened, use meeting_booked even if the call was otherwise weak.',
        'follow_up_agreed = rep will call back or send info but NO specific date/time was confirmed. E.g. "call me next week", "send some info over", "try me in a few weeks". This is a SOFT outcome — see follow_up.specific below, this should almost always be false for this outcome type.',
        'not_interested = prospect clearly declined. E.g. "happy with our current broker", "not interested", "already renewed".',
        'callback_requested = prospect asked rep to call back at a specific time they named.',
        'no_answer = nobody answered, voicemail only, or under 30 seconds with no real conversation.',
        'When choosing between meeting_booked and follow_up_agreed: if a specific time was named and agreed by both, it is meeting_booked. If vague ("next week", "soon", "sometime"), it is follow_up_agreed and follow_up.specific must be false.',
        '',
        'FLAG DEFINITIONS — apply every flag whose trigger condition is genuinely met by the transcript. Do not apply a flag just because its name sounds relevant; check the actual condition.',
        'Negative flags:',
        '  single_close_attempt — rep asked for the meeting/next step exactly once and did not try again after a deflection.',
        '  no_close_attempt — rep never explicitly asked for a next step at all.',
        '  terminology_error — rep used incorrect or generic insurance terminology that a sector specialist would not use.',
        '  missed_buying_signal — prospect said something indicating readiness or interest and the rep did not pick up on it or follow it.',
        '  pain_point_not_leveraged — prospect revealed a real business pain point (staffing, cost, poor incumbent service) and the rep moved on without connecting it to QCG\'s offering.',
        '  failed_to_handle_objection — prospect raised a real objection and the rep either ignored it or gave a weak, non-specific response.',
        '  no_permission_to_talk — rep launched into the pitch without checking the prospect had time/was free to talk.',
        '  generic_pitch — rep described QCG in terms that could apply to any broker, no care-sector specificity, no named proof points.',
        '  renewal_date_not_asked — rep never established when the current policy renews.',
        '  current_broker_not_asked — rep never established who the current broker/insurer is.',
        '  vague_follow_up — a follow-up outcome was reached but with no specific day/time attached (matches follow_up.specific=false).',
        'Positive flags (require a real trigger — do not apply just because the call went reasonably):',
        '  rapport_established — rep got the prospect volunteering information beyond direct answers, or the tone genuinely warmed (evidenced by a quote), not just politeness.',
        '  strong_questions_asked — rep asked open, consultative questions about the business (not just insurance) that surfaced real information.',
        '  effective_close — rep asked for a specific next step, handled any deflection, and secured a concrete, dated outcome.',
        '  buying_signal_identified — prospect gave a clear signal of interest or dissatisfaction with their incumbent, and the rep explicitly acted on it in the same call.',
        '  credibility_established — rep referenced a specific, named QCG proof point rather than a generic claim — e.g. over 15 years exclusively in the care sector, 95% client retention rate, mock CQC inspections, the Pollard Promise charity (giving back to the care sector, funded by premiums), the free energy market tracking service, 25% BrightHR discount, or the panel solicitor\'s 100% win rate against CQC. A generic "we\'ve been doing this a while" does NOT count — it must be a specific, real proof point.',
        '  specialism_established — rep explicitly positioned QCG as care-sector specialist, not a generalist broker offering a quote.',
        '  claims_test_asked — rep asked about claims history in the last 3-5 years specifically, not just "any claims?".',
        '  consultative_approach — rep asked about the business broadly (staffing, growth plans, challenges) before or instead of leading with insurance.',
        '  broker_conflict_created — rep got the prospect articulating a genuine gap or frustration with their current broker (see broker_conflict field).',
        '',
        'MISSED OPPORTUNITIES — this is the section that matters most for coaching. Populate it generously. Every time the prospect said something a stronger rep would have picked up on and didn\'t — a buying signal, a pain point, an opening for rapport, an unqualified assumption, a chance to create broker jeopardy, a soft follow-up that should have been pinned to a date — capture it here with the exact quote and a concrete, usable alternative line. Aim for as many genuine instances as the call actually contains; do not pad with trivial ones, but do not under-report either.',
        '',
        'REP BEHAVIOURS — be strict, these feed a team dashboard so false positives are worse than false negatives:',
        '  mentioned_care_specialism = true ONLY if the rep explicitly said QCG specialises in the care sector (not just "we\'re an insurance broker").',
        '  mentioned_claims_team = true ONLY if the rep specifically referenced QCG\'s in-house claims handling as a differentiator.',
        '  asked_renewal_date = true if the rep asked, at any point, when the current policy renews.',
        '  asked_claims_history = true ONLY if the rep asked about past claims specifically (not just "any issues?").',
        '  asked_current_broker = true if the rep asked who the current broker/insurer is.',
        '  asked_business_challenges = true if the rep asked an open question about the business beyond insurance (staffing, growth, operational pressure).',
        '  asked_staff_retention = true ONLY if staff retention/turnover was specifically raised by the rep.',
        '  asked_expansion_plans = true ONLY if the rep asked about growth, new sites, or acquisition plans.',
        '  asked_other_insurances = true if the rep asked what other cover the prospect holds (D&O, cyber, fleet etc).',
        '  mentioned_cqc_support = true ONLY if the rep specifically mentioned mock CQC inspections or CQC-related support as a QCG service.',
        '  identified_decision_maker_correctly = true if the rep correctly worked out who actually has authority over the insurance decision, even if that person wasn\'t reached on this call (e.g. correctly identifying "the owner handles this, not the manager").',
        '  consultative_approach = true if the rep asked about the broader business BEFORE or INSTEAD OF leading with an insurance pitch — the ordering matters, not just whether business questions were asked at all.',
        '  generic_pitch = true if the rep described QCG in terms that could apply to any broker, with no care-sector specificity and no named proof point.',
        '',
        'TRANSCRIPT:',
        transcript,
    ]
    prompt = '\n'.join(prompt_lines)

    data = http_post(
        'https://api.openai.com/v1/chat/completions',
        {
            'model':       'gpt-4o-mini',
            'messages':    [{'role':'user','content':prompt}],
            'temperature': 0.2,
            'max_tokens':  4500,
        },
        headers={'Authorization': f'Bearer {OPENAI_KEY}'}
    )
    finish_reason = data['choices'][0].get('finish_reason', 'unknown')
    usage         = data.get('usage', {})
    raw = data['choices'][0]['message']['content'].strip()
    if raw.startswith('```'):
        raw = raw.split('```')[1]
        if raw.startswith('json'): raw = raw[4:]
    raw = raw.strip()

    # ── DIAGNOSTIC LOGGING — remove once the sparse-field issue is diagnosed ──
    # finish_reason='length' means the model got cut off mid-response before
    # completing the JSON — that alone would explain missing fields and is a
    # completely different fix (raise max_tokens) than a model capability issue.
    log(f"    [diag] finish_reason={finish_reason} prompt_tokens={usage.get('prompt_tokens')} completion_tokens={usage.get('completion_tokens')}")
    parsed = json.loads(raw)
    top_level_keys = list(parsed.keys())
    dim_keys = list(parsed.get('dimensions', {}).keys())
    qual = parsed.get('dimensions', {}).get('qualifying', 'MISSING')
    bc   = parsed.get('broker_conflict', 'MISSING')
    mo   = parsed.get('missed_opportunities', 'MISSING')
    log(f"    [diag] top-level keys returned: {top_level_keys}")
    log(f"    [diag] dimensions keys returned: {dim_keys}")
    log(f"    [diag] dimensions.qualifying = {json.dumps(qual)[:300]}")
    log(f"    [diag] broker_conflict = {json.dumps(bc)[:300]}")
    log(f"    [diag] missed_opportunities = {json.dumps(mo)[:300]}")
    # ── END DIAGNOSTIC LOGGING ──

    return parsed

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
            # Prove the write actually landed rather than trusting ok:true alone —
            # this is the only way to catch a stale/misbehaving deployment that
            # returns success without the data actually reaching the Sheet.
            log(f"    [diag] doPost response: ok={result.get('ok')} "
                f"scores_rows={result.get('scores_row_count')} "
                f"missed_rows={result.get('missed_row_count')} "
                f"rapport_rows={result.get('rapport_row_count')} "
                f"version_check={result.get('deployed_version_check', 'MISSING — old deployment!')}")
            return result.get('ok', False)
    except Exception as e:
        log(f"Sheet write warning: {e}")
        return False

# ── MATCH REP ─────────────────────────────────────────────────────────────

def match_rep(detected, user_email=None):
    """Match detected rep name to known reps."""
    # Try user ID map first (most reliable)
    if user_email and user_email in REP_MAP:
        return REP_MAP[user_email], 'high'
    if not detected:
        return '', 'low'
    dl = detected.lower().strip()
    # Try exact variant match
    if dl in REP_NAME_VARIANTS:
        return REP_NAME_VARIANTS[dl], 'high'
    # Try partial variant match
    for variant, canonical in REP_NAME_VARIANTS.items():
        if variant in dl or dl in variant:
            return canonical, 'high'
    # Try against rep names directly
    for name in REP_NAMES:
        parts = name.lower().split(' ')
        if dl == name.lower() or all(p in dl for p in parts):
            return name, 'high'
        if any(p in dl for p in parts if len(p) > 3):
            return name, 'low'
    return '', 'low'

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
    token_time = time.time()
    TOKEN_TTL = 3000  # refresh after 50 minutes (token lasts 3599s)

    # Fetch call records
    records = fetch_call_records(token)

    if not records:
        log("No call records found for this period — exiting")
        return

    processed = 0
    skipped   = 0
    errors    = 0

    # Load already-processed call IDs to avoid duplicates
    processed_ids_file = 'processed_calls.txt'
    try:
        with open(processed_ids_file, 'r') as f:
            already_done = set(f.read().splitlines())
        log(f"Loaded {len(already_done)} previously processed call IDs")
    except FileNotFoundError:
        already_done = set()
        log("No processed calls file yet — starting fresh")

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

        # Skip if already processed
        if call_id in already_done:
            skipped += 1
            continue

        # Extract extension from objectName
        # Format: ipbx:qualitycareinsura:callrecording:users:EXT:...
        obj_name = record.get('objectName', '')
        ext = ''
        parts = obj_name.replace('ipbx.', 'ipbx:').split(':')
        for i, p in enumerate(parts):
            if p == 'users' and i + 1 < len(parts):
                ext = parts[i + 1]
                break

        # Filter to our 5 reps by extension BEFORE downloading
        if ext and ext not in REP_EXTENSIONS:
            log(f"Skipping ext {ext} — not one of our reps")
            skipped += 1
            continue

        # Get rep name from extension if available
        rep_from_ext = REP_EXTENSIONS.get(ext, '')

        # Skip calls under 60 seconds
        duration_secs = duration / 1000 if duration > 1000 else duration
        if duration_secs < 60:
            log(f"Skipping {call_id} — too short ({duration_secs:.0f}s)")
            skipped += 1
            continue

        # Skip Tuesday 09:00-12:30 UK — the call-drive block. Different call
        # goal (raw volume, not consultative BD) means coaching scores from
        # this window aren't comparable to normal calls and would skew the
        # rep's averages. We still count it toward daily call volume by
        # logging a minimal, unscored row rather than silently dropping it.
        if is_call_drive_window(created):
            log(f"  Call-drive window (Tue AM) — logging volume only, not scoring")
            call_drive_payload = {
                'call_id':   f'auto_{call_id}_{date_str}',
                'rep':       rep_from_ext,
                'rep_id':    rep_from_ext[:2].upper() if rep_from_ext else '',
                'manager':   'Josh',
                'date':      date_str,
                'time':      time_str,
                'duration':  duration,
                'filename':  filename,
                'prospect':  prospect,
                'outcome':   '',
                'call_type': 'call_drive',
                'overall':   '',
                'dimensions': {},
                'flags': [], 'verbatim': [], 'narrative': '',
                'strength': '', 'focus': '', 'transcript': '',
                'prospect_intelligence': {}, 'rep_behaviours': {},
            }
            write_to_sheets(call_drive_payload)
            skipped += 1
            already_done.add(call_id)
            with open(processed_ids_file, 'a') as f:
                f.write(call_id + '\n')
            continue

        # Refresh token if approaching expiry
        if time.time() - token_time > TOKEN_TTL:
            log("Refreshing 8x8 auth token...")
            token = get_8x8_token()
            token_time = time.time()

        log(f"Processing call {call_id} ({duration_secs:.0f}s) ext={ext} ({rep_from_ext})")

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

            # ── SERVER-SIDE CALL-TYPE ENFORCEMENT ──
            # The prompt asks the model to self-zero dimension scores for
            # gatekeeper/no_meaningful_conversation calls, but that was
            # entirely self-enforced — if the model skipped classification
            # or got it wrong, a call that never reached a decision-maker
            # could get scored (and coached against) as if it had. This
            # forces the rule in code instead of trusting the model's
            # compliance with its own instructions.
            VALID_CALL_TYPES = {'decision_maker', 'gatekeeper', 'no_meaningful_conversation', 'logistics_admin'}
            raw_call_type = result.get('call_type', '')
            if raw_call_type not in VALID_CALL_TYPES:
                log(f"  WARNING: call_type missing/invalid ('{raw_call_type}') — defaulting to gatekeeper (conservative: don't score rather than score wrongly)")
                result['call_type'] = 'gatekeeper'
            if result['call_type'] in ('gatekeeper', 'no_meaningful_conversation', 'logistics_admin'):
                # Force zero/blank on every field a coaching report would surface —
                # regardless of what the model actually returned for them.
                zero_reason = ('Call never reached a decision-maker — not scored.'
                                if result['call_type'] != 'logistics_admin'
                                else 'Purely administrative/reschedule call — no prospecting opportunity, not scored.')
                zeroed_dim = {'score': 0, 'rationale': zero_reason, 'what_would_raise_it': ''}
                result['dimensions'] = {
                    'opening': dict(zeroed_dim), 'qualifying': dict(zeroed_dim),
                    'objection_handling': dict(zeroed_dim), 'questions_asked': dict(zeroed_dim),
                    'closing': dict(zeroed_dim),
                }
                result['overall'] = ''
                result['qualifying_assessment'] = {}
                result['broker_conflict'] = {}
                result['rapport'] = {}
                result['missed_opportunities'] = []
                result['flags'] = []
                result['strength'] = ''
                result['focus'] = ''
                result['narrative'] = result.get('narrative', '') or 'Call did not reach a decision-maker — nothing to coach on this call.'

            # Match rep
            # Use extension-based rep as ground truth if available
            if rep_from_ext:
                rep            = rep_from_ext
                rep_confidence = 'high'
            else:
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
                'call_type':  result.get('call_type', ''),
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
                'qualifying_assessment': result.get('qualifying_assessment', {}),
                'broker_conflict':       result.get('broker_conflict', {}),
                'rapport':               result.get('rapport', {}),
                'follow_up':             result.get('follow_up', {}),
                'missed_opportunities':  result.get('missed_opportunities', []),
            }

            # Skip no_answer and gatekeeper calls — not worth logging
            call_type = result.get('call_type', 'decision_maker')
            if outcome == 'no_answer' or call_type in ('no_meaningful_conversation',):
                log(f"  Skipping — {call_type}/{outcome}, not logging")
                skipped += 1
                already_done.add(call_id)
                with open(processed_ids_file, 'a') as f:
                    f.write(call_id + '\n')
                continue

            # Log gatekeeper / reschedule calls for intel only (no scoring)
            if call_type == 'gatekeeper':
                log(f"  Gatekeeper call — logging intel only, no score")
            elif call_type == 'logistics_admin':
                log(f"  Logistics/reschedule call — logging intel only, excluded from prospecting metrics")

            # Write to Sheet
            log(f"  Writing to Sheet (rep={rep}, outcome={outcome})...")
            ok = write_to_sheets(payload)
            if ok:
                log(f"  ✓ Saved — {rep} | {outcome} | overall={result.get('overall')}")
                processed += 1
                # Mark as processed
                already_done.add(call_id)
                with open(processed_ids_file, 'a') as f:
                    f.write(call_id + '\n')
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
