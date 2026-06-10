# Codex Handoff — OP25 Curses "All" Channel Display

## The One Remaining Problem

The OP25 curses terminal (`terminal.py`) has a 3-state audio cycle (left/right arrow): TC → SAFE-T → All.

"All" mode should show both trunks' current status on one line in the bottom bar (`active1`), like:

```
[ALL] Tippecanoe County voice freq 851.8500... | Indiana SAFE-T voice freq 858.7125...
```

It still may not render correctly on switch. The logic is in `_render_all` and the caching in `process_json`.

## Relevant Files

- `/home/cstahly/src/op25/op25/gr-op25_repeater/apps/terminal.py` — curses terminal
- `/home/cstahly/src/op25/op25/gr-op25_repeater/apps/stt_audio.py` — audio + Whisper
- `/home/cstahly/op25_tippecanoe/tippecanoe.json` — OP25 config (DO NOT MODIFY)
- `/home/cstahly/op25_tippecanoe/stderr.log` — OP25 stderr output

## Architecture

- OP25 runs as `op25` command (see `/usr/local/bin/op25`), started manually by user
- Two trunked systems on one HackRF: Tippecanoe County (Voice0/port 23456) and Indiana SAFE-T (Voice1/port 23457)
- `rx_ctl.get_chan_status()` in `trunking.py` only ever returns ONE channel's data (hardcoded `d['0']`, `d['channels'] = ['0']`) — this is the root cause of all the complexity
- `trunk_update` messages DO contain ALL systems' data keyed by NAC number
- The terminal receives JSON messages via a queue from multi_rx.py

## What Was Tried and Why It Failed

1. **Cache from `channel_update`** — fails because `channel_update` only ever carries the most recently active system. `_ch_display` ends up with only one entry.

2. **Read `/tmp/p25_ch_status.json`** — stt_audio.py writes this, but it depends on TX drains having happened for both channels. Timing-dependent and fragile.

3. **Read `stderr.log` directly** — correct data is there (`[0] voice update` and `[1] voice update` lines), but silently failed due to exception handling hiding errors.

4. **Cache from `trunk_update`** — THIS IS THE RIGHT APPROACH. `trunk_update` always contains ALL systems in `msg[nac]` for each NAC. Current code does this but may have a bug in `_render_all` or the cache key.

## Current Code State

In `terminal.py`:

```python
# In __init__:
self._ch_display = {}   # sysname -> last known display string

# In process_json, trunk_update handler (added at top of handler):
for nac in nacs:
    sysname = msg[nac].get('system') or ('NAC %s' % nac)
    freqs   = msg[nac].get('frequencies') or {}
    freq_str = next(iter(freqs.values()), None) if freqs else None
    top      = str(msg[nac].get('top_line', ''))
    self._ch_display[sysname] = freq_str if freq_str else top
if _AUDIO_CYCLE[self._audio_idx] == 'all':
    self._render_all()

# _render_all:
def _render_all(self):
    if not self._ch_display:
        return
    s = ('[ALL] ' + ' | '.join(self._ch_display.values()))[:(self.maxx - 16)]
    self.active1.erase()
    self.active2.erase()
    self.active1.addstr(0, 0, s)
    self.active1.refresh()
    self.stdscr.refresh()
```

## Debugging Tips

- Remove `except Exception: pass` in `_render_all` temporarily to see actual errors in `stderr.log`
- Add `sys.stderr.write(f"[DEBUG] _ch_display={self._ch_display}\n")` in `_render_all` to verify cache contents
- Check if `trunk_update` messages actually contain both NACs: add debug print in the `trunk_update` handler
- OP25 writes to `~/op25_tippecanoe/stderr.log`
- `tail -f ~/op25_tippecanoe/stderr.log` while running to see debug output

## Security Constraints

- DO NOT touch `/etc/p25-server.env`
- Whisper must stay CPU-only (no CUDA)
- Do not restart OP25 casually — user runs it manually
- `~/src/op25/CMakeLists.txt` is modified for unrelated reasons, do not revert

## How to Restart

```bash
# OP25 (curses terminal):
op25   # run in terminal

# Web server:
sudo systemctl restart p25-server
```
