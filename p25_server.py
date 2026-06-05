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
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

LOG_FILE = Path.home() / "op25_tippecanoe/p25_log.txt"
STATIC   = Path(__file__).parent / "static"

USERNAME = os.environ.get("P25_USER", "p25")
PASSWORD = os.environ.get("P25_PASSWORD", "scanner")
SUMMARY_MARKER = "=== SUMMARY ==="

app = FastAPI()
security = HTTPBasic()

def require_auth(creds: HTTPBasicCredentials = Depends(security)):
    ok = (
        secrets.compare_digest(creds.username.encode(), USERNAME.encode()) and
        secrets.compare_digest(creds.password.encode(), PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

# ── Parsing ───────────────────────────────────────────────────────────────────

TX_RE         = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\] \[(.+?)\] (.+)$')
SUMMARY_START = re.compile(r'^=== SUMMARY === (.+)$')
SUMMARY_END   = '=' * 40

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
    return parse_log()

@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    stat = LOG_FILE.stat() if LOG_FILE.exists() else None
    return {
        "entries": parse_log(),
        "log_size": stat.st_size if stat else 0,
        "log_mtime": stat.st_mtime if stat else 0,
    }

@app.get("/api/health")
def health():
    return {"ok": True, "log_exists": LOG_FILE.exists()}

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
Note any ongoing or unresolved situations. Be direct and concise."""

class SummarizeReq(BaseModel):
    note: str = ""

@app.post("/api/summarize", dependencies=[Depends(require_auth)])
async def summarize(req: SummarizeReq):
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
