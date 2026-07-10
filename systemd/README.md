# STT wedge watchdog

`stt_watchdog.sh` + these units auto-recover the recurring Whisper STT wedge
(op25 keeps decoding but the in-process STT thread stalls; p25_log.txt stops
growing while stderr.log keeps growing). See the header of `stt_watchdog.sh`.

## Install (user systemd)
```
cp systemd/op25-stt-watchdog.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now op25-stt-watchdog.timer
loginctl enable-linger cstahly   # so it runs without an active login
```
## Disable / roll back
```
systemctl --user disable --now op25-stt-watchdog.timer
```
Runs every 5 min; restarts op25 only when it's actively decoding but the
transcript feed has been stale > 20 min (and op25 has been up > 20 min).
