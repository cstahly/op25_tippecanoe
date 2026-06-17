# Postgres + PostGIS migration plan (design only — nothing wired yet)

Goal: move the incident/transmission data layer from SQLite to **Postgres 16 + PostGIS**,
model it as **event-log → incident projection**, and push changes via **LISTEN/NOTIFY → SSE**
to retire polling. The LLM summarizer (transmissions → incident clustering) is unchanged.

This is reversible and stageable: stand PG up *alongside* SQLite, shadow-verify, then flip a
flag. Rollback = flip the flag back.

---

## 1. Schema (concrete DDL)

```sql
CREATE EXTENSION IF NOT EXISTS postgis;

-- Append-only event log. id stays the log-line position (stable, like today).
CREATE TABLE transmissions (
    id         BIGINT PRIMARY KEY,            -- log position
    ts         TIMESTAMPTZ,                   -- parsed time (nullable; we also keep raw)
    time_raw   TEXT,
    talkgroup  TEXT,
    agency     TEXT,
    trunk      TEXT,
    text       TEXT,
    wav_file   TEXT,
    raw_line   TEXT
);
CREATE INDEX ON transmissions (id DESC);

-- Geocode cache, now with real geometry (replaces geocode_cache).
CREATE TABLE geocodes (
    address    TEXT PRIMARY KEY,
    lat        DOUBLE PRECISION,
    lng        DOUBLE PRECISION,
    geom       GEOGRAPHY(Point, 4326),        -- set = ST_MakePoint(lng,lat)
    precise    BOOLEAN NOT NULL DEFAULT TRUE,
    cached_at  TIMESTAMPTZ DEFAULT now()
);

-- Incident projection = current state (replaces incident_state).
CREATE TABLE incidents (
    number       INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    agency       TEXT,
    status       TEXT,
    status_kind  TEXT NOT NULL,               -- active | routine | clear
    priority     SMALLINT NOT NULL DEFAULT 3, -- 1..5
    location     TEXT,
    details      JSONB NOT NULL DEFAULT '[]',
    action       TEXT,
    first_seen   TIMESTAMPTZ,
    last_seen    TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ DEFAULT now(),
    first_tx_id  BIGINT,
    last_tx_id   BIGINT,
    alerted      SMALLINT NOT NULL DEFAULT 0,
    geom         GEOGRAPHY(Point, 4326),      -- denormalized from geocodes for fast spatial
    precise      BOOLEAN
);
CREATE INDEX ON incidents (status_kind, priority);
CREATE INDEX ON incidents (last_seen DESC);
CREATE INDEX ON incidents USING GIST (geom);

-- Incident <-> transmission attribution (replaces incident_tx).
CREATE TABLE incident_tx (
    incident_number INTEGER REFERENCES incidents(number) ON DELETE CASCADE,
    tx_id           BIGINT,
    PRIMARY KEY (incident_number, tx_id)
);

-- NEW: append-only audit of state transitions. Powers retro/analytics
-- (priority churn, time-to-clear, escalation history) for ~free.
CREATE TABLE incident_events (
    id       BIGSERIAL PRIMARY KEY,
    number   INTEGER,
    at       TIMESTAMPTZ DEFAULT now(),
    kind     TEXT,        -- created | priority | status | cleared | alert | resolve
    from_val TEXT,
    to_val   TEXT
);
```

Why this shape:
- `transmissions` is the immutable event log; `incidents` is a **projection** the summarizer
  rebuilds/updates. You already do this informally — this makes it explicit and queryable.
- `GEOGRAPHY(Point,4326)` + GiST index = the map/heatmap/"near me"/hotspot queries become
  spatial SQL (`ST_DWithin`, `ST_ClusterKMeans`, kNN `<->`) instead of client-side math.
- `incident_events` is the cheap unlock for the "board analytics" you were circling.

---

## 2. Real-time: LISTEN/NOTIFY → SSE (kills polling)

```sql
CREATE OR REPLACE FUNCTION notify_incident_change() RETURNS trigger AS $$
BEGIN
  PERFORM pg_notify('incidents', json_build_object(
    'number', NEW.number, 'op', TG_OP,
    'status_kind', NEW.status_kind, 'priority', NEW.priority
  )::text);
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER incidents_notify
AFTER INSERT OR UPDATE ON incidents
FOR EACH ROW EXECUTE FUNCTION notify_incident_change();
```

FastAPI side (asyncpg listener fanned out to SSE subscribers):

```python
import asyncpg, asyncio
_subs: set[asyncio.Queue] = set()

async def _pg_listener():                      # started in @app.on_event("startup")
    conn = await asyncpg.connect(DSN)
    await conn.add_listener('incidents',
        lambda *_args: [q.put_nowait(_args[3]) for q in list(_subs)])
    while True:
        await asyncio.sleep(3600)              # keep the listener connection alive

@app.get("/api/events", dependencies=[Depends(require_auth)])
async def events(request: Request):
    q = asyncio.Queue(); _subs.add(q)
    async def gen():
        try:
            while not await request.is_disconnected():
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _subs.discard(q)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})
```

Clients open `/api/events` once; on a pushed change they fetch the one changed incident (or the
payload carries enough to patch the board in place). Web/iOS/CarPlay drop their 6s/30s polls.

---

## 3. The query port (this is the bulk of the work)

~40 raw SQL queries move from SQLite to Postgres dialect. Recommend **psycopg3** (sync *and*
async, `%s` params) to stay close to the current `sqlite3` usage. Dialect cheatsheet:

| SQLite | Postgres |
|---|---|
| `?` placeholders | `%s` (psycopg) |
| `INSERT OR IGNORE` | `INSERT ... ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE` | `INSERT ... ON CONFLICT (pk) DO UPDATE SET ...` |
| `datetime('now','localtime','-24 hours')` | `now() - interval '24 hours'` |
| `row["col"]` (sqlite3.Row) | `dict_row` row factory → `row["col"]` |
| `AUTOINCREMENT` | `BIGSERIAL` / `GENERATED ... AS IDENTITY` |
| text dates | `TIMESTAMPTZ` (parse on ingest) |

Wrap it behind the existing `_db()` so most call sites barely change.

---

## 4. Cutover (staged, reversible)

- **P0 — stand up.** `apt install postgresql-16 postgresql-16-postgis-3` (or docker). Create db +
  run the DDL. Zero impact on the live SQLite server.
- **P1 — adapter.** Introduce a `DB_BACKEND` env flag and a thin psycopg `_db()`; port the queries.
- **P2 — migrate data.** One script: copy `geocode_cache` → `geocodes` (compute `geom`); copy
  `incident_state` → `incidents` (join geocodes for `geom`); copy `incident_tx`; rebuild
  `transmissions` from the log (or copy). **`geocode_cache` is the only precious/expensive data
  (Google-billed coords) — migrate it first and verify.**
- **P3 — real-time.** Add the trigger + `/api/events`; switch frontends to SSE (keep polling as
  fallback initially).
- **P4 — shadow-verify.** Run the PG-backed server on a second port; diff `/api/state`,
  `/api/transmissions`, the board, and the map against the SQLite server until they match.
- **P5 — flip.** Point `p25-server.service` at PG. Keep the SQLite db file for ~1 week as rollback
  (`DB_BACKEND=sqlite` reverts instantly).

---

## Effort / risk (honest)

- **Bulk of effort:** the query port (P1) + the migration script (P2). A few days, mechanical.
- **Unchanged:** the LLM summarizer, the geocoding logic, the alert lifecycle, the frontends
  (SSE is an additive swap; they keep working on polling until you cut over).
- **Risk:** it touches the core data layer — but it's run-alongside, shadow-verified, and
  flag-reversible, so the live system is never the experiment.
- **Optional follow-ons (not now):** offload alerting to **Keep** (Grafana OnCall OSS is archived
  as of 2026-03-24); point **Grafana** at PG for hotspot/time-of-day dashboards.

## Do NOT
- Adopt a Jira/board product (Plane/Huly/OpenProject) — wrong grain for auto-generated,
  auto-clearing, high-churn incidents.
