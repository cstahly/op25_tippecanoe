# P25 System — Agent Handoff

Last updated: 2026-06-05. Read this before doing anything. Hardware is a Kali bare-metal
box with `bypassPermissions` mode enabled in Claude Code. Full sudo. RDP access.

---

## 1. What's Running Right Now

| System | Status | Notes |
|--------|--------|-------|
| **P25 decoder (OP25)** | Operational | Decoding Tippecanoe County traffic |
| **Whisper STT** | Operational | `turbo` model, CPU, int8 |
| **p25_summarize.py** | Operational | On-demand Claude summary, press Enter |
| **p25_server.py (web app)** | Deployed on HTTP | systemd + nginx running; HTTPS waits for DNS |
| **Satellite scheduler** | **PAUSED** | All rules `"enabled": false` |
| **ATC reception** | Manual only | Working command, not scheduled |

---

## 2. P25 System

### How to run OP25

```bash
cd ~/op25_tippecanoe
python3 ~/src/op25/op25/gr-op25_repeater/apps/multi_rx.py -v 1 -c tippecanoe.json 2>stderr.log
```

Run the summarizer in a second terminal:
```bash
python3 ~/op25_tippecanoe/p25_summarize.py
# Press Enter to get an AI summary. A note box appears — type context or hit Enter to skip.
```

### Tippecanoe County P25 config

- **System**: P25 Phase I Trunked
- **WACN**: 0xBEE00 — **SysID**: 0x6FD
- **Control channels**: 851.050 / 853.8375 / 857.7375 MHz
- **HackRF gains**: RF:14, IF:16, BB:20 — rate 2 Msps, offset 25 kHz
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
| `~/op25_tippecanoe/p25_log.txt` | Live transcript log (all STT output + summaries) |
| `~/op25_tippecanoe/stderr.log` | OP25 stderr (TGID tracking, decoder state) |
| `~/op25_tippecanoe/p25_summarize.py` | On-demand Claude summary (CLI) |
| `~/op25_tippecanoe/p25_server.py` | Web app backend (FastAPI, port 8765) |
| `~/op25_tippecanoe/static/index.html` | PWA frontend |
| `~/src/op25/op25/gr-op25_repeater/apps/stt_audio.py` | Audio module (drop-in for sockaudio.py) |

### stt_audio.py internals

Drop-in replacement for sockaudio.py. Intercepts the OP25 UDP audio socket, buffers each
transmission (flag packet 0 = drain/end-of-TX, flag 1 = drop/abort), and on drain submits
the PCM buffer to a thread pool for Whisper transcription. Output goes to `p25_log.txt`.

- **Model**: `turbo` (distilled large-v3), CPU, int8 — use CPU only, GPU kills RDP
- **Audio format**: 8000 Hz S16LE mono from OP25, upsampled 2x to 16000 Hz float32 for Whisper
- **TGID detection**: polls `stderr.log` for `tg(\d+)` pattern after each transmission
- **Minimum clip**: 1 second (shorter clips discarded)

**CRITICAL**: Never switch Whisper to `device="cuda"`. The GPU is also used for RDP display
encoding and Whisper will make the RDP session unusable even when CPU is idle.

---

## 3. P25 Web App — Deployment

### What it does

- **URL target**: `p25.sadbabyrabbit.com` (user controls this domain)
- **Auth**: HTTP Basic Auth — default `p25` / `scanner`, override with `P25_USER` / `P25_PASSWORD` env vars
- **Live feed**: SSE stream of OP25 transmissions, color-coded by agency (blue=police, red=fire, green=EMS)
- **Incident grouping**: transmissions < 5 min apart grouped into visual clusters
- **Clickable cards**: tap to expand long transcriptions
- **AI Summary**: modal with optional note → streams Claude haiku response → summary card in feed
- **PWA**: installable on iOS/Android via "Add to Home Screen"

### How to start it

```bash
cd ~/op25_tippecanoe
P25_PASSWORD=yourpassword python3 -m uvicorn p25_server:app --host 0.0.0.0 --port 8765
```

### Current deployment state

- **systemd**: `p25-server.service` enabled/running, reads `/etc/p25-server.env`
- **nginx**: enabled/running, proxies `p25.sadbabyrabbit.com` on port 80 → `127.0.0.1:8765`
- **DNS needed**: add `p25 A 104.218.151.49`
- **HTTPS still needed**: once DNS resolves, run certbot and switch to the SSL nginx config

HTTP nginx config:
```nginx
server {
    listen 80;
    server_name p25.sadbabyrabbit.com;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        # Required for SSE:
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        add_header X-Accel-Buffering no;
    }
}
```

HTTPS command to run after DNS resolves:
```bash
sudo certbot --nginx -d p25.sadbabyrabbit.com
```

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

Rules file: `~/sdr_scheduler_rules.json`  
Scheduler: `~/src/satellites-overhead/scheduler/sdr_scheduler.py`

**All rules are currently `"enabled": false`** — user paused the whole thing to focus on P25.

To resume: set rules back to `"enabled": true` as desired. The scheduler reads the file live.

Satellites the user had active before pausing:
- ISS (25544) — 145.800 MHz, raw IQ
- FUNcube-1 AO-73 (39444) — 145.815 MHz, raw IQ
- SO-50 (27607) — 145.850 MHz, raw IQ
- Meteor-M2 3 (57166) — 137.900 MHz, LRPT (beacon only, LRPT OFF as of June 2026)
- ORBCOMM FM109/FM112/FM114/FM118 — various 137 MHz

**Meteor-M2 4 (59051) LRPT is also OFF** as of June 4 2026 — confirmed dead across multiple
passes. Do not re-enable until fresh status is verified from a live source.

See `~/src/satellites-overhead/CLAUDE.md` for full satellite scheduler documentation.

---

## 5. Hardware

| Device | Details |
|--------|---------|
| HackRF One | Serial: `14d463dc2f209de1` — primary SDR |
| RTL-SDR v3 | Backup |
| V-dipole | 54cm arms, ~137 MHz, outdoor mast ~12 ft |
| Arrow 440-3 | 3-element 70cm Yagi, ordered — for hand-tracking LEO sats |
| RTL-SDR Blog dipole kit | Arrived — multipurpose, 23cm arms minimum |

Location: Lafayette IN, 40.42°N 86.88°W, 180m alt.

---

## 6. ATC Reception

Working, manual only. KLAF (Purdue Airport) frequencies:
- **127.75 MHz** — ATIS
- **119.6 MHz** — Tower
- **123.85 MHz** — Approach/Departure

Aviation uses **AM** not FM. Command:
```bash
hackrf_transfer -f 127750000 -s 2000000 -r - | \
  python3 ~/src/satellites-overhead/hackrf_am_demod.py | \
  sox -t raw -r 16000 -e signed -b 16 -c 1 - -d
```

Change `-f` for other frequencies. The v-dipole antenna works for this.

---

## 7. Environment

- `ANTHROPIC_API_KEY` — set in `~/.zshrc` (last line). The key pasted during chat history is
  **compromised** — user was warned to revoke at console.anthropic.com. Current key in .zshrc
  should be a fresh one.
- Claude Code uses OAuth (not the API key). The API key is only for scripts calling the API
  directly (p25_summarize.py, p25_server.py).
- OP25 source: `~/src/op25/` — built with cmake fixes for CMP0026/CMP0045 (required for cmake 4.x)

---

## 8. Pending Work

1. **Finish HTTPS** — add DNS `p25 A 104.218.151.49`, then run certbot for p25.sadbabyrabbit.com
2. **Mast/coax planning** — user wants 3 antennas on a mast (v-dipole + Arrow Yagi + ?), 3 coax runs
3. **Satellite scheduler** — re-enable when user is ready to resume sat work
4. **P25 improvements** (future, explicitly deferred):
   - SQLite incident database
   - Incident aggregation across talkgroups
   - Push notifications
