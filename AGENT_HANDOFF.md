# P25 System — Agent Handoff

Last updated: 2026-06-06 (state DB session). Read before changing anything.

Hardware is a Kali bare-metal box. Full sudo is available. OP25 runs as a user terminal
process; the web app runs under systemd.

---

## 1. Current Running State

| System | Status | Notes |
|--------|--------|-------|
| OP25 decoder | Operational | Running as `python3 multi_rx.py -v 1 -c /home/cstahly/op25_tippecanoe/tippecanoe.json` |
| Whisper STT | Operational | `turbo`, CPU, int8. Do not use CUDA. |
| Web app | Operational | `p25-server.service`, HTTPS via nginx |
| Incident state DB | Operational | `~/op25_tippecanoe/p25_state.db` |
| Satellite scheduler | Paused | Leave rules disabled unless user asks |

Current DB snapshot at handoff:

- `transmissions`: 3015
- `incident_state`: 171
- `last_summarized_tx_id`: 2857
- `summary_jobs`: 13
- stale incidents: 101

Latest relevant commits:

- `0671099 Age out stale incidents from summary context`
- `1ca7233 Compact incident context for summaries`
- `dc7eae1 Stabilize incident rendering and feed expansion`
- `4668f7c Add incident detail controls and filters`
- `28b4c1e Add SQLite incident state for summaries`

Repo `~/op25_tippecanoe` is clean at handoff. Repo `~/src/op25` has a pre-existing modified
`CMakeLists.txt`; do not touch/revert it unless user asks.

---

## 2. OP25 / STT

User’s normal OP25 command:

```bash
cd ~/src/op25/op25/gr-op25_repeater/apps && python3 multi_rx.py -v 1 -c ~/op25_tippecanoe/tippecanoe.json 2>~/op25_tippecanoe/stderr.log
```

Handoff previously listed a `cd ~/op25_tippecanoe` variant, but the user specifically runs the
command above. Be careful with relative paths:

- `tippecanoe.json` uses `tgid_tags_file: "trunk-tags.tsv"`.
- Running from the OP25 apps dir can make OP25 log `read_tags_file` missing unless OP25 resolves
  relative to config or the user has adapted around it. Do not restart OP25 unless necessary.

`stt_audio.py`:

- Path: `~/src/op25/op25/gr-op25_repeater/apps/stt_audio.py`
- Buffers OP25 UDP audio, transcribes with faster-whisper, appends TX lines to `p25_log.txt`.
- Whisper: `turbo`, CPU, int8.
- It currently includes a small `initial_prompt` asking Whisper to label clear non-speech audio
  events like `[tone]`, `[siren]`, `[static]`, `[unintelligible]`.
- Audio FIFO: `/tmp/p25_audio.fifo`; used by web app for MP3 live audio stream.

Tippecanoe config:

- File: `~/op25_tippecanoe/tippecanoe.json`
- HackRF gains: `RF:14,IF:16,BB:32`
- PPM: `0.0`
- Control channels: 851.05000, 853.83750, 857.73750 MHz

---

## 3. Web App Architecture

URL: `https://p25.sadbabyrabbit.com`

Service:

```ini
[Service]
User=cstahly
WorkingDirectory=/home/cstahly/op25_tippecanoe
EnvironmentFile=/etc/p25-server.env
ExecStart=/usr/bin/python3 -m uvicorn p25_server:app --host 127.0.0.1 --port 8765
Restart=always
RestartSec=5
```

Do not overwrite `/etc/p25-server.env`. It contains:

- `P25_USER`
- `P25_PASSWORD`
- `ANTHROPIC_API_KEY`
- `P25_EXTRA_USERS`

Auth:

- Basic auth stored in sessionStorage as `p25_auth`
- Bearer token for QR/share links and audio URL query param
- Primary user can generate share QR and run full summaries
- Viewer user has rate limits

---

## 4. State DB Model

Important: `p25_log.txt` is now an audit/source log, not the application state cursor.

SQLite file:

```text
~/op25_tippecanoe/p25_state.db
```

Tables:

- `transmissions`: parsed raw TX lines from `p25_log.txt`
- `incident_state`: current board keyed by Claude-owned incident number
- `app_state`: cursor values, especially `last_summarized_tx_id`
- `summary_jobs`: audit records for summary attempts

The server calls `ensure_state_ready()` to rebuild/sync transmissions from `p25_log.txt`. It is
safe to move/delete `p25_state.db` and let it rebuild from the log if schema/data gets wedged,
but do not do that casually because manual status edits live in the DB.

`/api/state` now serves incidents from `incident_state`. It only falls back to old parsed summaries
if DB incidents are empty.

Manual incident status updates:

```http
POST /api/incidents/{number}
```

Body can include `status`, `title`, `agency`, `location`, `details`, `action`. The common UI action
is setting `status` to `CLEAR`, `ACTIVE`, `PENDING`, or `ROUTINE`. Backend recalculates and persists
`status_kind`.

---

## 5. Summarization

Model: `claude-sonnet-4-6`.

Regular incremental summaries:

- Use SQLite cursor `last_summarized_tx_id`, not summary markers.
- Send compact current incident context plus raw TX rows with `id > last_summarized_tx_id`.
- Claude returns JSON only.
- Server validates JSON and updates `incident_state`.
- Only after successful validation does it advance `last_summarized_tx_id`.
- Failed/truncated/invalid summaries write failed `summary_jobs` rows and do not advance the cursor.
- A readable markdown summary is still appended to `p25_log.txt` for audit/feed display.

Full summaries:

- Still use the earlier chunked markdown path.
- Primary user only.
- Consider this legacy compared with the DB-backed incremental flow.
- Do not rely on full-summary markers for state.

Stale/timeout behavior:

- Env/default: `P25_STALE_INCIDENT_SECONDS = 4h`.
- Non-clear incidents older than this are marked `is_stale: true` in API output.
- Stale does not overwrite actual `status`.
- UI has a Stale filter; default Open excludes stale and clear incidents.
- Prompt context includes fresh open incidents, a small stale-open tail, and recently cleared items.

Current cost/cap notes:

- After compact/stale context, current rough incremental input prompt measured about 5k tokens with
  a large pending queue.
- SWAG cost after optimization: about `$0.025-$0.04` per incremental summary.
- User is considering around `$20/mo` max. Suggested schedule: every 2 hours, with manual summaries
  as needed.

---

## 6. Frontend Notes

Static files:

- `static/index.html`
- `static/sw.js`

Current visible app version: `v19`.
Current service worker cache: `p25-v22`.

Incident UI:

- Search box for text matching.
- Status filters: Open, Active, Watch, Stale, Routine, Clear, All.
- Rows click into a detail modal.
- Modal supports status changes via `/api/incidents/{number}`.
- Address links are map links.
- Rendering was fixed so polling does not rewrite the incident DOM unless incident data changes;
  this avoids layout jitter from address-link relayout.

Feed UI:

- Log cards have delegated expand/collapse handler.
- Avoid reintroducing per-card click handlers plus delegated handlers; that broke show-more once.

---

## 7. Known Issues / Cautions

1. **Many stale active incidents**: 101 stale at handoff. This is expected after migration. User can
   manually clear important ones, or a future agent can add bulk close/age-out tools.
2. **Full summaries are legacy**: full-summary path is chunked for rate limits but not yet DB-native.
3. **Incident context may miss old omitted incidents**: stale tail mitigates this, but a very old
   incident referenced after many hours may get a new number. This is acceptable for cost control
   for now.
4. **Do not let bad Claude output advance state**: preserve the JSON validation + cursor-advance
   ordering.
5. **Do not restart OP25 casually**: user runs it manually and may be watching live traffic.
6. **OP25 repo dirty file**: `~/src/op25/CMakeLists.txt` is modified and unrelated.

---

## 8. Satellite / Hardware / Misc

Satellite scheduler remains paused. Re-enable only if user asks.

Hardware:

- HackRF One serial `14d463dc2f209de1` is primary for P25.
- RTL-SDR v3 backup.
- Outdoor V-dipole around 137 MHz.
- Location: Lafayette, IN, ~40.42 N, 86.88 W.

KLAF manual AM monitoring:

```bash
hackrf_transfer -f 127750000 -s 2000000 -r - | \
  python3 ~/src/satellites-overhead/hackrf_am_demod.py | \
  sox -t raw -r 16000 -e signed -b 16 -c 1 - -d
```
