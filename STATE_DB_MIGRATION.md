# State DB Migration Notes

Started: 2026-06-05.

Goal: stop using `p25_log.txt` summary markers as application state. The raw log remains an
audit trail, but incident cards and summary cursors move to SQLite so bad Claude output cannot
advance the cursor or poison the board.

Plan:

1. Add `p25_state.db` with:
   - `transmissions`: parsed raw transcript lines from `p25_log.txt`
   - `incident_state`: current incident board keyed by Claude-owned incident number
   - `app_state`: cursor values such as `last_summarized_tx_id`
   - `summary_jobs`: audit records for summary attempts
2. Sync new transcript lines from `p25_log.txt` into SQLite on API requests.
3. Seed `incident_state` from the latest valid parsed summary board for compatibility.
4. For regular summaries, send only:
   - compact current incident board
   - transmissions with `id > last_summarized_tx_id`
5. Require Claude JSON for incident updates. Only after valid JSON is parsed and applied should
   `last_summarized_tx_id` advance.
6. Keep appending human-readable summary blocks to `p25_log.txt` for audit/UI feed, but do not use
   those markers as the state cursor.

Recovery state before migration:

- Bad preamble-only markers were removed from the log earlier.
- Last valid log summary marker at the time of planning: `2026-06-05 21:49:22`.
- Unsummarized traffic should begin around `[21:49:30]`.
- Backup before migration: `p25_log.backup-20260605-235728.pre-state-db.txt`.

If interrupted:

- Check `git status` in `~/op25_tippecanoe`.
- Do not touch `~/src/op25/CMakeLists.txt`; it was already locally modified.
- If `p25_state.db` exists but schema/code is incomplete, it is safe to move it aside and rebuild
  from `p25_log.txt`.

Implemented in this pass:

- `p25_state.db` schema and rebuild-from-log sync in `p25_server.py`.
- `/api/state` now uses `incident_state` from SQLite, falling back to old summary parsing only if
  the DB has no incidents.
- Regular `/api/summarize` now uses `last_summarized_tx_id` and raw `transmissions` rows, not
  `read_since_last_summary()`.
- Regular summaries ask Claude for JSON only, validate it, update `incident_state`, then advance
  `last_summarized_tx_id`.
- Failed/truncated/invalid regular summaries write a failed `summary_jobs` row and do not advance
  the cursor.
- Full summaries are still on the earlier chunked markdown path and should be considered legacy
  until migrated to the same state-store model.

Verification:

- Service restarted successfully after commit `28b4c1e`.
- SQLite seeded from `p25_log.txt` with 1,571 transmissions and 54 incidents initially.
- Initial DB cursor: `last_summarized_tx_id=1362`, matching the first pending recovery line
  `[21:49:30] [2009] 7-18-198`.
- A recovery regular summary succeeded as `summary_jobs.id=1`, covering `from_tx_id=1363`
  through `to_tx_id=1572`.
- After recovery, `last_summarized_tx_id=1572`; two newer live transmissions were pending at
  verification time.
