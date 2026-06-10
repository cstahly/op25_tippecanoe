# P25 / SDR System — Agent Handoff

Last updated: 2026-06-10 (morning session). Read before changing anything.

Hardware is a Kali bare-metal box. Full sudo is available. OP25 runs as a user terminal
process; the web app runs under systemd.

---

## 1. Current Running State

| System | Status | Notes |
|--------|--------|-------|
| OP25 decoder | Operational | Launcher: `/usr/local/bin/op25`. User service available: `systemctl --user {start,stop,status} op25` runs it in tmux session "op25" — curses UI attachable from any ssh/tty via `tmux attach -t op25` (detach: Ctrl-b d). Linger is enabled so it starts at boot. Note: post-call `control channel timeout, freq(857.7375)` lines in stderr are benign ~1s CQPSK re-acquisition on the live CC, not a fault. |
| Whisper STT | Operational | `turbo` (large-v3-turbo), CUDA int8 on the 1050 Ti — GPU per user's choice 2026-06-10; old "never CUDA" rule obsolete. large-v3 does NOT fit: weights load (1.6GB) but decode OOMs the 4GB card. GPU decodes serialized + automatic CPU fallback on CUDA OOM (stt_audio.py, commits e3f5dca/7b3052c). |
| Web app | Operational | `p25-server.service`, HTTPS via nginx |
| Incident state DB | Operational | `~/op25_tippecanoe/p25_state.db` |
| Satellite scheduler | **Active** | system `sdr-scheduler.service`; rules in `~/sdr_scheduler_rules.json` |
| SAWbird+ NOAA 137 | **In chain** | Confirmed working — LED lit during captures. Powered via bias-tee. |

---

## 2. OP25 / STT

User's normal OP25 command:

```bash
cd ~/src/op25/op25/gr-op25_repeater/apps && python3 multi_rx.py -v 1 -c ~/op25_tippecanoe/tippecanoe.json 2>~/op25_tippecanoe/stderr.log
```

- `tippecanoe.json` uses `tgid_tags_file: "trunk-tags.tsv"` — relative path matters, run from apps dir.
- Do not restart OP25 unless necessary.
- HackRF One serial `14d463dc2f209de1` is the P25 radio — **never touch for satellite work**.

Tippecanoe config:
- File: `~/op25_tippecanoe/tippecanoe.json`
- HackRF gains: `RF:14,IF:16,BB:32`
- PPM: `0.0`
- Control channels: 851.05000, 853.83750, 857.73750 MHz

`stt_audio.py`:
- Path: `~/src/op25/op25/gr-op25_repeater/apps/stt_audio.py`
- Whisper: `turbo`, CUDA int8 (see §1 — old "never CUDA" rule obsolete)
- Audio FIFO: `/tmp/p25_audio.fifo`

---

## 3. Web App

URL: `https://p25.sadbabyrabbit.com`

**Do not overwrite `/etc/p25-server.env`** — contains `P25_USER`, `P25_PASSWORD`, `ANTHROPIC_API_KEY`, `P25_EXTRA_USERS`.

Service:
```ini
[Service]
User=cstahly
WorkingDirectory=/home/cstahly/op25_tippecanoe
EnvironmentFile=/etc/p25-server.env
ExecStart=/usr/bin/python3 -m uvicorn p25_server:app --host 127.0.0.1 --port 8765
```

State DB: `~/op25_tippecanoe/p25_state.db` — transmissions table rebuilds from log, but
`incident_state`, `incident_tx`, and `geocode_cache` are authoritative — do not delete.
Backed up daily at 14:00 via cron (`backup_db.sh` → `macbook-pro-3.local:~/backups/`, silent no-op if Mac is off).

### Conversational slicing / tx attribution (2026-06-10, commit 1c04c4a)

The summarizer attributes individual transmissions to incidents:

- Prompt lines are numbered `#ID`; the model returns `tx_ids` per incident.
  Attribution is deliberately **liberal** — garbled/ambiguous lines get best-guess
  attribution by talkgroup/timing/adjacency. Expect occasional wrong guesses.
- `incident_tx` table maps incident → tx (with time/talkgroup fallback columns).
- `last_seen` = newest attributed tx timestamp (real radio activity), NOT the summary
  clock. `first_seen` for new incidents = oldest attributed tx. Falls back to summary
  clock if the model omits tx_ids.
- `sync_transmissions_from_log` is **append-only** — tx ids are stable. Full rebuild
  only if the log shrinks (logged to stderr; ids may shift, hence the fallback columns).
- `GET /api/incidents/{number}/transcript` — attributed lines + wav clips per incident.
  Backend exists; no client UI for it yet (natural next step: transcript + clip playlist
  in incident detail on web/iOS).
- Auto-summary: every **15 min** (`P25_AUTO_SUMMARY_INTERVAL`, was 2h), first run 2 min
  after server start (was: slept a full interval first). This matters because stale
  display threshold is 1h — with a 2h interval everything looked stale between runs.

### Status/priority model (2026-06-09/10 sessions)

- status_kind: `active` | `routine` | `clear` — "watch" eliminated entirely.
- priority 1-5: P1 purple #a855f7, P2 red #ef4444, P3 yellow #eab308, P4 sky #0ea5e9, P5 slate #475569.
- CLEAR forces priority ≥ 4 (server clamp + prompt rule).
- Incident sort is always priority then status weight — no sort toggle.
- `is_stale` (last_seen > 1h, `P25_STALE_DISPLAY_SECONDS`) is a **display hint only —
  never use it to hide incidents from map/list filters** (caused empty-map bugs twice).
  Auto-clear at 4h (`P25_STALE_INCIDENT_SECONDS`) is separate.

### iOS app (~/src/p25-ios, Mac: macbook-pro-3.local)

- Map tab: pins colored by priority; filter pill = Critical / Now / 8hr / All (default Now =
  non-cleared). Incidents tab filter: Active / Critical / All.
- CarPlay: pins colored by priority via `withTintColor(_, renderingMode: .alwaysOriginal)` —
  `UIImage.SymbolConfiguration(paletteColors:)` does NOT tint monochrome symbols (black-dot bug).
- After pushing iOS: `ssh macbook-pro-3.local "cd ~/src/p25-ios && git pull"`, then verify with
  `xcodebuild -scheme P25Monitor -destination 'platform=iOS Simulator,OS=18.5,name=iPhone 16 Pro Max' build`
  **before telling the user it's ready**. Code signing fails over SSH (errSecInternalComponent) — expected;
  user hits Run in Xcode himself.

---

## 4. Satellite Scheduler

### Service

System service (not user service). Managed with:
```bash
sudo systemctl restart sdr-scheduler
sudo systemctl status sdr-scheduler
```

- **Unit file**: `/etc/systemd/system/sdr-scheduler.service`
- **Script**: `~/src/satellites-overhead/scheduler/sdr_scheduler.py`
- **Rules file**: `~/sdr_scheduler_rules.json`
- **Captures**: `~/noaa_captures/` (Meteor LRPT), `~/cosmos_captures/` (other sats)
- **Log**: `~/sdr_scheduler.log`
- **Location**: Lafayette, IN — 40.4259°N, 86.9081°W

### RTL-SDR hardware

- RTL-SDR v3 (USB, SN 00000001) for satellite work
- SAWbird+ NOAA 137 LNA in chain — powered via bias-tee (`rtl_biast -d 0 -b 1` before capture, `-b 0` after)
- Antenna: horizontal V-dipole (53.4cm arms, 137 MHz) on outdoor 12' painter's pole, ~3 ft below ADS-B. Primary sat antenna — has been in place for a while; previously misdocumented as "145 MHz." Currently via SAWbird+ NOAA 137 → RTL-SDR #1. Plan: swap SAWbird+ for Nooelec LaNA (wideband) to also cover 145/435 MHz birds on same antenna.

### Active rules (as of 2026-06-10)

Only 137 MHz rules are enabled. All 145/435 MHz rules disabled pending Nooelec LaNA arrival.

| NORAD | Name | Freq | bias_tee | Notes |
|-------|------|------|----------|-------|
| 57166 | METEOR-M2 3 | 137.9 MHz | true | Primary target |
| 59051 | METEOR-M2 4 | 137.9 MHz | true | **Off-air** — 0 CADU on every pass, do not waste time debugging |

All other rules (`enabled: false`): AO-73, AO-7, AO-91, PO-101, JO-97, CAS-6, SO-50, FO-29, RS-44, ISS.

Re-enable rules when hardware is ready:
- **435 MHz rules** (SO-50, FO-29, RS-44): re-enable after Arrow 440-3 Yagi arrives (2026-06-13) and LaNA is in chain.
- **145 MHz rules** (AO-7, AO-73, AO-91, CAS-6, JO-97, etc.): no 145 MHz antenna exists — user would need to build a ~49cm arm V-dipole and mount it. Do NOT re-enable these rules until that's done.
- LaNA does NOT need bias-tee. SAWbird+ does. Decide chain config before re-enabling.

### Bias-tee operation

The installed `rtl_sdr` build does not support `-T` flag. Use `rtl_biast` separately:

```bash
rtl_biast -d 0 -b 1   # before capture
rtl_biast -d 0 -b 0   # after capture
```

The scheduler's `rtl_sdr_capture()` handles this automatically when `bias_tee=True` in the rule. The SAWbird+ LED should be lit during every M2-3 and M2-4 capture.

### Bugs fixed in session 2026-06-10

1. **Claude PATH in systemd** — scheduler was logging `PASS MANAGER Claude invoke FAIL — [Errno 2] No such file or directory: 'claude'` on every pass. Fixed by adding `Environment=PATH=/home/cstahly/.local/bin:/usr/local/bin:/usr/bin:/bin` to `/etc/systemd/system/sdr-scheduler.service`. Service restarted.

2. **Backfill elevation = 0 bug** — `backfill_meteor_images()` was looking up history by the `_decode` capdir (e.g. `meteor_m2_3_1113_decode`) which never matched history keys (keyed by `meteor_m2_3_1113`). Fixed in `sdr_scheduler.py`: `_lookup = capdir.removesuffix("_decode")` fallback. Manifest also corrected: `meteor_m2_3_2234_decode` 0→69.0°, `meteor_m2_3_1113_decode` 0→79.9°. Index re-pushed to server.

### Bugs fixed in session 2026-06-09

1. **Blocking satdump decode**: `rtlsdr_satdump_decode()` was calling `proc.wait()` on the satdump process, blocking the scheduler's main loop and causing it to miss subsequent passes. **Fixed**: satdump now runs in a background thread (`threading.Thread(target=_run_decode, daemon=False).start()`). The function returns 0 immediately after launching the thread; DECODE DONE is logged asynchronously.

2. **satdump TLE fetch loop**: satdump is hardcoded to fetch TLEs from `http://celestrak.org` port 80, which is unreachable. When the fetch fails, satdump retries indefinitely and never decodes. **Fixed**: `refresh_satdump_tles()` runs before every satdump invocation. It tries alternate sources (5s timeout each), falls back to the scheduler's `.tlecache/active.tle`, then stamps `tles_last_updated` in `~/.config/satdump/settings.json` to now. satdump sees fresh TLEs and skips its own fetch. The stamp lasts 24 hours (satdump's default update interval); since `refresh_satdump_tles()` runs before every decode, it stays current indefinitely.

### Meteor LRPT decode pipeline

satdump live RTL-SDR source is broken. Working pipeline: capture CU8 IQ with `rtl_sdr`, decode offline:

```bash
# Must use --samplerate 2000000 — rtl_sdr captures at 2 MS/s, not 1 MS/s
satdump meteor_m2-x_lrpt baseband <file.iq> <outdir> \
  --samplerate 2000000 --baseband_format cu8 --iq_swap --dc_block
```

Before running satdump manually, bump the TLE timestamp first:
```python
python3 -c "
import json, time
f = open('/home/cstahly/.config/satdump/settings.json', 'r+')
c = json.load(f); c.setdefault('user', {})['tles_last_updated'] = int(time.time())
f.seek(0); json.dump(c, f, indent=4); f.truncate()
"
```

### M2-3 decode status (as of 2026-06-10)

**SAWbird+ first success: 74.0° pass (2026-06-08 22:10 local) — 3.3MB CADU, full imagery.** Best images at `~/noaa_captures/meteor_m2_3_2210/MSU-MR/`. A diagonal-fade splice of MSA_corrected_map (top-left) + AVHRR_3a21_false_color_corrected (bottom-right) was uploaded to sadbabyrabbit.com/meteor/ as `20260609T0251.png` and saved locally as `meteor_splice.png` in that same dir.

Failed passes with SAWbird+: 28.8°, 22.1°, 22.1° — all 0 CADU. Elevation threshold appears to be ~40°+ for reliable lock with V-dipole + SAWbird+. Gain=40 is correct at 74° — do not adjust without a reason.

Previously confirmed working (before SAWbird+, 2026-06-07): successful decodes at 41.7° and 79.9° passes.

Pending cleanup: `~/noaa_captures/meteor_m2_3_2328.iq` (3.7GB, 22.1° pass, 0 CADU confirmed) — safe to delete.

### Critical fixed bugs from earlier sessions (do not reintroduce)

1. **USB autosuspend** — fixed via `/etc/udev/rules.d/99-rtlsdr-autosuspend.rules`
2. **Duplicate scheduler instances** — fixed with `fcntl.flock` PID lock on `~/.sdr_scheduler.pid`
3. **RTL-SDR gain** — optimal is 37 dB. At 49 dB noise floor is ~7800 RMS. Do not raise gain.
4. **Samplerate bug** — always `2000000`, not `1000000`. Fixed 2026-06-07.
5. **Noise floor calibration** — `sat_iq_summary.py` uses 10th-percentile RMS, not first-10s baseline.

---

## 5. Incoming Hardware

| Item | ETA | Notes |
|------|-----|-------|
| Nooelec LaNA (standard, NOT WB) | 2026-06-11 (tomorrow) | Wideband LNA 20MHz-4GHz. Does NOT need bias-tee. Re-enable all disabled sat rules after chain is configured. |
| Arrow Antenna II 440-3 Yagi | 2026-06-13 (Fri) | 3-el 70cm Yagi. Fixed mount facing ENE (35.5% of elevation-weighted passes) based on 97-pass analysis. |
| LiteVNA | 2026-06-13 (Fri) | For antenna sweep/characterization |

---

## 6. Hardware Inventory

- **HackRF One** (serial `14d463dc2f209de1`) — P25 only, do not use for satellite
- **RTL-SDR v3 #1** (SN 00000001) — satellite scheduler
- **RTL-SDR v3 #2** (arrived 2026-06-10) — ADS-B / piaware at 1090 MHz
- **SAWbird+ NOAA 137** — inline on V-dipole → RTL-SDR #1, bias-tee powered
- **Nooelec LaNA** — arriving 2026-06-11; wideband, no bias-tee needed
- **Kenwood TR-7400A** — 2m FM rig, working. Speaker connected. No tone pad yet. PTT confirmed by shorting mic pins to ground (TX indicator lights up). DUP -600 kHz tested on 146.730 W9ARP (no tone).
- **Heltec LoRa32 V4** — purchased, not set up. Meshtastic target, 915 MHz ISM.
- **FA ADS-B antenna** — top of outdoor mast (~12-13 ft), on RTL-SDR #2 / piaware
- **Horizontal V-dipole, 53.4cm arms** — ~3 ft below ADS-B on painter's pole. Primary sat antenna, long-established. SAWbird+ NOAA 137 → RTL-SDR #1 / satellite scheduler (137.9 MHz).
- **V-dipole 8.8cm telescoping arms** — ~2 ft below horizontal V-dipole on mast, vertical orientation. For P25 / HackRF (8.8cm = λ/4 at 852 MHz, matches Tippecanoe control channels). User considering moving inside; considering a 60cm V-dipole for 2m repeater RX instead.

### Outdoor Mast Layout (as of 2026-06-10)

12' painter's pole. Top to bottom:
1. **FA ADS-B antenna** — ~12-13 ft AGL → ~25 ft coax → RTL-SDR #2 / piaware (1090 MHz)
2. **Horizontal V-dipole (53.4cm arms)** — ~3 ft below ADS-B → SAWbird+ NOAA 137 (for now) → RTL-SDR #1 / satellite scheduler (137.9 MHz). Swap to Nooelec LaNA when it arrives (2026-06-11) to enable 145/435 MHz coverage on same antenna.
3. **8.8cm V-dipole (vertical)** — ~3 ft below horizontal V-dipole → HackRF (P25, ~852 MHz). User moving this inside.

---

## 7. Kenwood TR-7400A

- **Mic connector**: 4-pin square DIN (NOT 8-pin — confirmed from physical inspection)
- **Tone pad connector**: proprietary multi-pin (NOT 3.5mm)
- **PL tones**: hardware module, not programmable. Module L79-0418-05 = 131.8 Hz.
- **Tone knob**: controls receive CTCSS squelch (not transmit)
- **Duplex**: DUP/RPT switch enables ±600 kHz TX offset for repeater operation
- Speaker jack works. EXT SP requires 8Ω speaker, not headphones.

Local repeaters (Lafayette/West Lafayette, IN):
| Freq | Call | PL | Notes |
|------|------|----|-------|
| 146.730 | W9ARP | None | Easiest to try first — no tone needed |
| 146.760 | W9YB | 131.8 Hz | Purdue club, most active |
| 147.135 | WI9RES | 131.8 Hz | Also 131.8 Hz |

---

## 8. GMRS / 462 MHz Antenna

User is researching GMRS. License: $35, no exam, covers immediate family, 10 years.
No existing radio is type-accepted for GMRS (HackRF cannot legally transmit on GMRS).

462 MHz quarter-wave ground plane (calculated at 462.000 MHz, VF 0.95):
- Whip: **15.4 cm**
- Radials (drooped 45°): **17.3 cm**

---

## 9. ADS-B

- piaware running on this machine, second RTL-SDR + FA ADS-B antenna
- Moving antenna to better height dramatically improved range
- UAT 978 MHz deferred — needs 3rd RTL-SDR dongle
- MLAT: small antenna position moves on same property don't require reconfiguration

---

## 10. Misc

- **Do not overwrite `/etc/p25-server.env`**
- **OP25 repo**: `~/src/op25/CMakeLists.txt` is modified — do not revert
- KLAF manual AM monitoring:
  ```bash
  hackrf_transfer -f 127750000 -s 2000000 -r - | \
    python3 ~/src/satellites-overhead/hackrf_am_demod.py | \
    sox -t raw -r 16000 -e signed -b 16 -c 1 - -d
  ```

---

## 11. sadbabyrabbit.com EC2 (main site, NOT p25)

- EC2 `i-0cf37dcf3d7a3a8a5` us-east-2, IP 3.148.96.123, **1GB RAM**. SSH:
  `ssh -i ~/.ssh/sadbabyrabbit.pem ec2-user@sadbabyrabbit.com`.
- Next.js site at `/var/www/site`, served by pm2 cluster "site" (port 3000) behind nginx.
  No git repo on the box — source lives only there. Meteor gallery images in `/var/www/meteor/`.
- **NEVER run `npm run build` on the EC2.** The Turbopack build OOMs the 1GB box into
  swap-death — took the whole site down 2026-06-10, needed a hard reboot, and the
  interrupted build deleted the production `.next`. Build procedure:
  1. `rsync` source (exclude `node_modules`, `.next`) to local `/tmp/site-build`
  2. `npm ci && npm run build` locally
  3. `rsync --delete /tmp/site-build/.next/ ec2:/var/www/site/.next/`
  4. `pm2 restart site` on the EC2
- Meteor gallery: scheduler's `push_meteor_image()` pushes full PNG + 1600px WebP
  (`{ts}_web.webp`, "web" field in index.json). Gallery `<img>` uses the WebP (lazy-loaded),
  full PNG via click-through. Raw PNGs are 4–14MB — serving them directly tripped the
  CloudWatch NetworkOut alarm (>50MB/5min). WebP gallery load is ~3MB total; alarm
  threshold left at 50MB intentionally.
