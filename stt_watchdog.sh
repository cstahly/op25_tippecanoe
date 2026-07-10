#!/usr/bin/env bash
# STT wedge watchdog.
#
# Failure mode it guards: op25's trunk decoder keeps running (stderr.log grows
# with voice updates) but the in-process Whisper STT thread silently wedges and
# stops transcribing — p25_log.txt (the web-app feed) and audio_clips/ stop
# growing. No CUDA error is raised, so systemd can't see it. Observed 2026-06-22
# and 2026-07-08; the fix each time is `systemctl --user restart op25.service`,
# which reloads stt_audio.py and re-inits Whisper.
#
# This only acts on the precise wedge signature — op25 IS decoding but the feed
# is stale — so a genuinely quiet band (no traffic => stderr also stale) never
# triggers a restart.
set -u

LOG_DIR="$HOME/op25_tippecanoe"
STDERR="$LOG_DIR/stderr.log"   # op25 raw decoder output — proves it's still decoding
FEED="$LOG_DIR/p25_log.txt"    # transcript feed — stops growing when STT wedges

STALE_S=1200          # feed stale this long while decoding => wedge (20 min)
DECODE_FRESH_S=300    # stderr written within this => op25 is actively decoding (5 min)
MIN_UPTIME_S=1200     # don't touch an op25 that (re)started < this ago (warmup/anti-loop)

log(){ echo "$(date '+%F %T') stt-watchdog: $*"; }

systemctl --user is-active --quiet op25.service || { log "op25 not active; skip"; exit 0; }
[ -f "$STDERR" ] && [ -f "$FEED" ] || { log "log files missing; skip"; exit 0; }

now=$(date +%s)
stderr_age=$(( now - $(stat -c %Y "$STDERR") ))
feed_age=$(( now - $(stat -c %Y "$FEED") ))

enter_wall=$(systemctl --user show -p ActiveEnterTimestamp --value op25.service)
enter_epoch=$(date -d "$enter_wall" +%s 2>/dev/null || echo 0)
# If we can't determine uptime, treat it as "just started" (0) so we never loop-restart.
if [ "$enter_epoch" -gt 0 ]; then uptime_s=$(( now - enter_epoch )); else uptime_s=0; fi

if [ "$stderr_age" -le "$DECODE_FRESH_S" ] && [ "$feed_age" -ge "$STALE_S" ] && [ "$uptime_s" -ge "$MIN_UPTIME_S" ]; then
    log "WEDGE: op25 decoding (stderr ${stderr_age}s) but feed stale ${feed_age}s (uptime ${uptime_s}s) -> restart op25"
    systemctl --user restart op25.service
    logger -t stt-watchdog "restarted op25.service: STT wedge (feed stale ${feed_age}s while decoding)" 2>/dev/null || true
    if command -v mail >/dev/null 2>&1; then
        printf 'STT watchdog auto-recovered op25 at %s.\nFeed was stale %ss while op25 was decoding.\n' \
            "$(date '+%F %T')" "$feed_age" | mail -s "[p25] STT watchdog restarted op25" cstahly@gmail.com 2>/dev/null || true
    fi
    exit 0
fi

log "ok (stderr ${stderr_age}s, feed ${feed_age}s, uptime ${uptime_s}s)"
