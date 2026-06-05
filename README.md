# Tippecanoe P25 Monitor

FastAPI/PWA monitor for the Tippecanoe County P25 system. OP25 writes transcribed
radio traffic to `p25_log.txt`; the web app serves a live authenticated feed,
groups traffic into incident clusters, and can request Claude summaries.

## Run the web app

```bash
cd ~/op25_tippecanoe
P25_USER=p25 P25_PASSWORD=change-me python3 -m uvicorn p25_server:app --host 127.0.0.1 --port 8765
```

The public deployment uses nginx as a reverse proxy for `p25.sadbabyrabbit.com`.
See `deploy/` for the nginx and systemd units.

## Run OP25

```bash
cd ~/op25_tippecanoe
python3 ~/src/op25/op25/gr-op25_repeater/apps/multi_rx.py -v 1 -c tippecanoe.json 2>stderr.log
```

## Summarize from the terminal

```bash
python3 ~/op25_tippecanoe/p25_summarize.py
```

Whisper STT must remain CPU-only. Do not move it to CUDA on this host because
the GPU is needed for RDP display encoding.

