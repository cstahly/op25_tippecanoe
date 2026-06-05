#!/usr/bin/env python3
"""
P25 web app backend.
  uvicorn p25_server:app --host 0.0.0.0 --port 8765
Auth: P25_USER / P25_PASSWORD env vars (defaults: p25 / scanner)
"""
import os, re, json, asyncio, secrets
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

import anthropic
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

LOG_FILE = Path.home() / "op25_tippecanoe/p25_log.txt"
STATIC   = Path(__file__).parent / "static"

USERNAME = os.environ.get("P25_USER", "p25")
PASSWORD = os.environ.get("P25_PASSWORD", "scanner")
SUMMARY_MARKER = "=== SUMMARY ==="
DEFAULT_SUMMARY_LIMIT = 0
RATE_LIMITS: dict[str, float] = {}

app = FastAPI()
security = HTTPBasic()

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

def require_auth(creds: HTTPBasicCredentials = Depends(security)):
    users = _load_users()
    user = users.get(creds.username)
    if user and secrets.compare_digest(creds.password.encode(), user["password"].encode()):
        return {
            "username": creds.username,
            "summarize_interval_seconds": int(user.get("summarize_interval_seconds", DEFAULT_SUMMARY_LIMIT)),
        }
    raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

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
Tippecanoe County, Indiana (Lafayette area).

Talkgroups: TEAS EMS (1833/2225), TCFD/LFD/WLFD/PUFD (fire), TCSD (1813, sheriff), \
LPD (1931), WLPD (2019), PUPD (2119).

10-codes: 10-4=ack, 10-7=OOS, 10-8=in service, 10-20=location, 10-22=disregard, \
10-23=arrived, 10-27=DL check, 10-28=registration, 10-29=warrants, 10-33=emergency, \
10-50=accident, 10-52=ambulance needed, 10-55=DUI, 10-57=hit and run, \
10-78=need assistance, 10-79=notify coroner. Signal 1=en route, Signal 4=arrived, Code 3=L&S.
{note_section}
Radio traffic since last summary (format [HH:MM:SS] [TALKGROUP] transcript):
{block}

Summarize what has been happening. Group by incident. Translate codes. \
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
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
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

app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("p25_server:app", host="0.0.0.0", port=8765, reload=False)
