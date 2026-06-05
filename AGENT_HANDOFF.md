# P25 System — Agent Handoff

Last updated: 2026-06-05 (session 2). Read this before doing anything. Hardware is a Kali
bare-metal box with `bypassPermissions` mode enabled in Claude Code. Full sudo. RDP access.

---

## 1. What's Running Right Now

| System | Status | Notes |
|--------|--------|-------|
| **P25 decoder (OP25)** | Operational | Decoding Tippecanoe County traffic |
| **Whisper STT** | Operational | `turbo` model, CPU, int8 |
| **p25_server.py (web app)** | Deployed on HTTPS | systemd + nginx + Let's Encrypt |
| **Satellite scheduler** | **PAUSED** | All rules `"enabled": false` |

`p25_summarize.py` (old CLI summarizer) is no longer the primary interface — summarization
is now entirely in the web app.

---

## 2. P25 System

### How to run OP25

```bash
cd ~/op25_tippecanoe
python3 ~/src/op25/op25/gr-op25_repeater/apps/multi_rx.py -v 1 -c tippecanoe.json 2>stderr.log
```

OP25 runs as a **terminal process** (not a systemd service). Kill and restart it to pick up
config changes (gains, ppm, etc.).

### Tippecanoe County P25 config

- **System**: P25 Phase I Trunked, CQPSK demod
- **WACN**: 0xBEE00 — **SysID**: 0x6FD
- **Control channels**: 851.050 / 853.8375 / 857.7375 MHz
- **HackRF gains**: `RF:14, IF:16, BB:32` (bumped BB from 20 to 32 this session for SNR)
- **PPM offset**: 0.0 — verified against NOAA WX at 162.400 MHz (carrier dead-on)
- **Audio UDP**: 127.0.0.1:23456 → default ALSA device
- **Config file**: `~/op25_tippecanoe/tippecanoe.json`

### Talkgroups (trunk-tags.tsv)

| TGID | Name | Agency |
|------|------|--------|
| 1813 | TCSD DISPATCH | Sheriff |
| 1827 | TCFD DISPATCH | County Fire |
| 1833 | TEAS EMS DISPATCH | EMS |
| 1901 | LFD DISPATCH | Lafayette Fire |
| 1931 | LPD DISPATCH | Lafayette PD |
| 2019 | WLPD DISPATCH | West Lafayette PD |
| 2119 | PUPD DISPATCH | Purdue PD |
| 2225 | TEAS OPS | EMS ops |

### Key files

| Path | Purpose |
|------|---------|
| `~/op25_tippecanoe/tippecanoe.json` | OP25 config (HackRF, trunking, audio) |
| `~/op25_tippecanoe/trunk-tags.tsv` | TGID→name map |
| `~/op25_tippecanoe/p25_log.txt` | Live transcript + summaries |
| `~/op25_tippecanoe/stderr.log` | OP25 stderr (TGID tracking) |
| `~/op25_tippecanoe/p25_server.py` | Web app backend (FastAPI, port 8765) |
| `~/op25_tippecanoe/static/index.html` | PWA frontend (v16) |
| `~/op25_tippecanoe/static/sw.js` | Service worker (p25-v16) |
| `~/src/op25/op25/gr-op25_repeater/apps/stt_audio.py` | Custom audio module |

### stt_audio.py internals

Drop-in for sockaudio.py. Intercepts OP25 UDP audio, buffers transmissions, on drain submits
to thread pool for Whisper transcription → appends to `p25_log.txt`.

- **Whisper**: `turbo` model, CPU, int8 — **NEVER use CUDA** (GPU is shared with RDP encoder)
- **Audio format**: 8000 Hz S16LE mono, upsampled 2x to 16000 Hz float32 for Whisper
- **TGID detection**: polls `stderr.log` for `tg(\d+)` after each TX
- **FIFO**: `/tmp/p25_audio.fifo` — writes PCM here for web audio streaming (added this session)
  - Opened with `O_RDWR|O_NONBLOCK` so writes never block if no reader
  - Keepalive thread writes 0.5s of silence every 0.5s between transmissions
  - This FIFO feeds ffmpeg in p25_server.py for the live audio stream

---

## 3. P25 Web App — Current State

### Auth

- Two users: `p25` (primary) and `viewer` (secondary, from `P25_EXTRA_USERS` in env)
- `viewer` has a 15-minute summarize rate limit
- Auth methods:
  - **Basic**: `Authorization: Basic base64(user:pass)` — stored in sessionStorage as `p25_auth`
  - **Bearer token**: HMAC-SHA256 signed JWT-like token `{u,exp}` — used for QR code / share links
  - **`?t=` query param**: Bearer token in URL, used for `<audio src>` (can't set headers there)
- Auth is validated manually — **no HTTPBasic dependency** (that caused native browser auth dialog)
- QR code generation: primary user only, TTL 2 weeks default

### API endpoints

| Endpoint | Auth | Notes |
|----------|------|-------|
| `GET /api/state` | any | Full state: entries, incidents, log size |
| `GET /api/entries` | any | Raw log entries |
| `GET /api/live` | any | SSE stream of new TX + summary_start events |
| `POST /api/summarize` | any (rate limited for viewer) | SSE stream of Claude summary |
| `GET /api/logs/download` | any | Download raw log as timestamped .txt |
| `GET /api/users` | primary only | List users (for share dropdown) |
| `POST /api/login/share` | primary only | Generate QR code + bearer token |
| `GET /api/audio/token` | any | Short-lived (1h) bearer token for audio URL |
| `GET /api/audio` | any (via `?t=` or header) | MP3 audio stream |

### Summarization (`/api/summarize`)

- **Model**: `claude-sonnet-4-6` (upgraded from haiku this session)
- **Tool**: `web_search_20260209` (server-side, Anthropic handles it — no agentic loop needed)
- **Two modes** controlled by `SummarizeReq.full`:
  - **Incremental** (`full=False`): reads tx since last summary, max_tokens=1024
  - **Full** (`full=True`): reads entire log minus existing summary blocks, max_tokens=16000, primary user only, bypasses rate limit
- The prompt includes Lafayette/47905 area context: roads, landmarks, agencies, 10-codes
- Claude is told to search silently (no narration of search intent)
- **Log markers**:
  - Incremental: `=== SUMMARY === YYYY-MM-DD HH:MM:SS`
  - Full: `=== FULL SUMMARY === YYYY-MM-DD HH:MM:SS`
  - Both terminated by `========================================` (40 `=`)

### Live Audio Streaming

- `stt_audio.py` writes PCM to `/tmp/p25_audio.fifo`
- `p25_server.py` runs one persistent ffmpeg subprocess reading the FIFO, encoding to MP3 32kbps
- MP3 chunks fanned out to all connected listeners via `asyncio.Queue`
- Frontend: speaker icon in header, fetches `/api/audio/token` then plays via `<audio src="/api/audio?t=TOKEN">`
- Active state shows icon in blue

### Incident Derivation (`derive_incidents`)

Three-layer priority (fixed a bug where oldest data was winning):
1. **Full summaries** (base context, oldest→newest within layer)
2. **Incremental summaries** (update layer, oldest→newest — newer occurrence overrides older)
3. **Raw tx entries** since last summary — tags matching incidents with `recent_tx` count and `last_tx_time`

Agency matching for layer 3 is approximate (category-based: police/fire/ems).
`recent_tx` / `last_tx_time` fields are on incidents in the API — frontend doesn't surface them yet.

### Deployment

- **URL**: `https://p25.sadbabyrabbit.com`
- **systemd**: `p25-server.service`, reads `/etc/p25-server.env`
- **Env file** (`/etc/p25-server.env`) — **DO NOT OVERWRITE**, must preserve all four keys:
  - `P25_USER`, `P25_PASSWORD`, `ANTHROPIC_API_KEY`, `P25_EXTRA_USERS`
  - `P25_EXTRA_USERS={"viewer":{"password":"s3cr3tp455","summarize_interval_seconds":900}}`
- **nginx**: proxies HTTPS → 127.0.0.1:8765, `proxy_buffering off` for SSE
- **LAN IP**: 192.168.4.26 (static)
- **Public IP**: 104.218.151.49

systemd unit at `/etc/systemd/system/p25-server.service`:
```ini
[Unit]
Description=P25 Web App
After=network.target

[Service]
User=cstahly
WorkingDirectory=/home/cstahly/op25_tippecanoe
EnvironmentFile=/etc/p25-server.env
ExecStart=/usr/bin/python3 -m uvicorn p25_server:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 4. Satellite Scheduler — Currently PAUSED

All rules `"enabled": false`. To resume, set rules back to `"enabled": true`.

Satellites active before pausing:
- ISS (25544) — 145.800 MHz
- FUNcube-1 AO-73 (39444) — 145.815 MHz
- SO-50 (27607) — 145.850 MHz
- Meteor-M2 3 (57166) — 137.900 MHz (LRPT OFF as of June 2026)
- ORBCOMM FM109/FM112/FM114/FM118 — various 137 MHz

**Meteor-M2 4 (59051) LRPT is OFF** as of June 4 2026. Verify live status before re-enabling.

---

## 5. Hardware

| Device | Details |
|--------|---------|
| HackRF One | Serial: `14d463dc2f209de1` — primary SDR, runs P25 |
| RTL-SDR v3 | Backup |
| V-dipole | 54cm arms, ~137 MHz, outdoor mast ~12 ft |
| Arrow 440-3 | 3-element 70cm Yagi, ordered |

Location: Lafayette IN, 40.42°N 86.88°W, 180m alt.

PPM calibrated against NOAA WX 162.400 MHz this session — offset is 0.0, no correction needed.

---

## 6. ATC Reception (manual only)

KLAF (Purdue Airport):
- **127.75 MHz** — ATIS
- **119.6 MHz** — Tower
- **123.85 MHz** — Approach/Departure

Aviation uses AM. Command:
```bash
hackrf_transfer -f 127750000 -s 2000000 -r - | \
  python3 ~/src/satellites-overhead/hackrf_am_demod.py | \
  sox -t raw -r 16000 -e signed -b 16 -c 1 - -d
```

---

## 7. Environment

- `ANTHROPIC_API_KEY` — in `/etc/p25-server.env` (for web app) and `~/.zshrc` (for CLI scripts)
- OP25 source: `~/src/op25/` — built with cmake fixes for CMP0026/CMP0045 (cmake 4.x)
- Claude Code uses OAuth, not the API key

---

## 8. Pending / Known Issues

1. **`recent_tx` / `last_tx_time`** on incident cards — data is in the API, not yet surfaced in UI
2. **Audio stream timeout** — `asyncio.wait_for(q.get(), timeout=30)` closes the stream after 30s silence. The stt_audio.py keepalive writes silence every 0.5s so this shouldn't trigger, but if ffmpeg dies and restarts, listeners may briefly disconnect.
3. **Satellite scheduler** — re-enable when user is ready
4. **P25 audio quality** — choppiness is likely signal margin (not PPM). BB:32 is current. If still choppy, signal path / antenna placement is the next thing to investigate.
5. **Mast/coax planning** — user wants 3 antennas on a mast (v-dipole + Arrow Yagi + ?), 3 coax runs
