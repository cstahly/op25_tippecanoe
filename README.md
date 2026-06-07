# Tippecanoe P25 Monitor

Real-time P25 radio monitoring for Lafayette/Tippecanoe County, Indiana. Receives two trunked P25 systems simultaneously from a single HackRF, transcribes transmissions with Whisper, groups them into incident clusters with Claude AI summaries, and serves a live PWA at [p25.sadbabyrabbit.com](https://p25.sadbabyrabbit.com).

## What it monitors

| System | WACN | SYSID | Control Channels |
|--------|------|-------|-----------------|
| Tippecanoe County P25 | BEE00 | 6FD | 851.050, 853.8375, 857.7375 MHz |
| Indiana SAFE-T (ISP Dist. 14) | BEE00 | 6BD | 858.7125, 859.7125 MHz |

The HackRF is fixed-tuned at 855.4 MHz with a 16 MHz passband, covering both systems simultaneously. Surrounding counties (Carroll, Clinton, White) ride the SAFE-T statewide trunk and appear automatically.

## Architecture

```
HackRF → OP25 multi_rx.py → stt_audio.py → p25_log.txt → p25_server.py → browser
                ↓                  ↓
          curses terminal    Whisper STT (CPU)
          (←→ channel            ↓
            switching)     Claude AI summaries
```

- **`stt_audio.py`** — drop-in replacement for OP25's `sockaudio.py`. Buffers per-channel PCM audio, transcribes each transmission with faster-whisper (turbo, CPU-only), and appends tagged log lines to `p25_log.txt`. Also streams PCM to `/tmp/p25_audio.fifo` for web playback.
- **`p25_server.py`** — FastAPI server. Tails `p25_log.txt`, groups transmissions into incidents, serves SSE live feed, requests Claude summaries, and handles audio proxying.
- **`static/`** — PWA (service worker, manifest, offline shell). Map view, incident list, live audio player, status/time filtering.

## Running

**OP25** (run manually in a terminal):
```bash
cd ~/op25_tippecanoe
python3 ~/src/op25/op25/gr-op25_repeater/apps/multi_rx.py \
    -v 1 -c tippecanoe.json 2>stderr.log
```

Or use the startup script in the op25 repo:
```bash
~/src/op25/start_op25.sh
```

**Web server** (systemd):
```bash
sudo systemctl start p25-server
sudo systemctl status p25-server
```

Manual:
```bash
cd ~/op25_tippecanoe
source /etc/p25-server.env
uvicorn p25_server:app --host 127.0.0.1 --port 8765
```

**Standalone summarizer**:
```bash
python3 ~/op25_tippecanoe/p25_summarize.py
```

## Curses terminal controls

| Key | Action |
|-----|--------|
| `←` / `→` | Cycle audio channel: Tippecanoe → SAFE-T → All |
| `↑` / `↓` | Scroll talkgroup list |
| `q` | Quit |

The selected audio channel is written to `/tmp/p25_audio_filter` and polled by `stt_audio.py`.

## Talkgroup tags

- `trunk-tags.tsv` — 230 Tippecanoe County talkgroups from RadioReference (SID 9099)
- `safe-t-tags.tsv` — Indiana SAFE-T talkgroups for active neighboring counties

Format: `TGID<tab>Label` (decimal TGID, no header).

## Deployment

nginx config and systemd unit are in `deploy/`. The service reads secrets from `/etc/p25-server.env` (not in repo):

```
P25_USER=...
P25_PASSWORD=...
ANTHROPIC_API_KEY=...
```

**Whisper must remain CPU-only** on this host — the GPU is used for RDP display encoding.

## Dependencies

```bash
pip install -r requirements.txt
# Key: fastapi uvicorn faster-whisper anthropic numpy
```

OP25 dependencies are handled by the op25 repo build (`cmake`/`make install`).
