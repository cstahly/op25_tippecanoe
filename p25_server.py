#!/usr/bin/env python3
"""
P25 web app backend.
  uvicorn p25_server:app --host 0.0.0.0 --port 8765
Auth: P25_USER / P25_PASSWORD env vars (defaults: p25 / scanner)
"""
import base64, hashlib, hmac, os, re, json, asyncio, secrets, time, sqlite3
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
DB_FILE     = Path.home() / "op25_tippecanoe/p25_state.db"

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
    ensure_state_ready()
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
SUMMARY_START      = re.compile(r'^=== SUMMARY === (.+)$')
FULL_SUMMARY_MARKER = "=== FULL SUMMARY ==="
FULL_SUMMARY_START  = re.compile(r'^=== FULL SUMMARY === (.+)$')
SUMMARY_END         = '=' * 40
INCIDENT_HEADING_RE = re.compile(r'^#{2,3}\s+\**(?:INCIDENT\s+(?:(\d+)|[A-Z])\s*:\s*)?(.+?)\**\s*$',
                                 re.IGNORECASE)
FIELD_RE = re.compile(r'^(?:-\s+)?\**([^:*]+)\**\s*:\s*(.*)$')
SUMMARY_HAS_INCIDENT_RE = re.compile(r'^#{2,3}\s+\**INCIDENT\s+(?:\d+|NEW|[A-Z])\s*:', re.IGNORECASE | re.MULTILINE)

def _strip_summary_preamble(text: str) -> str:
    match = SUMMARY_HAS_INCIDENT_RE.search(text)
    return text[match.start():].strip() if match else text.strip()

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
        ms = SUMMARY_START.match(line) or FULL_SUMMARY_START.match(line)
        if ms:
            is_full = bool(FULL_SUMMARY_START.match(line))
            body, i = [], i + 1
            while i < len(lines) and not lines[i].startswith(SUMMARY_END):
                body.append(lines[i]); i += 1
            entries.append({"type":"summary","id":f"sum-{len(entries)}",
                            "time":ms.group(1),"text":"\n".join(body).strip(),
                            "full": is_full})
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
            number = heading.group(1)
            title = _clean_md(heading.group(2))
            title_upper = title.upper()
            if title and not any(skip in title_upper for skip in ("INCIDENTS", "UNRESOLVED", "SUMMARY", "ASSESSMENT", "RECOMMENDATION")):
                if current:
                    blocks.append(current)
                current = {"number": number, "title": title, "lines": []}
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
        number = block.get("number")
        incidents.append({
            "id": f"incident-{number}" if number else _incident_id(title),
            "number": int(number) if number else None,
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

    def _merge(incident: dict, newer_wins: bool):
        existing = merged.get(incident["id"])
        if not existing:
            incident["updates"] = 1
            incident["first_seen"] = incident["summary_time"]
            incident["last_seen"]  = incident["summary_time"]
            merged[incident["id"]] = incident
        else:
            if newer_wins and incident["summary_time"] > existing["last_seen"]:
                # This occurrence is more recent — promote it to authoritative
                incident["updates"]    = existing["updates"] + 1
                incident["first_seen"] = min(existing["first_seen"], incident["summary_time"])
                incident["last_seen"]  = incident["summary_time"]
                incident.setdefault("recent_tx", existing.get("recent_tx", 0))
                merged[incident["id"]] = incident
            else:
                existing["updates"] += 1
                if incident["summary_time"] < existing["first_seen"]:
                    existing["first_seen"] = incident["summary_time"]

    # Layer 1: the latest full summary is the baseline source of truth.
    full_sums = sorted(
        [e for e in entries if e.get("type") == "summary" and e.get("full")],
        key=lambda e: e["time"],
    )
    last_full = full_sums[-1] if full_sums else None
    last_full_time = last_full.get("time", "") if last_full else ""
    for entry in ([last_full] if last_full else []):
        for inc in _extract_incidents_from_summary(entry):
            _merge(inc, newer_wins=True)

    # Layer 2: incremental summaries after that full baseline override it.
    incr_sums = sorted(
        [
            e for e in entries
            if e.get("type") == "summary" and not e.get("full") and e.get("time", "") > last_full_time
        ],
        key=lambda e: e["time"],
    )
    for entry in incr_sums:
        for inc in _extract_incidents_from_summary(entry):
            _merge(inc, newer_wins=True)

    # Layer 3: raw tx entries since last summary — tag incidents with recent activity
    all_sum_times = [e["time"] for e in entries if e.get("type") == "summary"]
    last_sum_time = max(all_sum_times) if all_sum_times else ""
    recent_tx = [e for e in entries if e.get("type") == "tx" and e.get("time", "") > last_sum_time]
    if recent_tx:
        def _cats(agency_str: str) -> set:
            a = agency_str.upper()
            cats = set()
            if any(x in a for x in ("LPD", "WLPD", "TCSD", "PUPD", "POLICE", "SHERIFF")): cats.add("police")
            if any(x in a for x in ("LFD", "WLFD", "TCFD", "PUFD", "FIRE")): cats.add("fire")
            if any(x in a for x in ("EMS", "TEAS")): cats.add("ems")
            return cats

        for inc in merged.values():
            inc_cats = _cats(inc.get("agency", ""))
            if not inc_cats:
                continue
            matching = [t for t in recent_tx if _cats(t.get("agency", "")) & inc_cats]
            if matching:
                inc["recent_tx"]      = len(matching)
                inc["last_tx_time"]   = max(t["time"] for t in matching)

    priority = {"active": 0, "watch": 1, "routine": 2, "clear": 3}
    ordered = sorted(merged.values(), key=lambda i: i.get("last_seen", ""), reverse=True)
    return sorted(ordered, key=lambda i: priority.get(i.get("status_kind", "watch"), 1))

def incident_board_context(entries: list[dict]) -> str:
    numbered = [inc for inc in derive_incidents(entries) if inc.get("number")]
    if not numbered:
        return "Existing numbered incident board: none yet. Start numbering at INCIDENT 1."
    numbered.sort(key=lambda inc: int(inc["number"]))
    lines = [
        "Existing numbered incident board. Reuse these numbers for the same real-world incidents:"
    ]
    for inc in numbered:
        location = inc.get("location") or "Unknown"
        status = inc.get("status") or "WATCH"
        agency = inc.get("agency") or "Unknown"
        title = inc.get("title") or "Incident"
        lines.append(f"- INCIDENT {inc['number']}: {title} | {agency} | {status} | {location}")
    return "\n".join(lines)

def incident_board_context_from_incidents(incidents: list[dict]) -> str:
    numbered = [inc for inc in incidents if inc.get("number")]
    if not numbered:
        return "Existing numbered incident board: none yet. Start numbering at INCIDENT 1."

    def sort_key(inc: dict):
        kind = inc.get("status_kind", "watch")
        priority = 0 if kind in ("active", "watch") else 1
        return (priority, str(inc.get("last_seen", "")), int(inc["number"]))

    open_items = [inc for inc in numbered if inc.get("status_kind") != "clear"]
    recent_closed = [
        inc for inc in numbered
        if inc.get("status_kind") == "clear"
    ]
    open_items.sort(key=sort_key, reverse=True)
    recent_closed.sort(key=lambda inc: (str(inc.get("last_seen", "")), int(inc["number"])), reverse=True)

    selected = open_items[:60] + recent_closed[:10]
    selected.sort(key=lambda inc: int(inc["number"]))

    lines = [
        "Existing incident board subset. Reuse these numbers for matching. "
        f"Showing {len(selected)} of {len(numbered)} incidents: most recent open items plus recently cleared items."
    ]
    for inc in selected:
        location = inc.get("location") or "Unknown"
        status = inc.get("status") or "WATCH"
        title = inc.get("title") or "Incident"
        lines.append(f"{inc['number']}. {status} | {title} | {location}")
    return "\n".join(lines)

def _is_summary_marker(l: str) -> bool:
    return SUMMARY_MARKER in l or FULL_SUMMARY_MARKER in l

def read_since_last_summary() -> list[str]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    start = 0
    for i, l in enumerate(lines):
        if _is_summary_marker(l):
            start = i + 1
            while start < len(lines) and lines[start] != SUMMARY_END:
                start += 1
            if start < len(lines) and lines[start] == SUMMARY_END:
                start += 1
    return [l for l in lines[start:] if l.strip()]

def read_full_log() -> list[str]:
    """All transcript lines, stripping existing summary blocks."""
    if not LOG_FILE.exists():
        return []
    result = []
    in_summary = False
    for l in LOG_FILE.read_text(errors="replace").splitlines():
        if _is_summary_marker(l):
            in_summary = True
            continue
        if in_summary and set(l.strip()) <= {'='}:
            in_summary = False
            continue
        if not in_summary and l.strip():
            result.append(l)
    return result

# ── Durable state ────────────────────────────────────────────────────────────

def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_state_db() -> None:
    with _db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS transmissions (
            id INTEGER PRIMARY KEY,
            time TEXT NOT NULL,
            talkgroup TEXT NOT NULL,
            agency TEXT NOT NULL,
            text TEXT NOT NULL,
            raw_line TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS incident_state (
            number INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            agency TEXT NOT NULL,
            status TEXT NOT NULL,
            status_kind TEXT NOT NULL,
            location TEXT NOT NULL,
            details_json TEXT NOT NULL,
            action TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS app_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS summary_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            from_tx_id INTEGER NOT NULL,
            to_tx_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            output TEXT NOT NULL DEFAULT '',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            completed_at TEXT NOT NULL DEFAULT ''
        );
        """)

def _get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default

def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_state(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )

def _last_valid_summary_tx_id_from_log() -> int:
    if not LOG_FILE.exists():
        return 0
    tx_count = 0
    last_summary_tx = 0
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if TX_RE.match(line):
            tx_count += 1
            i += 1
            continue
        if _is_summary_marker(line):
            j = i + 1
            while j < len(lines) and lines[j] != SUMMARY_END:
                j += 1
            if j < len(lines) and lines[j] == SUMMARY_END:
                last_summary_tx = tx_count
                i = j + 1
                continue
        i += 1
    return last_summary_tx

def sync_transmissions_from_log(conn: sqlite3.Connection) -> int:
    rows = []
    if LOG_FILE.exists():
        for line in LOG_FILE.read_text(errors="replace").splitlines():
            m = TX_RE.match(line)
            if not m:
                continue
            tx_id = len(rows) + 1
            tg = m.group(2)
            rows.append((tx_id, m.group(1), tg, _agency(tg), m.group(3), line))
    conn.execute("DELETE FROM transmissions")
    conn.executemany(
        "INSERT INTO transmissions(id, time, talkgroup, agency, text, raw_line) VALUES(?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)

def incident_rows_from_db(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM incident_state ORDER BY "
        "CASE status_kind WHEN 'active' THEN 0 WHEN 'watch' THEN 1 WHEN 'routine' THEN 2 WHEN 'clear' THEN 3 ELSE 1 END, "
        "last_seen DESC"
    ).fetchall()
    return [_incident_row_to_api(row) for row in rows]

def _incident_row_to_api(row: sqlite3.Row) -> dict:
    details = json.loads(row["details_json"] or "[]")
    return {
        "id": f"incident-{row['number']}",
        "number": row["number"],
        "title": row["title"],
        "agency": row["agency"],
        "status": row["status"],
        "status_kind": row["status_kind"],
        "location": row["location"],
        "details": details,
        "action": row["action"],
        "first_seen": row["first_seen"],
        "last_seen": row["last_seen"],
        "summary_time": row["last_seen"],
        "updates": 1,
    }

def _incident_by_number(conn: sqlite3.Connection, number: int) -> dict | None:
    row = conn.execute("SELECT * FROM incident_state WHERE number = ?", (number,)).fetchone()
    return _incident_row_to_api(row) if row else None

def _upsert_incident(conn: sqlite3.Connection, inc: dict, now: str) -> None:
    number = int(inc["number"])
    details = inc.get("details") or []
    if isinstance(details, str):
        details = [details]
    title = str(inc.get("title") or "Incident").strip()
    status = str(inc.get("status") or "WATCH").strip()
    blob = "\n".join([title, status, *(str(d) for d in details)])
    row = conn.execute("SELECT first_seen FROM incident_state WHERE number = ?", (number,)).fetchone()
    first_seen = row["first_seen"] if row else now
    conn.execute(
        """
        INSERT INTO incident_state
            (number, title, agency, status, status_kind, location, details_json, action, first_seen, last_seen, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(number) DO UPDATE SET
            title = excluded.title,
            agency = excluded.agency,
            status = excluded.status,
            status_kind = excluded.status_kind,
            location = excluded.location,
            details_json = excluded.details_json,
            action = excluded.action,
            last_seen = excluded.last_seen,
            updated_at = excluded.updated_at
        """,
        (
            number,
            title,
            str(inc.get("agency") or "Unknown").strip(),
            status,
            _status_kind(status, blob),
            str(inc.get("location") or "Unknown").strip(),
            json.dumps([str(d).strip() for d in details if str(d).strip()][:8]),
            str(inc.get("action") or "").strip(),
            first_seen,
            now,
            now,
        ),
    )

def seed_incident_state(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT COUNT(*) AS c FROM incident_state").fetchone()["c"]
    if existing:
        return
    for inc in derive_incidents(parse_log()):
        if not inc.get("number"):
            continue
        _upsert_incident(conn, inc, inc.get("last_seen") or inc.get("summary_time") or "")

def ensure_state_ready() -> None:
    init_state_db()
    with _db() as conn:
        tx_count = sync_transmissions_from_log(conn)
        seed_incident_state(conn)
        if not _get_state(conn, "last_summarized_tx_id"):
            _set_state(conn, "last_summarized_tx_id", str(_last_valid_summary_tx_id_from_log()))
        _set_state(conn, "last_tx_id", str(tx_count))

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/entries", dependencies=[Depends(require_auth)])
def get_entries():
    return JSONResponse(parse_log(), headers={"Cache-Control": "no-store"})

@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    ensure_state_ready()
    stat = LOG_FILE.stat() if LOG_FILE.exists() else None
    entries = parse_log()
    with _db() as conn:
        incidents = incident_rows_from_db(conn)
    return JSONResponse({
        "entries": entries,
        "entries_latest": list(reversed(entries)),
        "incidents": incidents or derive_incidents(entries),
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
{mode_section}
{incident_context}

Radio traffic (format [HH:MM:SS] [TALKGROUP] transcript):
{block}

Summarize what has been happening. Group by incident. Translate codes. \
When you recognize a local address, business, or landmark in the Lafayette area, \
include that context. If you are unsure about a specific local address or entity, \
use web_search to look it up silently — do not narrate that you are searching. \
Output only the final incident sections. Do not include preamble, analysis narration, search narration, \
or phrases like "I'll analyze", "I'll work through", "let me", or "now I have enough". \
Use one markdown section per incident with this shape:
### INCIDENT 12: Short incident title
- Agency: agency or agencies
- Status: ACTIVE, DISPATCHED, EN ROUTE, ROUTINE, CLEAR, or PENDING
- Location: pure mappable address/place only, or Unknown. Do not add context, explanations, parentheticals, routes, or "near..." guesses here.
- Details: concise update
- Action: what remains unresolved or what to watch for

Incident numbers are persistent identifiers. Use only integers, never letters. \
Reuse an existing incident number when updating the same real-world incident. \
Do not renumber incidents. For a new incident, use the next unused integer after the highest existing incident number.

Put any local context, landmark explanation, uncertainty, or secondary locations in Details, not Location. \
Location is used directly as a map link label and query, so keep it clean and exact.

Use stable incident titles when an older incident is still being updated. \
Note any unresolved situations. Be direct and concise."""

FULL_CHUNK_TEMPLATE = """\
You are reviewing one chunk of Tippecanoe County public safety radio traffic.

{incident_context}

This is chunk {chunk_num} of {chunk_count}. Extract incident facts from this chunk only. \
Preserve existing incident numbers when the traffic clearly belongs to one. For new incidents, \
label them NEW in this chunk summary; do not assign final numbers here.
Output only incident sections. Do not include preamble, analysis narration, or search narration.

Radio traffic:
{block}

Return concise markdown sections:
### INCIDENT 12 or NEW: Short title
- Agency:
- Status:
- Location: pure mappable address/place only, or Unknown
- Details:
- Action:
"""

FULL_CONSOLIDATE_TEMPLATE = """\
You are creating the final full-session incident summary for Tippecanoe County public safety radio traffic.

{incident_context}

Consolidate the chunk summaries below into one current incident list. Incident numbers are persistent \
identifiers. Use only integers, never letters. Reuse existing incident numbers for the same real-world \
incident. Do not renumber incidents. Assign new incidents the next unused integer after the highest \
existing incident number.

Location must be a pure mappable address/place only, or Unknown. Put context, uncertainty, and secondary \
locations in Details, not Location.
Output only the final incident sections. Do not include preamble, analysis narration, search narration, \
or phrases like "I'll analyze", "I'll work through", "let me", or "now I have enough".

Use one markdown section per incident:
### INCIDENT 12: Short incident title
- Agency: agency or agencies
- Status: ACTIVE, DISPATCHED, EN ROUTE, ROUTINE, CLEAR, or PENDING
- Location: pure mappable address/place only, or Unknown
- Details: concise full-arc update
- Action: what remains unresolved or what to watch for

Chunk summaries:
{block}
"""

INCREMENTAL_JSON_TEMPLATE = """\
You update a live incident board for Tippecanoe County public safety radio traffic.

Existing incident board:
{incident_context}

New transmissions since the last successful summary:
{block}

Return JSON only, with no markdown and no preamble. The JSON shape is:
{{
  "incidents": [
    {{
      "number": 12,
      "title": "Short incident title",
      "agency": "agency or agencies",
      "status": "ACTIVE, DISPATCHED, EN ROUTE, ROUTINE, CLEAR, or PENDING",
      "location": "pure mappable address/place only, or Unknown",
      "details": ["one short fact from the new transmissions", "optional second short fact"],
      "action": "what remains unresolved or what to watch for"
    }}
  ]
}}

Rules:
- Include only incidents directly mentioned or updated by the new transmissions.
- Reuse an existing incident number for the same real-world incident.
- For a new incident, assign the next unused integer after the highest existing incident number.
- Use only integer incident numbers, never letters.
- Do not include administrative traffic unless it changes an incident.
- Location must be a pure mappable address/place only, or Unknown. Put uncertainty and context in details.
- Keep each incident concise: at most two details.
- If there are no incident updates, return {{"incidents":[]}}.
"""

class SummarizeReq(BaseModel):
    note: str = ""
    full: bool = False

class ShareLoginReq(BaseModel):
    ttl_seconds: int = DEFAULT_SHARE_TOKEN_SECONDS
    for_username: str = ""

class IncidentUpdateReq(BaseModel):
    title: str | None = None
    agency: str | None = None
    status: str | None = None
    location: str | None = None
    details: list[str] | None = None
    action: str | None = None

def _chunk_lines(lines: list[str], max_chars: int = 80000) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        line_size = len(line) + 1
        if current and size + line_size > max_chars:
            chunks.append(current)
            current = []
            size = 0
        current.append(line)
        size += line_size
    if current:
        chunks.append(current)
    return chunks

async def _anthropic_text(
    client: anthropic.AsyncAnthropic,
    prompt: str,
    max_tokens: int,
    use_search: bool,
    on_chunk=None,
) -> str:
    kwargs = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "messages": [{"role":"user","content":prompt}],
    }
    if use_search:
        kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]
    async with client.messages.stream(**kwargs) as s:
        full = ""
        async for chunk in s.text_stream:
            full += chunk
            if on_chunk:
                await on_chunk(chunk)
        message = await s.get_final_message()
        if message.stop_reason == "max_tokens":
            raise RuntimeError("Claude hit max_tokens before finishing; nothing was written to the log.")
        return full

def _parse_json_object(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            return json.loads(cleaned[start:end + 1])
        raise

def _validate_incident_updates(payload: dict) -> list[dict]:
    incidents = payload.get("incidents")
    if not isinstance(incidents, list):
        raise ValueError("Claude JSON missing incidents list")
    valid = []
    for inc in incidents:
        if not isinstance(inc, dict):
            raise ValueError("Incident update is not an object")
        number = inc.get("number")
        if not isinstance(number, int):
            raise ValueError("Incident number must be an integer")
        title = str(inc.get("title") or "").strip()
        if not title:
            raise ValueError(f"Incident {number} missing title")
        details = inc.get("details") or []
        if isinstance(details, str):
            details = [details]
        if not isinstance(details, list):
            raise ValueError(f"Incident {number} details must be a list")
        valid.append({
            "number": number,
            "title": title,
            "agency": str(inc.get("agency") or "Unknown").strip(),
            "status": str(inc.get("status") or "WATCH").strip(),
            "location": str(inc.get("location") or "Unknown").strip(),
            "details": [str(d).strip() for d in details if str(d).strip()][:2],
            "action": str(inc.get("action") or "").strip(),
        })
    return valid

def _incident_updates_markdown(updates: list[dict]) -> str:
    if not updates:
        return "No incident updates."
    blocks = []
    for inc in updates:
        details = " ".join(inc.get("details") or []) or "Updated by recent radio traffic."
        blocks.append(
            f"### INCIDENT {inc['number']}: {inc['title']}\n"
            f"- Agency: {inc.get('agency') or 'Unknown'}\n"
            f"- Status: {inc.get('status') or 'WATCH'}\n"
            f"- Location: {inc.get('location') or 'Unknown'}\n"
            f"- Details: {details}\n"
            f"- Action: {inc.get('action') or 'None'}"
        )
    return "\n\n---\n\n".join(blocks)

@app.post("/api/incidents/{number}")
def update_incident(number: int, req: IncidentUpdateReq, auth: dict = Depends(require_auth)):
    ensure_state_ready()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _db() as conn:
        row = conn.execute("SELECT * FROM incident_state WHERE number = ?", (number,)).fetchone()
        if not row:
            raise HTTPException(404, detail=f"Incident {number} not found")
        current_details = json.loads(row["details_json"] or "[]")
        inc = {
            "number": number,
            "title": req.title if req.title is not None else row["title"],
            "agency": req.agency if req.agency is not None else row["agency"],
            "status": req.status if req.status is not None else row["status"],
            "location": req.location if req.location is not None else row["location"],
            "details": req.details if req.details is not None else current_details,
            "action": req.action if req.action is not None else row["action"],
        }
        _upsert_incident(conn, inc, now)
        return JSONResponse(_incident_by_number(conn, number), headers={"Cache-Control": "no-store"})

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
    if req.full:
        if auth["username"] != USERNAME:
            raise HTTPException(403, detail="Only the primary user can run a full summary")
        lines = read_full_log()
        tx_rows = []
        from_tx_id = to_tx_id = 0
    else:
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
        ensure_state_ready()
        with _db() as conn:
            from_tx_id = int(_get_state(conn, "last_summarized_tx_id", "0") or "0")
            tx_rows = conn.execute(
                "SELECT * FROM transmissions WHERE id > ? ORDER BY id",
                (from_tx_id,),
            ).fetchall()
            to_tx_id = tx_rows[-1]["id"] if tx_rows else from_tx_id
            lines = [row["raw_line"] for row in tx_rows]

    async def stream_empty():
        yield f"data: {json.dumps({'done':True,'text':'No new traffic since last summary.'})}\n\n"

    if not lines:
        return StreamingResponse(stream_empty(), media_type="text/event-stream")

    if not req.full:
        async def stream_incremental_summary() -> AsyncGenerator[str, None]:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                msg = "ANTHROPIC_API_KEY is not set for p25-server.service."
                yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
                return

            created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with _db() as conn:
                current_incidents = incident_rows_from_db(conn)
                incident_context = incident_board_context_from_incidents(current_incidents)
                cur = conn.execute(
                    "INSERT INTO summary_jobs(mode, from_tx_id, to_tx_id, status, created_at) VALUES(?, ?, ?, ?, ?)",
                    ("incremental", from_tx_id + 1, to_tx_id, "running", created_at),
                )
                job_id = cur.lastrowid

            prompt = INCREMENTAL_JSON_TEMPLATE.format(
                incident_context=incident_context,
                block="\n".join(lines),
            )
            try:
                client = anthropic.AsyncAnthropic()
                message = await client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    messages=[{"role": "user", "content": prompt}],
                )
                if message.stop_reason == "max_tokens":
                    raise RuntimeError("Claude hit max_tokens before finishing; cursor was not advanced.")
                raw = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
                updates = _validate_incident_updates(_parse_json_object(raw))
                markdown = _incident_updates_markdown(updates)
            except Exception as exc:
                completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with _db() as conn:
                    conn.execute(
                        "UPDATE summary_jobs SET status = ?, error = ?, completed_at = ? WHERE id = ?",
                        ("failed", str(exc), completed_at, job_id),
                    )
                yield f"data: {json.dumps({'error':f'Summary failed: {exc}','done':True})}\n\n"
                return

            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with _db() as conn:
                for inc in updates:
                    _upsert_incident(conn, inc, completed_at)
                _set_state(conn, "last_summarized_tx_id", str(to_tx_id))
                conn.execute(
                    "UPDATE summary_jobs SET status = ?, output = ?, completed_at = ? WHERE id = ?",
                    ("succeeded", markdown, completed_at, job_id),
                )
            with open(LOG_FILE, "a") as f:
                f.write(f"\n{SUMMARY_MARKER} {completed_at}\n{markdown}\n{'='*40}\n\n")
            yield f"data: {json.dumps({'text':markdown})}\n\n"
            yield f"data: {json.dumps({'done':True,'time':completed_at})}\n\n"

        return StreamingResponse(stream_incremental_summary(), media_type="text/event-stream",
                                 headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

    note_section = f"\nOperator note: {req.note}\n" if req.note else ""
    incident_context = incident_board_context(parse_log())
    mode_section = (
        "This is a FULL LOG SUMMARY covering the entire session from the beginning. "
        "Cover every incident — dispatches, responses, closures, and anything still open. "
        "For each incident show its full arc: when it was called, who responded, current status. "
        "Be comprehensive; do not skip incidents because they resolved. "
        "Reconcile the full log against the existing numbered incident board below."
    ) if req.full else (
        "This is an INCREMENTAL UPDATE covering only the radio traffic below. "
        "Output only incidents directly mentioned or updated by this new traffic, plus genuinely new incidents. "
        "Do not restate existing incidents unless the new traffic changes their status, location, details, or action. "
        "Keep Details to no more than two short sentences per incident."
    )
    prompt = "" if req.full else PROMPT_TEMPLATE.format(
        note_section=note_section,
        mode_section=mode_section,
        incident_context=incident_context,
        block="\n".join(lines),
    )
    max_tokens   = 16000 if req.full else 8192

    async def stream_summary() -> AsyncGenerator[str, None]:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            msg = "ANTHROPIC_API_KEY is not set for p25-server.service."
            yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
            return

        client = anthropic.AsyncAnthropic()
        full   = ""
        try:
            request_prompt = prompt
            if req.full:
                chunks = _chunk_lines(lines)
                partials = []
                for idx, chunk_lines in enumerate(chunks, start=1):
                    notice = f"\n\n[processing full-summary chunk {idx}/{len(chunks)}]\n"
                    yield f"data: {json.dumps({'text':notice})}\n\n"
                    chunk_prompt = FULL_CHUNK_TEMPLATE.format(
                        incident_context=incident_context,
                        chunk_num=idx,
                        chunk_count=len(chunks),
                        block="\n".join(chunk_lines),
                    )
                    chunk_task = asyncio.create_task(
                        _anthropic_text(client, chunk_prompt, max_tokens=3000, use_search=False)
                    )
                    while not chunk_task.done():
                        await asyncio.sleep(15)
                        yield ": keepalive\n\n"
                    partial = chunk_task.result()
                    partials.append(f"## Chunk {idx}/{len(chunks)}\n{partial.strip()}")
                    if idx < len(chunks):
                        yield f"data: {json.dumps({'text':'[waiting for rate-limit window]\\n'})}\n\n"
                        for _ in range(5):
                            await asyncio.sleep(13)
                            yield ": keepalive\n\n"

                yield f"data: {json.dumps({'text':'\\n[consolidating full summary]\\n'})}\n\n"
                if len(chunks) > 1:
                    for _ in range(5):
                        await asyncio.sleep(13)
                        yield ": keepalive\n\n"
                request_prompt = FULL_CONSOLIDATE_TEMPLATE.format(
                    incident_context=incident_context,
                    block="\n\n".join(partials),
                )

            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=max_tokens,
                tools=[{"type": "web_search_20260209", "name": "web_search"}],
                messages=[{"role":"user","content":request_prompt}],
            ) as s:
                display_started = False
                display_buffer = ""
                async for chunk in s.text_stream:
                    full += chunk
                    if display_started:
                        yield f"data: {json.dumps({'text':chunk})}\n\n"
                    else:
                        display_buffer += chunk
                        match = SUMMARY_HAS_INCIDENT_RE.search(display_buffer)
                        if match:
                            display_started = True
                            yield f"data: {json.dumps({'text':display_buffer[match.start():]})}\n\n"
                message = await s.get_final_message()
                if message.stop_reason == "max_tokens":
                    msg = "Summary failed: Claude hit max_tokens before finishing, so nothing was written to the log. Try again."
                    yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
                    return
        except Exception as exc:
            msg = f"Summary failed: {exc}"
            yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
            return

        full = _strip_summary_preamble(full)
        if not SUMMARY_HAS_INCIDENT_RE.search(full):
            msg = "Summary failed: Claude did not return incident sections, so nothing was written to the log. Try again."
            yield f"data: {json.dumps({'error':msg,'done':True})}\n\n"
            return

        ts     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        marker = FULL_SUMMARY_MARKER if req.full else SUMMARY_MARKER
        with open(LOG_FILE, "a") as f:
            f.write(f"\n{marker} {ts}\n{full.strip()}\n{'='*40}\n\n")
        yield f"data: {json.dumps({'done':True,'time':ts})}\n\n"

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
