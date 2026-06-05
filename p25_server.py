#!/usr/bin/env python3
"""
P25 web app backend.
  uvicorn p25_server:app --host 0.0.0.0 --port 8765
Auth: P25_USER / P25_PASSWORD env vars (defaults: p25 / scanner)
"""
import base64, hashlib, hmac, os, re, json, asyncio, secrets, time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import AsyncGenerator, Set

import anthropic
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LOG_FILE    = Path.home() / "op25_tippecanoe/p25_log.txt"
STATIC      = Path(__file__).parent / "static"
AUDIO_FIFO  = "/tmp/p25_audio.fifo"

USERNAME = os.environ.get("P25_USER", "p25")
PASSWORD = os.environ.get("P25_PASSWORD", "scanner")
SUMMARY_MARKER = "=== SUMMARY ==="
DEFAULT_SUMMARY_LIMIT = 0
DEFAULT_SHARE_TOKEN_SECONDS = 14 * 24 * 60 * 60
RATE_LIMITS: dict[str, float] = {}
TOKEN_SECRET = os.environ.get("P25_TOKEN_SECRET", "")

app = FastAPI()

_audio_subs: Set[asyncio.Queue] = set()
_audio_lock  = asyncio.Lock()

async def _audio_broadcast_loop():
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-loglevel", "quiet",
                "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", AUDIO_FIFO,
                "-c:a", "libmp3lame", "-b:a", "32k", "-f", "mp3", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                async with _audio_lock:
                    for q in list(_audio_subs):
                        try:
                            q.put_nowait(chunk)
                        except asyncio.QueueFull:
                            pass
        except Exception:
            pass
        await asyncio.sleep(3)

@app.on_event("startup")
async def _startup():
    asyncio.create_task(_audio_broadcast_loop())

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

def _token_secret_bytes() -> bytes:
    secret = TOKEN_SECRET or f"{USERNAME}:{PASSWORD}:{SUMMARY_MARKER}"
    return secret.encode()

def _sign_token_payload(payload: str) -> str:
    digest = hmac.new(_token_secret_bytes(), payload.encode(), hashlib.sha256).digest()
    return _b64url_encode(digest)

def _make_login_token(username: str, ttl_seconds: int = DEFAULT_SHARE_TOKEN_SECONDS) -> tuple[str, int]:
    exp = int(time.time()) + int(ttl_seconds)
    payload = json.dumps({"u": username, "exp": exp}, separators=(",", ":"), sort_keys=True)
    token = f"{_b64url_encode(payload.encode())}.{_sign_token_payload(payload)}"
    return token, exp

def _verify_login_token(token: str) -> str:
    try:
        payload_b64, signature = token.split(".", 1)
        payload = _b64url_decode(payload_b64).decode()
        expected = _sign_token_payload(payload)
        if not secrets.compare_digest(expected, signature):
            raise ValueError("bad signature")
        data = json.loads(payload)
        username = str(data.get("u", "")).strip()
        exp = int(data.get("exp", 0))
        if not username or exp <= int(time.time()):
            raise ValueError("expired")
        return username
    except Exception as exc:
        raise HTTPException(401, detail="Invalid login token") from exc

def _qr_data_url(text: str) -> str:
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2, box_size=8)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

def _public_base_url(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto")
    if not proto:
        proto = "https" if host and not host.startswith(("127.0.0.1", "localhost")) else request.url.scheme
    return f"{proto}://{host}"

def _load_users() -> dict[str, dict]:
    users = {
        USERNAME: {
            "password": PASSWORD,
            "summarize_interval_seconds": DEFAULT_SUMMARY_LIMIT,
        }
    }
    raw = os.environ.get("P25_EXTRA_USERS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, dict):
                for username, config in extra.items():
                    if isinstance(config, str):
                        users[username] = {
                            "password": config,
                            "summarize_interval_seconds": DEFAULT_SUMMARY_LIMIT,
                        }
                    elif isinstance(config, dict) and config.get("password"):
                        users[username] = {
                            "password": str(config["password"]),
                            "summarize_interval_seconds": int(config.get("summarize_interval_seconds", DEFAULT_SUMMARY_LIMIT)),
                        }
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return users

def require_auth(request: Request):
    users = _load_users()

    t = request.query_params.get("t", "").strip()
    if t:
        username = _verify_login_token(t)
        user = users.get(username)
        if user:
            return {
                "username": username,
                "summarize_interval_seconds": int(user.get("summarize_interval_seconds", DEFAULT_SUMMARY_LIMIT)),
                "auth_mode": "bearer",
            }

    auth_header = request.headers.get("authorization", "").strip()

    if auth_header.lower().startswith("bearer "):
        username = _verify_login_token(auth_header[7:].strip())
        user = users.get(username)
        if user:
            return {
                "username": username,
                "summarize_interval_seconds": int(user.get("summarize_interval_seconds", DEFAULT_SUMMARY_LIMIT)),
                "auth_mode": "bearer",
            }

    if auth_header.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
            username, sep, password = decoded.partition(":")
            if sep:
                user = users.get(username)
                if user and secrets.compare_digest(password.encode(), user["password"].encode()):
                    return {
                        "username": username,
                        "summarize_interval_seconds": int(user.get("summarize_interval_seconds", DEFAULT_SUMMARY_LIMIT)),
                        "auth_mode": "basic",
                    }
        except Exception:
            pass

    raise HTTPException(401, detail="Unauthorized")

def check_summary_rate_limit(auth: dict):
    interval = int(auth.get("summarize_interval_seconds", 0))
    if interval <= 0:
        return
    username = auth["username"]
    now = datetime.now().timestamp()
    last = RATE_LIMITS.get(username, 0)
    remaining = int(interval - (now - last))
    if remaining > 0:
        retry_at = datetime.fromtimestamp(last + interval).strftime("%H:%M:%S")
        raise HTTPException(
            429,
            detail={
                "error": f"Summary is rate limited. Try again at {retry_at}.",
                "retry_after_seconds": remaining,
            },
            headers={"Retry-After": str(remaining)},
        )
    RATE_LIMITS[username] = now

# ── Parsing ───────────────────────────────────────────────────────────────────

TX_RE         = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\] \[(.+?)\] (.+)$')
SUMMARY_START = re.compile(r'^=== SUMMARY === (.+)$')
SUMMARY_END   = '=' * 40
INCIDENT_HEADING_RE = re.compile(r'^#{2,3}\s+\**(?:INCIDENT\s+\d+\s*:\s*)?(.+?)\**\s*$',
                                 re.IGNORECASE)
FIELD_RE = re.compile(r'^(?:-\s+)?\**([^:*]+)\**\s*:\s*(.*)$')

def _agency(tg: str) -> str:
    t = tg.upper()
    if any(x in t for x in ("LPD","WLPD","TCSD","PUPD")): return "police"
    if any(x in t for x in ("LFD","WLFD","TCFD","PUFD")): return "fire"
    if any(x in t for x in ("EMS","TEAS")):                return "ems"
    return "other"

def parse_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    entries, i = [], 0
    while i < len(lines):
        line = lines[i]
        m = TX_RE.match(line)
        if m:
            tg = m.group(2)
            entries.append({"type":"tx","id":f"tx-{len(entries)}",
                            "time":m.group(1),"talkgroup":tg,
                            "agency":_agency(tg),"text":m.group(3)})
            i += 1; continue
        ms = SUMMARY_START.match(line)
        if ms:
            body, i = [], i + 1
            while i < len(lines) and not lines[i].startswith(SUMMARY_END):
                body.append(lines[i]); i += 1
            entries.append({"type":"summary","id":f"sum-{len(entries)}",
                            "time":ms.group(1),"text":"\n".join(body).strip()})
            i += 1; continue
        i += 1
    return entries

def _clean_md(text: str) -> str:
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'[`*_#>]', '', text)
    return text.strip(" -\t")

def _incident_id(title: str) -> str:
    lower = title.lower()
    keyword_ids = [
        (("suicidal", "stone"), "suicidal-stone-gate"),
        (("seizure",), "employee-seizure"),
        (("guardrail",), "guardrail-damage"),
        (("u-haul",), "u-haul-recovery"),
        (("trash", "injury"), "trash-worker-injury"),
        (("status check",), "unit-status-check"),
    ]
    for words, incident_id in keyword_ids:
        if all(word in lower for word in words):
            return incident_id
    clean = re.sub(r'\([^)]*\)', '', lower)
    clean = re.sub(r'\b(incident|active|routine|status|check)\b', '', clean)
    clean = re.sub(r'[^a-z0-9]+', '-', clean).strip('-')
    return clean[:80] or "incident"

def _agency_from_text(text: str) -> str:
    t = text.upper()
    found = []
    for key in ("TCSD", "LPD", "WLPD", "PUPD"):
        if key in t:
            found.append(key)
    for key in ("LFD", "TCFD", "WLFD", "PUFD"):
        if key in t:
            found.append(key)
    if "TEAS" in t or "EMS" in t:
        found.append("TEAS EMS")
    return " / ".join(dict.fromkeys(found)) or "Unknown"

def _status_kind(status: str, text: str) -> str:
    status_upper = status.upper()
    if re.search(r'\b(CLEAR|CLEARED|RESOLVED|AVAILABLE|CANCELLED|CANCELED)\b', status_upper):
        return "clear"
    if re.search(r'\bROUTINE\b', status_upper):
        return "routine"
    if any(x in status_upper for x in ("EN ROUTE", "DISPATCHED", "ACTIVE", "PENDING", "AWAIT")):
        return "active"
    t = f"{status} {text}".upper()
    if re.search(r'\bUNCLEAR\b', t):
        return "watch"
    if re.search(r'\b(CLEAR|CLEARED|RESOLVED|AVAILABLE|CANCELLED|CANCELED)\b', t):
        return "clear"
    if any(x in t for x in ("EN ROUTE", "DISPATCHED", "ACTIVE", "PENDING", "AWAIT")):
        return "active"
    return "watch"

def _extract_incidents_from_summary(entry: dict) -> list[dict]:
    text = entry.get("text", "")
    lines = text.splitlines()
    blocks = []
    current = None
    for line in lines:
        heading = INCIDENT_HEADING_RE.match(line.strip())
        if heading:
            title = _clean_md(heading.group(1))
            title_upper = title.upper()
            if title and not any(skip in title_upper for skip in ("INCIDENTS", "UNRESOLVED", "SUMMARY", "ASSESSMENT", "RECOMMENDATION")):
                if current:
                    blocks.append(current)
                current = {"title": title, "lines": []}
                continue
        if current is not None:
            if line.startswith("---") or re.match(r'^#{1,2}\s+', line):
                blocks.append(current)
                current = None
            else:
                current["lines"].append(line)
    if current:
        blocks.append(current)

    incidents = []
    for block in blocks:
        body_lines = [_clean_md(line) for line in block["lines"] if _clean_md(line)]
        if not body_lines:
            continue
        fields: dict[str, str] = {}
        details = []
        for line in body_lines:
            m = FIELD_RE.match(line)
            if m:
                key = _clean_md(m.group(1)).lower()
                value = _clean_md(m.group(2))
                fields[key] = value
                if key not in ("agency", "status", "location", "time"):
                    details.append(value)
            elif not line.upper().startswith(("AGENCY:", "STATUS:", "LOCATION:")):
                details.append(line)
        title = block["title"]
        blob = "\n".join([title, *body_lines])
        status = fields.get("status", "WATCH")
        agency = fields.get("agency") or _agency_from_text(blob)
        incidents.append({
            "id": _incident_id(title),
            "title": title,
            "summary_time": entry.get("time", ""),
            "agency": agency,
            "status": status,
            "status_kind": _status_kind(status, blob),
            "location": fields.get("location", ""),
            "details": details[:8],
            "source_summary_id": entry.get("id", ""),
        })
    return incidents

def derive_incidents(entries: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    summaries = [e for e in entries if e.get("type") == "summary"]
    for entry in reversed(summaries):
        for incident in _extract_incidents_from_summary(entry):
            existing = merged.get(incident["id"])
            if not existing:
                incident["updates"] = 1
                incident["first_seen"] = incident["summary_time"]
                incident["last_seen"] = incident["summary_time"]
                merged[incident["id"]] = incident
            else:
                existing["updates"] += 1
                existing["first_seen"] = incident["summary_time"]
    priority = {"active": 0, "watch": 1, "routine": 2, "clear": 3}
    ordered = sorted(merged.values(), key=lambda i: i.get("last_seen", ""), reverse=True)
    return sorted(ordered, key=lambda i: priority.get(i.get("status_kind", "watch"), 1))

def read_since_last_summary() -> list[str]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    last = -1
    for i, l in enumerate(lines):
        if SUMMARY_MARKER in l:
            last = i
    return [l for l in lines[last+1:] if l.strip()]

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/entries", dependencies=[Depends(require_auth)])
def get_entries():
    return JSONResponse(parse_log(), headers={"Cache-Control": "no-store"})

@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    stat = LOG_FILE.stat() if LOG_FILE.exists() else None
    entries = parse_log()
    return JSONResponse({
        "entries": entries,
        "entries_latest": list(reversed(entries)),
        "incidents": derive_incidents(entries),
        "log_size": stat.st_size if stat else 0,
        "log_mtime": stat.st_mtime if stat else 0,
    }, headers={"Cache-Control": "no-store"})

@app.get("/api/logs/download")
def download_logs(auth: dict = Depends(require_auth)):
    if not LOG_FILE.exists():
        raise HTTPException(404, detail="Log file not found")
    filename = f"p25_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return FileResponse(LOG_FILE, media_type="text/plain", filename=filename)

@app.get("/api/health")
def health():
    return {"ok": True, "log_exists": LOG_FILE.exists()}

@app.get("/")
def index():
    return FileResponse(
        STATIC / "index.html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )

@app.get("/api/stream", dependencies=[Depends(require_auth)])
async def live_stream(request: Request):
    state = {"pos": LOG_FILE.stat().st_size if LOG_FILE.exists() else 0}

    async def generator() -> AsyncGenerator[str, None]:
        while not await request.is_disconnected():
            if LOG_FILE.exists():
                size = LOG_FILE.stat().st_size
                if size > state["pos"]:
                    with open(LOG_FILE, errors="replace") as f:
                        f.seek(state["pos"]); new = f.read()
                        state["pos"] = f.tell()
                    for line in new.splitlines():
                        m = TX_RE.match(line)
                        if m:
                            tg = m.group(2)
                            payload = {"type":"tx","time":m.group(1),
                                       "talkgroup":tg,"agency":_agency(tg),
                                       "text":m.group(3)}
                            yield f"data: {json.dumps(payload)}\n\n"
                        ms = SUMMARY_START.match(line)
                        if ms:
                            yield f"data: {json.dumps({'type':'summary_start'})}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(generator(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

PROMPT_TEMPLATE = """\
You are an experienced public safety dispatcher reviewing radio traffic logs from \
Tippecanoe County, Indiana. You know this area well.

Geography: Lafayette and West Lafayette straddle the Wabash River. \
Major roads — I-65 (N/S), US 52/Sagamore Pkwy (bypass), SR 25, SR 38, SR 43, \
Creasy Lane, Veterans Memorial Pkwy, McCarty Lane, Teal Rd, Schuyler Ave, \
South St, Main St (downtown Lafayette). \
Key landmarks — IU Health Arnett Hospital (2600 Greenbush St, Lafayette), \
St. Elizabeth East (1501 Hartford St, Lafayette), Franciscan Lafayette (1501 Hartford St area), \
Purdue University (West Lafayette campus), Tippecanoe County Courthouse (downtown Lafayette), \
Columbian Park / zoo (south Lafayette), Happy Hollow Park, River Road corridor. \
Notable areas — Eastside (east of I-65), South End (Creasy/McCarty area), \
West Lafayette (Purdue/campus), downtown Lafayette (Main/5th St grid), \
Battle Ground (north county), Shadeland area (northwest Lafayette), \
State St corridor (student housing near Purdue), Murdock Park area. \
Major employers/facilities — Subaru of Indiana Automotive (SIA, northwest Lafayette), \
Purdue University, TCOM, Alcoa Warrick (south county). \
Zip codes: 47901/47904 (Lafayette core), 47905 (south Lafayette), 47906 (West Lafayette/Purdue), \
47907 (West Lafayette east).

Talkgroups: TEAS EMS (1833/2225), TCFD/LFD/WLFD/PUFD (fire depts), \
TCSD (1813, Tippecanoe County Sheriff), LPD (1931, Lafayette Police), \
WLPD (2019, West Lafayette Police), PUPD (2119, Purdue University Police).

10-codes: 10-4=ack, 10-7=OOS, 10-8=in service, 10-20=location, 10-22=disregard, \
10-23=arrived, 10-27=DL check, 10-28=registration, 10-29=warrants, 10-33=emergency, \
10-50=accident, 10-52=ambulance needed, 10-55=DUI, 10-57=hit and run, \
10-78=need assistance, 10-79=notify coroner. Signal 1=en route, Signal 4=arrived, Code 3=L&S.
{note_section}
Radio traffic since last summary (format [HH:MM:SS] [TALKGROUP] transcript):
{block}

Summarize what has been happening. Group by incident. Translate codes. \
When you recognize a local address, business, or landmark in the Lafayette area, \
include that context. If you are unsure about a specific local address or entity, \
use web_search to look it up silently — do not narrate that you are searching. \
Use one markdown section per incident with this shape:
### INCIDENT N: Short incident title
- Agency: agency or agencies
- Status: ACTIVE, DISPATCHED, EN ROUTE, ROUTINE, CLEAR, or PENDING
- Location: best known location, or Unknown
- Details: concise update
- Action: what remains unresolved or what to watch for

Use stable incident titles when an older incident is still being updated. \
Note any unresolved situations. Be direct and concise."""

class SummarizeReq(BaseModel):
    note: str = ""

class ShareLoginReq(BaseModel):
    ttl_seconds: int = DEFAULT_SHARE_TOKEN_SECONDS
    for_username: str = ""

@app.get("/api/users")
def list_users(auth: dict = Depends(require_auth)):
    if auth["username"] != USERNAME:
        raise HTTPException(403, detail="Only the primary user can list users")
    return JSONResponse([{"username": u} for u in _load_users().keys()])

@app.post("/api/login/share")
def share_login(req: ShareLoginReq, request: Request, auth: dict = Depends(require_auth)):
    if auth["username"] != USERNAME:
        raise HTTPException(403, detail="Only the primary user can generate QR codes")
    target = req.for_username.strip() or auth["username"]
    users = _load_users()
    if target not in users:
        raise HTTPException(400, detail=f"Unknown user: {target}")
    ttl = max(300, min(int(req.ttl_seconds or DEFAULT_SHARE_TOKEN_SECONDS), 30 * 24 * 60 * 60))
    token, exp = _make_login_token(target, ttl)
    share_path = f"/?token={token}"
    share_url = f"{_public_base_url(request)}{share_path}"
    return JSONResponse({
        "username": target,
        "token": token,
        "url": share_url,
        "qr_data_url": _qr_data_url(share_url),
        "expires_at": datetime.fromtimestamp(exp).isoformat(timespec="seconds"),
        "ttl_seconds": ttl,
    }, headers={"Cache-Control": "no-store"})

@app.post("/api/summarize")
async def summarize(req: SummarizeReq, auth: dict = Depends(require_auth)):
    try:
        check_summary_rate_limit(auth)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": str(exc.detail)}

        async def stream_limited():
            yield f"data: {json.dumps({'error':detail.get('error', 'Rate limited'),'done':True,'retry_after_seconds':detail.get('retry_after_seconds', 0)})}\n\n"

        return StreamingResponse(
            stream_limited(),
            status_code=exc.status_code,
            media_type="text/event-stream",
            headers={**(exc.headers or {}), "Cache-Control":"no-cache","X-Accel-Buffering":"no"},
        )

    lines = read_since_last_summary()

    async def stream_empty():
        yield f"data: {json.dumps({'done':True,'text':'No new traffic since last summary.'})}\n\n"

    if not lines:
        return StreamingResponse(stream_empty(), media_type="text/event-stream")

    block        = "\n".join(lines)
    note_section = f"\nOperator note: {req.note}\n" if req.note else ""
    prompt       = PROMPT_TEMPLATE.format(note_section=note_section, block=block)

    async def stream_summary() -> AsyncGenerator[str, None]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            msg = "ANTHROPIC_API_KEY is not set for p25-server.service."
            yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
            return

        client = anthropic.AsyncAnthropic()
        full   = ""
        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                tools=[{"type": "web_search_20260209", "name": "web_search"}],
                messages=[{"role":"user","content":prompt}],
            ) as s:
                async for chunk in s.text_stream:
                    full += chunk
                    yield f"data: {json.dumps({'text':chunk})}\n\n"
        except Exception as exc:
            msg = f"Summary failed: {exc}"
            yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
            return

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write(f"\n{SUMMARY_MARKER} {ts}\n{full.strip()}\n{'='*40}\n\n")
        yield f"data: {json.dumps({'done':True,'time':ts,'text':full.strip()})}\n\n"

    return StreamingResponse(stream_summary(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.get("/api/audio/token")
def audio_token(auth: dict = Depends(require_auth)):
    token, exp = _make_login_token(auth["username"], ttl_seconds=3600)
    return JSONResponse({"token": token,
                         "expires_at": datetime.fromtimestamp(exp).isoformat(timespec="seconds")})

@app.get("/api/audio")
async def audio_stream(auth: dict = Depends(require_auth)):
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    async with _audio_lock:
        _audio_subs.add(q)
    async def generate():
        try:
            while True:
                chunk = await asyncio.wait_for(q.get(), timeout=30)
                yield chunk
        except asyncio.TimeoutError:
            pass
        finally:
            async with _audio_lock:
                _audio_subs.discard(q)
    return StreamingResponse(generate(), media_type="audio/mpeg",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("p25_server:app", host="0.0.0.0", port=8765, reload=False)
