#!/bin/bash
# Safe hot backup of p25_state.db to Mac when available.
set -euo pipefail

TMP=$(mktemp /tmp/p25_state_backup.XXXXXX.db)
trap 'rm -f "$TMP"' EXIT

sqlite3 ~/op25_tippecanoe/p25_state.db ".backup $TMP"
rsync -az --timeout=10 "$TMP" macbook-pro-3.local:~/backups/p25_state.db
