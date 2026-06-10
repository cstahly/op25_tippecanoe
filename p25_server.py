#!/usr/bin/env python3
"""
P25 web app backend.
  uvicorn p25_server:app --host 0.0.0.0 --port 8765
Auth: P25_USER / P25_PASSWORD env vars (defaults: p25 / scanner)
"""
import base64, hashlib, hmac, html as _html, math, os, re, sys, json, asyncio, secrets, time, sqlite3, urllib.request, urllib.parse
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import AsyncGenerator, Set

import anthropic
from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LOG_FILE         = Path.home() / "op25_tippecanoe/p25_log.txt"
STATIC           = Path(__file__).parent / "static"
AUDIO_FIFO       = "/tmp/p25_audio.fifo"
DB_FILE          = Path.home() / "op25_tippecanoe/p25_state.db"
AUDIO_CLIPS_DIR  = Path.home() / "op25_tippecanoe/audio_clips"

MYCASE_PROXY_BASE = os.environ.get("MYCASE_PROXY_BASE", "https://p25.sadbabyrabbit.com")
USERNAME = os.environ.get("P25_USER", "p25")
PASSWORD = os.environ.get("P25_PASSWORD", "scanner")
SUMMARY_MARKER = "=== SUMMARY ==="
DEFAULT_SUMMARY_LIMIT = 0
DEFAULT_SHARE_TOKEN_SECONDS = 14 * 24 * 60 * 60
RATE_LIMITS: dict[str, float] = {}
TOKEN_SECRET = os.environ.get("P25_TOKEN_SECRET", "")
STALE_INCIDENT_SECONDS = int(os.environ.get("P25_STALE_INCIDENT_SECONDS", str(4 * 60 * 60)))
AUTO_SUMMARY_INTERVAL  = int(os.environ.get("P25_AUTO_SUMMARY_INTERVAL", str(2 * 60 * 60)))  # seconds

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

_PHOTON_URL   = "https://photon.komoot.io/api/"
_PHOTON_BIAS  = {"lat": "40.4167", "lon": "-86.8753"}  # Lafayette, IN
_PHOTON_BBOX  = "-87.8,39.8,-86.3,40.9"               # lon_min,lat_min,lon_max,lat_max
_GOOGLE_URL   = "https://maps.googleapis.com/maps/api/geocode/json"
_GOOGLE_BOUNDS = "39.8,-87.8|40.9,-86.3"              # sw_lat,sw_lng|ne_lat,ne_lng bias
GOOGLE_MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")
_GEOCODE_CENTER = (40.4167, -86.8753)
_GEOCODE_MAX_KM = 150
_GEOCODE_SKIP = frozenset({"unknown", "", "n/a", "none", "n/a."})
_geocode_failed: set[str] = set()

_GEOCODE_CITY_HINTS = (
    "lafayette", "west lafayette", "tippecanoe", "indiana", ", in",
    "battle ground", "dayton", "shadeland", "otterbein", "west point",
    "white county", "carroll county", "clinton county",
)

def _enrich_address(address: str) -> str:
    lower = address.lower()
    if any(h in lower for h in _GEOCODE_CITY_HINTS):
        return address
    return f"{address}, Lafayette, IN"

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))

def _within_range(lat: float, lng: float) -> bool:
    return _haversine_km(lat, lng, *_GEOCODE_CENTER) <= _GEOCODE_MAX_KM

def _cache_geocode(norm: str, lat: float, lng: float):
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO geocode_cache(address, lat, lng, cached_at) VALUES(?, ?, ?, ?)",
            (norm, lat, lng, datetime.now().isoformat()),
        )

async def _geocode_photon(query: str) -> tuple[float, float] | None:
    params = urllib.parse.urlencode({"q": query, "limit": "3", "bbox": _PHOTON_BBOX, **_PHOTON_BIAS})
    req = urllib.request.Request(f"{_PHOTON_URL}?{params}", headers={"User-Agent": "P25Monitor/1.0"})
    def _fetch():
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    for feature in data.get("features", []):
        coords = feature["geometry"]["coordinates"]
        lat, lng = float(coords[1]), float(coords[0])
        if _within_range(lat, lng):
            return lat, lng
    return None

async def _geocode_google(query: str) -> tuple[float, float] | None:
    if not GOOGLE_MAPS_KEY:
        return None
    params = urllib.parse.urlencode({"address": query, "bounds": _GOOGLE_BOUNDS, "key": GOOGLE_MAPS_KEY})
    req = urllib.request.Request(f"{_GOOGLE_URL}?{params}", headers={"User-Agent": "P25Monitor/1.0"})
    def _fetch():
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    data = await asyncio.get_event_loop().run_in_executor(None, _fetch)
    for result in data.get("results", []):
        loc = result["geometry"]["location"]
        lat, lng = float(loc["lat"]), float(loc["lng"])
        if _within_range(lat, lng):
            return lat, lng
    return None

async def _geocode_one(address: str) -> tuple[float, float] | None:
    """Geocode via Photon, falling back to Google if Photon finds nothing."""
    norm = address.strip()
    if not norm or norm.lower() in _GEOCODE_SKIP or norm in _geocode_failed:
        return None
    with _db() as conn:
        row = conn.execute("SELECT lat, lng FROM geocode_cache WHERE address = ?", (norm,)).fetchone()
        if row:
            return row["lat"], row["lng"]
    query = _enrich_address(norm)
    try:
        result = await _geocode_photon(query)
        if result is None:
            result = await _geocode_google(query)
        if result:
            _cache_geocode(norm, *result)
            return result
        _geocode_failed.add(norm)
        return None
    except Exception as exc:
        sys.stderr.write(f"[geocode] {norm!r}: {exc}\n")
        return None

async def _geocode_worker():
    """Background task: geocode all uncached incident locations."""
    await asyncio.sleep(3)
    # Purge cached results that landed outside the Lafayette area (wrong-state results)
    try:
        clat, clng = _GEOCODE_CENTER
        with _db() as conn:
            bad = conn.execute(
                "SELECT address, lat, lng FROM geocode_cache"
            ).fetchall()
            purge = [r["address"] for r in bad
                     if _haversine_km(r["lat"], r["lng"], clat, clng) > _GEOCODE_MAX_KM]
            if purge:
                conn.executemany("DELETE FROM geocode_cache WHERE address = ?", [(a,) for a in purge])
                sys.stderr.write(f"[geocode] purged {len(purge)} out-of-range cache entries\n")
    except Exception as exc:
        sys.stderr.write(f"[geocode worker purge] {exc}\n")

    while True:
        try:
            with _db() as conn:
                rows = conn.execute("""
                    SELECT DISTINCT i.location FROM incident_state i
                    LEFT JOIN geocode_cache g ON i.location = g.address
                    WHERE g.address IS NULL
                      AND lower(i.location) NOT IN ('unknown','','n/a','none','n/a.')
                    ORDER BY i.number DESC
                """).fetchall()
            pending = [r["location"] for r in rows if r["location"] not in _geocode_failed]
            if pending:
                sys.stderr.write(f"[geocode] {len(pending)} addresses to geocode\n")
            for loc in pending:
                result = await _geocode_one(loc)
                if result:
                    sys.stderr.write(f"[geocode] ok: {loc!r} → {result[0]:.4f},{result[1]:.4f}\n")
                await asyncio.sleep(0.3)
            next_sleep = 60 if not pending else 10
        except Exception as exc:
            sys.stderr.write(f"[geocode worker] {exc}\n")
            next_sleep = 30
        await asyncio.sleep(next_sleep)

async def _auto_summary_worker():
    """Run an incremental Haiku summary every AUTO_SUMMARY_INTERVAL seconds."""
    await asyncio.sleep(60)  # let the server settle before first run
    while True:
        await asyncio.sleep(AUTO_SUMMARY_INTERVAL)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            continue
        try:
            ensure_state_ready()
            with _db() as conn:
                from_tx_id = int(_get_state(conn, "last_summarized_tx_id", "0") or "0")
                tx_rows = conn.execute(
                    "SELECT * FROM transmissions WHERE id > ? ORDER BY id",
                    (from_tx_id,),
                ).fetchall()
                if not tx_rows:
                    continue
                to_tx_id = tx_rows[-1]["id"]
                lines = [row["raw_line"] for row in tx_rows]
                current_incidents = incident_rows_from_db(conn)
                incident_context = incident_board_context_from_incidents(current_incidents)

            prompt = INCREMENTAL_JSON_TEMPLATE.format(
                incident_context=incident_context,
                block="\n".join(lines),
            )
            client = anthropic.AsyncAnthropic()
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            if message.stop_reason == "max_tokens":
                sys.stderr.write("[auto-summary] hit max_tokens — skipping write\n")
                continue
            raw = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
            updates = _validate_incident_updates(_parse_json_object(raw))
            markdown = _incident_updates_markdown(updates)
            completed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with _db() as conn:
                for inc in updates:
                    _upsert_incident(conn, inc, completed_at, first_tx_id=from_tx_id + 1, last_tx_id=to_tx_id)
                _set_state(conn, "last_summarized_tx_id", str(to_tx_id))
                conn.execute(
                    "INSERT INTO summary_jobs(mode, from_tx_id, to_tx_id, status, output, created_at, completed_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    ("auto", from_tx_id + 1, to_tx_id, "succeeded", markdown, completed_at, completed_at),
                )
            with open(LOG_FILE, "a") as f:
                f.write(f"\n{SUMMARY_MARKER} {completed_at}\n{markdown}\n{'='*40}\n\n")
            sys.stderr.write(f"[auto-summary] {completed_at}: processed {len(tx_rows)} lines, {len(updates)} incident updates\n")
        except Exception as exc:
            sys.stderr.write(f"[auto-summary] error: {exc}\n")

@app.on_event("startup")
async def _startup():
    ensure_state_ready()
    asyncio.create_task(_audio_broadcast_loop())
    asyncio.create_task(_geocode_worker())
    asyncio.create_task(_auto_summary_worker())

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

TX_RE         = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\] \[(.+?)\](?:\s+\[trunk:(tippecanoe|safet)\])?(?:\s+\[clip:([^\]]+)\])? (.+)$')
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
    if any(x in t for x in ("LPD","WLPD","TCSD","PUPD","ISP")): return "police"
    if any(x in t for x in ("LFD","WLFD","TCFD","PUFD")):        return "fire"
    if any(x in t for x in ("EMS","TEAS")):                       return "ems"
    return "other"

_SAFE_T_KEYWORDS = frozenset(["ISP", "BENTON", "CARROLL", "DELPHI", "CLINTON", "INDOT"])

def _trunk(tg: str) -> str:
    upper = tg.upper()
    if any(k in upper for k in _SAFE_T_KEYWORDS):
        return "safet"
    m = re.search(r'(?:^|\()(\d{5,})\)?$', tg.strip())
    if m and int(m.group(1)) >= 10000:
        return "safet"
    return "tippecanoe"

def parse_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    lines = LOG_FILE.read_text(errors="replace").splitlines()
    entries, i, tx_count = [], 0, 1
    while i < len(lines):
        line = lines[i]
        m = TX_RE.match(line)
        if m:
            tg = m.group(2)
            tx_entry: dict = {"type":"tx","id":f"tx-{tx_count}",
                            "time":m.group(1),"talkgroup":tg,
                            "agency":_agency(tg),"trunk":m.group(3) or _trunk(tg),"text":m.group(5)}
            if m.group(4) and (AUDIO_CLIPS_DIR / m.group(4)).exists():
                tx_entry["wav_file"] = m.group(4)
            entries.append(tx_entry)
            tx_count += 1
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
                if key not in ("agency", "status", "location", "time", "priority"):
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
            "priority": max(1, min(5, int(fields.get("priority", 3) or 3))),
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

    fresh_open = [
        inc for inc in numbered
        if inc.get("status_kind") != "clear" and not inc.get("is_stale")
    ]
    stale_open = [
        inc for inc in numbered
        if inc.get("status_kind") != "clear" and inc.get("is_stale")
    ]
    recent_closed = [
        inc for inc in numbered
        if inc.get("status_kind") == "clear"
    ]

    fresh_open.sort(key=lambda inc: (str(inc.get("last_seen", "")), int(inc["number"])), reverse=True)
    stale_open.sort(key=lambda inc: (str(inc.get("last_seen", "")), int(inc["number"])), reverse=True)
    recent_closed.sort(key=lambda inc: (str(inc.get("last_seen", "")), int(inc["number"])), reverse=True)

    selected = fresh_open[:60] + stale_open[:15] + recent_closed[:10]
    selected.sort(key=lambda inc: int(inc["number"]))

    lines = [
        "Existing incident board subset. Reuse these numbers for matching. "
        f"Showing {len(selected)} of {len(numbered)} incidents: fresh open items, some stale open items, and recently cleared items. "
        f"Open incidents older than {STALE_INCIDENT_SECONDS // 3600}h are marked STALE."
    ]
    for inc in selected:
        location = inc.get("location") or "Unknown"
        status = inc.get("status") or "WATCH"
        title = inc.get("title") or "Incident"
        stale = " | STALE" if inc.get("is_stale") else ""
        priority = inc.get("priority") or 3
        lines.append(f"{inc['number']}. {status}{stale} | P{priority} | {title} | {location}")
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
            updated_at TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 3
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
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS geocode_cache (
            address TEXT PRIMARY KEY,
            lat     REAL NOT NULL,
            lng     REAL NOT NULL,
            cached_at TEXT NOT NULL
        );
        """)
        try:
            conn.execute("ALTER TABLE transmissions ADD COLUMN wav_file TEXT")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE incident_state ADD COLUMN first_tx_id INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE incident_state ADD COLUMN last_tx_id INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE incident_state ADD COLUMN priority INTEGER NOT NULL DEFAULT 3")
        except Exception:
            pass  # column already exists
        # Backfill first_tx_id for incidents created before this column existed.
        # Find the earliest summary job whose output mentions each incident number.
        unfilled = conn.execute("SELECT number FROM incident_state WHERE first_tx_id = 0").fetchall()
        for row in unfilled:
            number = row["number"]
            job = conn.execute(
                "SELECT from_tx_id FROM summary_jobs WHERE status = 'succeeded' AND output LIKE ? ORDER BY id ASC LIMIT 1",
                (f"% INCIDENT {number}:%",),
            ).fetchone()
            if job:
                conn.execute(
                    "UPDATE incident_state SET first_tx_id = ? WHERE number = ? AND first_tx_id = 0",
                    (int(job["from_tx_id"]) + 1, number),
                )
        # Backfill last_tx_id from the most recent summary job that mentions each incident.
        unfilled_last = conn.execute("SELECT number FROM incident_state WHERE last_tx_id = 0").fetchall()
        for row in unfilled_last:
            number = row["number"]
            job = conn.execute(
                "SELECT to_tx_id FROM summary_jobs WHERE status = 'succeeded' AND output LIKE ? ORDER BY id DESC LIMIT 1",
                (f"% INCIDENT {number}:%",),
            ).fetchone()
            if job:
                conn.execute(
                    "UPDATE incident_state SET last_tx_id = ? WHERE number = ? AND last_tx_id = 0",
                    (int(job["to_tx_id"]), number),
                )

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
            rows.append((tx_id, m.group(1), tg, _agency(tg), m.group(5), m.group(4), line))
    conn.execute("DELETE FROM transmissions")
    conn.executemany(
        "INSERT INTO transmissions(id, time, talkgroup, agency, text, wav_file, raw_line) VALUES(?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)

def incident_rows_from_db(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT i.*, g.lat AS lat, g.lng AS lng FROM incident_state i "
        "LEFT JOIN geocode_cache g ON i.location = g.address "
        "ORDER BY "
        "CASE i.status_kind WHEN 'active' THEN 0 WHEN 'watch' THEN 1 WHEN 'routine' THEN 2 WHEN 'clear' THEN 3 ELSE 1 END, "
        "i.priority ASC, "
        "i.last_seen DESC"
    ).fetchall()
    return [_incident_row_to_api(row) for row in rows]

def _parse_dt(text: str) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if fmt == "%H:%M:%S":
                today = datetime.now()
                dt = dt.replace(year=today.year, month=today.month, day=today.day)
            return dt
        except ValueError:
            pass
    return None

def _incident_age_seconds(last_seen: str) -> int | None:
    dt = _parse_dt(last_seen)
    if not dt:
        return None
    age = int((datetime.now() - dt).total_seconds())
    return max(0, age)

def _incident_is_stale(row_or_inc) -> bool:
    if (row_or_inc["status_kind"] if isinstance(row_or_inc, sqlite3.Row) else row_or_inc.get("status_kind")) == "clear":
        return False
    last_seen = row_or_inc["last_seen"] if isinstance(row_or_inc, sqlite3.Row) else row_or_inc.get("last_seen", "")
    age = _incident_age_seconds(last_seen)
    return age is not None and age > STALE_INCIDENT_SECONDS

def _auto_clear_stale_incidents(conn: sqlite3.Connection) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT number, last_seen, status_kind FROM incident_state WHERE status_kind != 'clear'"
    ).fetchall()
    cleared = 0
    for row in rows:
        if _incident_is_stale(row):
            conn.execute(
                "UPDATE incident_state SET status = 'CLEAR', status_kind = 'clear', updated_at = ? WHERE number = ?",
                (now, row["number"]),
            )
            cleared += 1
    return cleared

def _incident_row_to_api(row: sqlite3.Row) -> dict:
    details = json.loads(row["details_json"] or "[]")
    age_seconds = _incident_age_seconds(row["last_seen"])
    is_stale = _incident_is_stale(row)
    try:
        lat = row["lat"]
        lng = row["lng"]
    except (IndexError, KeyError):
        lat = lng = None
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
        "first_tx_id": int(row["first_tx_id"]) if row["first_tx_id"] else 0,
        "last_tx_id": int(row["last_tx_id"]) if row["last_tx_id"] else 0,
        "updates": 1,
        "age_seconds": age_seconds,
        "is_stale": is_stale,
        "lat": lat,
        "lng": lng,
        "priority": int(row["priority"]) if row["priority"] is not None else 3,
    }

def _incident_by_number(conn: sqlite3.Connection, number: int) -> dict | None:
    row = conn.execute(
        "SELECT i.*, g.lat AS lat, g.lng AS lng FROM incident_state i "
        "LEFT JOIN geocode_cache g ON i.location = g.address "
        "WHERE i.number = ?", (number,)
    ).fetchone()
    return _incident_row_to_api(row) if row else None

def _upsert_incident(conn: sqlite3.Connection, inc: dict, now: str, first_tx_id: int = 0, last_tx_id: int = 0) -> None:
    number = int(inc["number"])
    details = inc.get("details") or []
    if isinstance(details, str):
        details = [details]
    title = str(inc.get("title") or "Incident").strip()
    status = str(inc.get("status") or "WATCH").strip()
    blob = "\n".join([title, status, *(str(d) for d in details)])
    priority = max(1, min(5, int(inc.get("priority") or 3)))
    row = conn.execute("SELECT first_seen, first_tx_id, last_tx_id FROM incident_state WHERE number = ?", (number,)).fetchone()
    first_seen = row["first_seen"] if row else now
    stored_first_tx_id = int(row["first_tx_id"]) if row and row["first_tx_id"] else 0
    stored_last_tx_id  = int(row["last_tx_id"])  if row and row["last_tx_id"]  else 0
    effective_first_tx_id = stored_first_tx_id if stored_first_tx_id else first_tx_id
    effective_last_tx_id  = max(stored_last_tx_id, last_tx_id)
    conn.execute(
        """
        INSERT INTO incident_state
            (number, title, agency, status, status_kind, location, details_json, action, first_seen, last_seen, updated_at, first_tx_id, last_tx_id, priority)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(number) DO UPDATE SET
            title = excluded.title,
            agency = excluded.agency,
            status = excluded.status,
            status_kind = excluded.status_kind,
            location = excluded.location,
            details_json = excluded.details_json,
            action = excluded.action,
            last_seen = excluded.last_seen,
            updated_at = excluded.updated_at,
            last_tx_id = excluded.last_tx_id,
            priority = excluded.priority
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
            effective_first_tx_id,
            effective_last_tx_id,
            priority,
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

@app.get("/api/transmissions", dependencies=[Depends(require_auth)])
def get_transmissions(limit: int = 50, before_id: int = 0, after_id: int = 0, from_id: int = 0, to_id: int = 0):
    limit = max(1, min(limit, 500))
    with _db() as conn:
        if from_id and to_id:
            rows = conn.execute(
                "SELECT * FROM transmissions WHERE id >= ? AND id <= ? ORDER BY id ASC",
                (from_id, to_id),
            ).fetchall()
        elif before_id:
            rows = conn.execute(
                "SELECT * FROM transmissions WHERE id < ? ORDER BY id DESC LIMIT ?",
                (before_id, limit),
            ).fetchall()
        elif after_id:
            rows = conn.execute(
                "SELECT * FROM transmissions WHERE id > ? ORDER BY id DESC LIMIT ?",
                (after_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transmissions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    _trunk_re = re.compile(r"\[trunk:(\w+)\]")
    result = []
    for row in rows:
        raw = row["raw_line"] or ""
        m = _trunk_re.search(raw)
        trunk = m.group(1) if m else None
        entry: dict = {
            "id": row["id"],
            "time": row["time"],
            "talkgroup": row["talkgroup"],
            "agency": row["agency"],
            "trunk": trunk,
            "text": row["text"],
        }
        if row["wav_file"] and (AUDIO_CLIPS_DIR / row["wav_file"]).exists():
            entry["wav_file"] = row["wav_file"]
        result.append(entry)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})

_PUBLIC_CORS = {"Cache-Control": "public, max-age=30", "Access-Control-Allow-Origin": "*"}

@app.get("/api/public")
def public_state():
    """No-auth, read-only snapshot of current incidents + recent radio traffic."""
    with _db() as conn:
        incidents = incident_rows_from_db(conn)
        rows = conn.execute(
            "SELECT id, time, talkgroup, agency, raw_line, text, wav_file FROM transmissions ORDER BY id DESC LIMIT 30"
        ).fetchall()
    _trunk_re2 = re.compile(r"\[trunk:(\w+)\]")
    tx = []
    for row in rows:
        m = _trunk_re2.search(row["raw_line"] or "")
        wav = row["wav_file"] if row["wav_file"] and (AUDIO_CLIPS_DIR / row["wav_file"]).exists() else None
        tx.append({"id": row["id"], "time": row["time"], "talkgroup": row["talkgroup"],
                   "agency": row["agency"], "trunk": m.group(1) if m else None,
                   "text": row["text"], "wav_file": wav})
    # Radio is "live" if the log file was written to in the last 10 minutes.
    last_tx_ago = None
    radio_live = False
    if LOG_FILE.exists():
        last_tx_ago = round(time.time() - LOG_FILE.stat().st_mtime)
        radio_live = last_tx_ago < 600
    return JSONResponse({"incidents": incidents, "transmissions": tx,
                         "updated": datetime.now().isoformat(),
                         "radio_live": radio_live,
                         "last_tx_ago": round(last_tx_ago) if last_tx_ago is not None else None},
                        headers=_PUBLIC_CORS)

@app.get("/embed")
def embed_page():
    return FileResponse(STATIC / "embed.html", headers={"Cache-Control": "public, max-age=60"})

@app.get("/api/public/clip/{filename}")
def public_clip(filename: str):
    """No-auth audio clip serving for the public embed page."""
    safe = Path(filename).name
    path = AUDIO_CLIPS_DIR / safe
    if not path.exists() or not safe.endswith(".wav"):
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav",
                        headers={"Cache-Control": "public, max-age=3600",
                                 "Access-Control-Allow-Origin": "*"})

@app.get("/api/state", dependencies=[Depends(require_auth)])
def get_state():
    ensure_state_ready()
    stat = LOG_FILE.stat() if LOG_FILE.exists() else None
    entries = parse_log()
    with _db() as conn:
        incidents = incident_rows_from_db(conn)
    feed = entries[-250:]
    return JSONResponse({
        "entries": feed,
        "entries_latest": list(reversed(feed)),
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

AUDIO_FILTER_FILE = "/tmp/p25_audio_filter"

@app.get("/api/audio-filter", dependencies=[Depends(require_auth)])
def get_audio_filter():
    try:
        val = Path(AUDIO_FILTER_FILE).read_text().strip()
    except Exception:
        val = "all"
    return {"filter": val if val in ("all", "0", "1") else "all"}

class AudioFilterReq(BaseModel):
    filter: str

@app.post("/api/audio-filter", dependencies=[Depends(require_auth)])
def set_audio_filter(req: AudioFilterReq):
    if req.filter not in ("all", "0", "1"):
        raise HTTPException(400, detail="filter must be 'all', '0', or '1'")
    Path(AUDIO_FILTER_FILE).write_text(req.filter)
    return {"filter": req.filter}

def _mycase_case_url(result: dict) -> str:
    token = result.get("CaseToken", "")
    payload = json.dumps({"v": {"CaseToken": token}}, separators=(",", ":"))
    b64 = base64.b64encode(payload.encode()).decode()
    return f"https://public.courts.in.gov/mycase/#/vw/CaseSummary/{b64}"

_MYCASE_CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'"

def _mycase_results_html(first: str, last: str, results: list, total: int) -> str:
    h = _html.escape
    name = h(f"{first} {last}".strip())
    rows = ""
    for r in results:
        url = h(_mycase_case_url(r), quote=True)
        num = h(r.get("CaseNumber", ""))
        style = h(r.get("Style", ""))
        charges = h(r.get("Charges") or "")
        rows += f'<li><a href="{url}">{num}</a> &mdash; {style}' + (f' <small>({charges})</small>' if charges else '') + '</li>\n'
    summary = h(f"{total} case{'s' if total != 1 else ''} found" if results else "No cases found")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MyCase: {name}</title>
<style>
body{{font-family:system-ui,-apple-system,sans-serif;padding:1.2rem;max-width:640px;margin:auto;color:#1a1a1a}}
h2{{font-size:1.1rem;margin-bottom:.4rem}}
p{{color:#555;font-size:.9rem;margin:.3rem 0}}
ul{{padding-left:1.2rem;margin-top:.8rem}}
li{{margin:.5rem 0;line-height:1.4}}
a{{color:#0057b8;text-decoration:none}}
a:hover{{text-decoration:underline}}
small{{color:#666}}
</style>
</head><body>
<h2>MyCase: {name}</h2>
<p>{summary}</p>
<ul>{rows}</ul>
<p style="margin-top:1rem;font-size:.8rem">
<a href="https://public.courts.in.gov/mycase/#/qs/Search">Open MyCase Search</a>
</p>
</body></html>"""

@app.get("/api/mycase")
def mycase_search(first: str = "", last: str = ""):
    first = first.strip(); last = last.strip()
    if not first and not last:
        raise HTTPException(400, detail="Provide first= and/or last= query params")
    payload = json.dumps({
        "Mode": "ByParty", "Last": last, "First": first,
        "NewSearch": True, "CaptchaAnswer": None,
        "Skip": 0, "Take": 10, "Sort": "CaseNumber ASC",
    }).encode()
    req = urllib.request.Request(
        "https://public.courts.in.gov/mycase/Search/SearchCases",
        data=payload,
        headers={"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        return HTMLResponse(
            f"<h2>MyCase lookup failed</h2><p>{_html.escape(str(exc))}</p>",
            status_code=502,
            headers={"Content-Security-Policy": _MYCASE_CSP},
        )
    results = data.get("Results") or []
    total = data.get("TotalResults", 0)
    if total == 1 and results:
        return RedirectResponse(url=_mycase_case_url(results[0]), status_code=303)
    return HTMLResponse(
        _mycase_results_html(first, last, results, total),
        headers={"Content-Security-Policy": _MYCASE_CSP},
    )


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
                                       "trunk":m.group(3) or _trunk(tg),"text":m.group(5)}
                            if m.group(4) and (AUDIO_CLIPS_DIR / m.group(4)).exists():
                                payload["wav_file"] = m.group(4)
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

Indiana SAFE-T talkgroups (Indiana State Police District 14 — Lafayette, covers Tippecanoe and surrounding counties): \
ISP LAF DISP (10748, dispatch), ISP LAF OPS1/2/3 (10749/10750/10751, ops channels), \
ISP LAF ATG (10747, multigroup). INDOT CRW MAIN/ENG/EVENT (10558/10559/10560, INDOT Crawfordsville district roads).

10-codes: 10-4=ack, 10-7=OOS, 10-8=in service, 10-20=location, 10-22=disregard, \
10-23=arrived, 10-27=DL check, 10-28=registration, 10-29=warrants, 10-33=emergency, \
10-50=accident, 10-52=ambulance needed, 10-55=DUI, 10-57=hit and run, \
10-78=need assistance, 10-79=notify coroner. Signal 1=en route, Signal 4=arrived, Code 3=L&S.
{note_section}
{mode_section}
{incident_context}

Radio traffic (format [HH:MM:SS] [TALKGROUP] [trunk:tippecanoe|safet] transcript).
Trunk "tippecanoe" = Tippecanoe County P25 system. Trunk "safet" = Indiana SAFE-T (ISP/INDOT).
Treat transmissions from different trunks as separate but potentially related systems.
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
- Priority: 1-5 urgency (1=critical/life-threatening/mass casualty, 2=serious/major incident, 3=moderate/standard, 4=non-urgent response, 5=routine traffic stop/minor call). Default is 3. Update if new traffic changes severity. Use the existing board value if no change.
- Action: what remains unresolved or what to watch for. REQUIRED: if a person's name (suspect, subject, driver, wanted person) was mentioned in the traffic, append a MyCase link using the format [MyCase: Firstname Lastname](https://p25.sadbabyrabbit.com/api/mycase?first=Firstname&last=Lastname) — substitute the real name in both the link text and query params.

Incident numbers are persistent identifiers. Use only integers, never letters. \
Reuse an existing incident number when updating the same real-world incident. \
Do not renumber incidents. For a new incident, use the next unused integer after the highest existing incident number.

Put any local context, landmark explanation, uncertainty, or secondary locations in Details, not Location. \
Location is used directly as a map link label and query, so keep it clean and exact.

CRITICAL — incident clearance: You MUST actively clear resolved incidents. \
An incident should be marked CLEAR when: units return to service (10-8, available, in-service, back in service), \
dispatch says disregard (10-22), scene is cleared, transport completed, or no follow-up activity suggests resolution. \
Do NOT leave an incident ACTIVE just because it was active before — review the full log and mark it CLEAR if resolved. \
Any incident in the existing board marked STALE (no radio traffic in 4+ hours) should be marked CLEAR unless \
there is explicit ongoing activity in the log. A single brief mention with no follow-up should be CLEAR, not ACTIVE. \
Incidents that are minor and short-duration (traffic stops, minor accidents, medical assists, welfare checks) \
resolve within minutes and should be CLEAR unless you see continued traffic. \
Use stable incident titles when an older incident is still being updated. \
Be direct and concise."""

FULL_CHUNK_TEMPLATE = """\
You are reviewing one chunk of Tippecanoe County public safety radio traffic.

{incident_context}

This is chunk {chunk_num} of {chunk_count}. Extract incident facts from this chunk only. \
Preserve existing incident numbers when the traffic clearly belongs to one. For new incidents, \
label them NEW in this chunk summary; do not assign final numbers here.
Output only incident sections. Do not include preamble, analysis narration, or search narration.

Radio traffic (format [HH:MM:SS] [TALKGROUP] [trunk:tippecanoe|safet] transcript).
Trunk "tippecanoe" = Tippecanoe County P25. Trunk "safet" = Indiana SAFE-T (ISP/INDOT).
{block}

Return concise markdown sections:
### INCIDENT 12 or NEW: Short title
- Agency:
- Status:
- Location: pure mappable address/place only, or Unknown
- Details:
- Priority: 1-5 urgency (1=critical/life-threatening, 5=routine)
- Action:
"""

FULL_CONSOLIDATE_TEMPLATE = """\
You are creating the final full-session incident summary for Tippecanoe County public safety radio traffic.

{incident_context}

Consolidate the chunk summaries below into one current incident list. Incident numbers are persistent \
identifiers. Use only integers, never letters. Reuse existing incident numbers for the same real-world \
incident. Do not renumber incidents. Assign new incidents the next unused integer after the highest \
existing incident number.

CRITICAL — incident clearance: Actively review every incident in the existing board. \
Mark incidents CLEAR when: units return to service (10-8, available), dispatch says disregard (10-22), \
scene cleared, transport completed, or log shows no follow-up activity. \
A brief mention with no callback or follow-up = CLEAR. Short-duration calls (stops, minor accidents, \
medical assists, welfare checks) resolve in minutes unless the log shows otherwise.

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
- Priority: 1-5 urgency (1=critical/life-threatening, 2=serious, 3=moderate, 4=non-urgent, 5=routine)
- Action: what remains unresolved or what to watch for. REQUIRED: if a person's name (suspect, subject, driver, wanted person) was mentioned, append a MyCase link using the format [MyCase: Firstname Lastname](https://p25.sadbabyrabbit.com/api/mycase?first=Firstname&last=Lastname) — substitute the real name in both the link text and query params.

Chunk summaries:
{block}
"""

INCREMENTAL_JSON_TEMPLATE = """\
You update a live incident board for Tippecanoe County public safety radio traffic.

Existing incident board:
{incident_context}

New transmissions since the last successful summary (format [HH:MM:SS] [TALKGROUP] [trunk:tippecanoe|safet] transcript).
Trunk "tippecanoe" = Tippecanoe County P25. Trunk "safet" = Indiana SAFE-T (ISP/INDOT).
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
      "priority": 3,
      "action": "what remains unresolved or what to watch for. If a person's name (suspect, subject, driver, wanted person) was mentioned, append a MyCase link: [MyCase: Firstname Lastname](https://p25.sadbabyrabbit.com/api/mycase?first=Firstname&last=Lastname) — substitute the real name in both label and query params."
    }}
  ]
}}

Rules:
- Include incidents directly mentioned or updated by the new transmissions.
- Reuse an existing incident number for the same real-world incident.
- For a new incident, assign the next unused integer after the highest existing incident number.
- Use only integer incident numbers, never letters.
- Do not include administrative traffic unless it changes an incident.
- Mark an incident CLEAR when: units return to service (10-8, available, in-service), dispatch says
  disregard (10-22), scene cleared, transport done, or there is no follow-up after initial dispatch.
  Short-duration calls (stops, minor accidents, medical assists, welfare checks) default to CLEAR unless
  the log shows ongoing activity.
- Location must be a pure mappable address/place only, or Unknown. Put uncertainty and context in details.
- Keep each incident concise: at most two details.
- priority is 1-5 urgency: 1=critical/life-threatening, 2=serious/major, 3=moderate (default), 4=non-urgent, 5=routine/minor. Use the existing board value (shown as P# in the board context) if unchanged. Update it if new traffic changes severity.
- IMPORTANT: When a person's name appears in the transmissions, you MUST include a MyCase link in the action field. Use the format [MyCase: Firstname Lastname](https://p25.sadbabyrabbit.com/api/mycase?first=Firstname&last=Lastname) with the real name in both the label and the query params.
- If there are no incident updates, return {{"incidents":[]}}.
"""

class SummarizeReq(BaseModel):
    note: str = ""
    full: bool = False
    expensive: bool = False
    fast: bool = False

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
            "priority": max(1, min(5, int(inc.get("priority") or 3))),
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
            f"- Priority: {inc.get('priority') or 3}\n"
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
    elif req.expensive:
        if auth["username"] != USERNAME:
            raise HTTPException(403, detail="Only the primary user can run an expensive summary")
        ensure_state_ready()
        with _db() as conn:
            from_tx_id = int(_get_state(conn, "last_summarized_tx_id", "0") or "0")
            tx_rows = conn.execute(
                "SELECT * FROM transmissions WHERE id > ? ORDER BY id",
                (from_tx_id,),
            ).fetchall()
            to_tx_id = tx_rows[-1]["id"] if tx_rows else from_tx_id
            lines = [row["raw_line"] for row in tx_rows]
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
            mode = "expensive" if req.expensive else ("fast" if req.fast else "incremental")
            with _db() as conn:
                _auto_clear_stale_incidents(conn)
                current_incidents = incident_rows_from_db(conn)
                incident_context = incident_board_context_from_incidents(current_incidents)
                cur = conn.execute(
                    "INSERT INTO summary_jobs(mode, from_tx_id, to_tx_id, status, created_at) VALUES(?, ?, ?, ?, ?)",
                    (mode, from_tx_id + 1, to_tx_id, "running", created_at),
                )
                job_id = cur.lastrowid

            prompt = INCREMENTAL_JSON_TEMPLATE.format(
                incident_context=incident_context,
                block="\n".join(lines),
            )
            if req.expensive:
                prompt = (
                    "Use web_search to silently look up any Lafayette/Tippecanoe address, business, "
                    "or landmark you are uncertain about. Do not narrate searches.\n"
                    "When a specific person's name (suspect, subject, driver, wanted person, etc.) is mentioned "
                    "in the radio traffic, include a pre-filled Indiana MyCase court records search link in the "
                    "action field using this exact format (substitute real names): "
                    "`[MyCase: Firstname Lastname](https://p25.sadbabyrabbit.com/api/mycase?first=Firstname&last=Lastname)` "
                    "— put the real first name in the first= param and last name in last=. "
                    "Only add links for specific named individuals, not generic descriptions.\n\n" + prompt
                )
            model = "claude-haiku-4-5-20251001" if req.fast else "claude-sonnet-4-6"
            try:
                client = anthropic.AsyncAnthropic()
                create_kwargs: dict = {
                    "model": model,
                    "max_tokens": 4096,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if req.expensive:
                    create_kwargs["tools"] = [{"type": "web_search_20260209", "name": "web_search"}]
                message = await client.messages.create(**create_kwargs)
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
                    _upsert_incident(conn, inc, completed_at, first_tx_id=from_tx_id + 1, last_tx_id=to_tx_id)
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

@app.get("/api/audio/clip/{filename}", dependencies=[Depends(require_auth)])
async def audio_clip(filename: str):
    if not re.fullmatch(r'[\w.-]+\.wav', filename):
        raise HTTPException(400, "Invalid filename")
    path = AUDIO_CLIPS_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Clip not found")
    return FileResponse(str(path), media_type="audio/wav",
                        headers={"Cache-Control": "max-age=86400"})

@app.get("/api/geocode", dependencies=[Depends(require_auth)])
async def geocode_address(q: str):
    norm = q.strip()
    if not norm or norm.lower() in _GEOCODE_SKIP:
        raise HTTPException(404, detail="No geocodable address")
    result = await _geocode_one(norm)
    if not result:
        raise HTTPException(404, detail="Address not found")
    return JSONResponse({"lat": result[0], "lng": result[1]})

app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("p25_server:app", host="0.0.0.0", port=8765, reload=False)
