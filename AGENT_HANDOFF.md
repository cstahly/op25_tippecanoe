# P25 / SDR System — Agent Handoff

Last updated: 2026-06-09 (evening session). Read before changing anything.

Hardware is a Kali bare-metal box. Full sudo is available. OP25 runs as a user terminal
process; the web app runs under systemd.

---

## 1. Current Running State

| System | Status | Notes |
|--------|--------|-------|
| OP25 decoder | Operational | Running as `python3 multi_rx.py -v 1 -c /home/cstahly/op25_tippecanoe/tippecanoe.json` |
| Whisper STT | Operational | `turbo`, CPU, int8. Do not use CUDA — kills RDP display. |
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
- Whisper: `turbo`, CPU, int8
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

State DB: `~/op25_tippecanoe/p25_state.db` — safe to rebuild from log if wedged, but do not delete casually (manual status edits live in DB).

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
- Antenna: outdoor V-dipole on 12' painter's pole, cut for 145 MHz

### Active rules (as of 2026-06-09)

Only 137 MHz rules are enabled. All 145/435 MHz rules disabled pending Nooelec LaNA arrival.

| NORAD | Name | Freq | bias_tee | Notes |
|-------|------|------|----------|-------|
| 57166 | METEOR-M2 3 | 137.9 MHz | true | Primary target |
| 59051 | METEOR-M2 4 | 137.9 MHz | true | **Off-air** — 0 CADU on every pass, do not waste time debugging |

All other rules (`enabled: false`): AO-73, AO-7, AO-91, PO-101, JO-97, CAS-6, SO-50, FO-29, RS-44, ISS.

Re-enable non-137 MHz rules after Nooelec LaNA arrives and is in chain. LaNA does NOT need bias-tee (it has its own power). SAWbird+ NOAA does need bias-tee. Decide chain config before re-enabling.

### Bias-tee operation

The installed `rtl_sdr` build does not support `-T` flag. Use `rtl_biast` separately:

```bash
rtl_biast -d 0 -b 1   # before capture
rtl_biast -d 0 -b 0   # after capture
```

The scheduler's `rtl_sdr_capture()` handles this automatically when `bias_tee=True` in the rule. The SAWbird+ LED should be lit during every M2-3 and M2-4 capture.

### Bugs fixed in this session (2026-06-09)

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

### M2-3 decode status (as of 2026-06-09)

No successful decodes with SAWbird+ yet. All captures tonight:
- 28.8° automated pass: 0 CADU, no lock — low elevation may be insufficient with current antenna
- 45.4° manual pass: 0 CADU, no lock — capture started after LOS (missed due to scheduler blocking bug, now fixed)

Previously confirmed working (before SAWbird+, 2026-06-07): successful decodes at 41.7° and 79.9° passes. Best images at `~/noaa_captures/meteor_m2_3_1113_decode/MSU-MR/`.

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
| Nooelec LaNA (standard, NOT WB) | 2026-06-10 | Wideband LNA 20MHz-4GHz. Does NOT need bias-tee. Re-enable all disabled sat rules after chain is configured. |
| Arrow Antenna II 440-3 Yagi | 2026-06-13 (Fri) | 3-el 70cm Yagi. Fixed mount facing ENE (35.5% of elevation-weighted passes) based on 97-pass analysis. |
| LiteVNA | 2026-06-13 (Fri) | For antenna sweep/characterization |

---

## 6. Hardware Inventory

- **HackRF One** (serial `14d463dc2f209de1`) — P25 only, do not use for satellite
- **RTL-SDR v3 #1** (SN 00000001) — satellite scheduler
- **RTL-SDR v3 #2** — ADS-B / piaware at 1090 MHz (second dongle, FA ADS-B antenna)
- **SAWbird+ NOAA 137** — inline on V-dipole → RTL-SDR #1, bias-tee powered
- **Nooelec LaNA** — arriving tomorrow; wideband, no bias-tee needed
- **Kenwood TR-7400A** — 2m FM rig, working. Speaker connected. No tone pad yet.
- **Heltec LoRa32 V4** — purchased, not set up. Meshtastic target, 915 MHz ISM.
- **V-dipole 145 MHz** — outdoor on 12' painter's pole, active for satellite work
- **V-dipole 8.8cm telescoping arms** — currently on P25 monitor. Arms extend — usable for 462 MHz if extended to ~16cm.
- **FA ADS-B antenna** — on RTL-SDR #2 / piaware

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
- **Do not use CUDA for Whisper** — shares GPU with RDP encoder, kills display
- **OP25 repo**: `~/src/op25/CMakeLists.txt` is modified — do not revert
- KLAF manual AM monitoring:
  ```bash
  hackrf_transfer -f 127750000 -s 2000000 -r - | \
    python3 ~/src/satellites-overhead/hackrf_am_demod.py | \
    sox -t raw -r 16000 -e signed -b 16 -c 1 - -d
  ```
