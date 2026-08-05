# OnTimeSA

A reliability-tracking dashboard for VIA Metropolitan Transit's (San Antonio) bus
network: live vehicle positions, historical on-time performance by route/stop,
bus-bunching detection, and a personalized "check my commute" reliability lookup
for a single route segment.

Google Maps and VIA's own trip planner already answer "where's my bus right now."
This project answers a different question they don't: **how has this route
actually performed**, based purely on comparing VIA's GTFS-Realtime feed against
its published GTFS static schedule over time. It does not make causal claims about
*why* a route is unreliable (weather, traffic signals, etc.) - only what the data
shows.

Data provided by VIA Metropolitan Transit, via their [documented GTFS / GTFS-Realtime
feeds](https://www.viainfo.net/developers-resources/). No other data source is used.

## Architecture

```
backend/    FastAPI + SQLAlchemy + Postgres (Neon)
  app/gtfs/       GTFS static parsing/loading, GTFS-RT polling & decoding,
                  timezone normalization, stop matching, deviation calc,
                  bunching detection
  app/api/        FastAPI routers
  app/*_service.py  Query/aggregation logic behind each router
  app/poller.py   One GTFS-RT poll cycle; also the recurring-job entrypoint
  tests/          pytest suite (parsing, matching, timezone math, reliability
                  aggregation, bunching detection)

frontend/   React + TypeScript + Vite
  src/pages/      LiveMap, Dashboard, CheckMyCommute
  src/api/        Typed fetch client against the backend
  src/components/ Shared UI (confidence badges, status badges, bunching
                  summary card, nav)
```

### Data model

- **Static schedule** (`routes`, `stops`, `trips`, `stop_times`, `calendar`) -
  loaded from VIA's GTFS static zip, refreshed periodically. Upserted, not
  truncated-and-reloaded, so historical rows in the tables below never lose
  their foreign keys when a trip drops out of a refreshed feed.
- **`vehicle_position_snapshots`** - one row per polled vehicle position.
  Time-series, never overwritten.
- **`schedule_deviations`** - computed delay per (trip, stop, event), tagged
  with which GTFS-RT feed it came from and how confidently the stop was
  matched (`exact_sequence` / `exact_stop_id` / `nearest_geographic`).
- **`headway_samples`** - every consecutive-vehicle-pair headway comparison
  computed during bunching detection, logged whether or not it crossed the
  bunching threshold - kept so the threshold can be tuned against real
  headway variance later instead of guessed upfront.
- **`bunching_events`** - the subset of headway samples that crossed the
  threshold, merged across polls into a single ongoing event rather than
  duplicated every cycle.
- **`daily_route_stats`** - one row per (route, service_date), computed
  nightly from that day's `schedule_deviations`/`bunching_events` before
  those raw rows age out of the 5-day retention window. Reliability queries
  fall back to this table for any part of a requested date range older than
  the retention window - see the storage-cap changelog entry below.

## Setup

### Prerequisites

- Python 3.12+, Node 20+
- A Postgres database (developed against [Neon](https://neon.tech))

### Backend

```bash
cd backend
python -m venv ../.venv          # or use an existing venv
../.venv/Scripts/activate        # Windows; `source ../.venv/bin/activate` on Unix
pip install -r requirements.txt

cp .env.example .env
# edit .env: set SATP_DATABASE_URL to your Postgres connection string
```

Load the static schedule and create tables for the first time:

```bash
python -c "
from app.db import Base, get_engine
from app import models
Base.metadata.create_all(get_engine())
"
python -c "
from app.db import get_sessionmaker
from app.gtfs.static import fetch_and_parse
from app.gtfs.loader import load_static_feed
session = get_sessionmaker()()
load_static_feed(session, fetch_and_parse())
"
```

Run the API:

```bash
uvicorn app.main:app --reload --port 8000
```

Run the test suite:

```bash
pytest                      # everything, including one live network call to VIA
pytest -m "not integration" # skip the live-feed smoke test
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env   # defaults to http://localhost:8000, edit if the API lives elsewhere
npm run dev
```

Open `http://localhost:5173`.

## Scheduled ingestion jobs

Two independent refresh cycles are needed in any real deployment - neither
runs automatically outside of what's described here.

**GTFS static refresh** (daily is plenty - VIA's schedule doesn't change
intra-day): re-run the same two-line loader snippet from setup above (fetch +
`load_static_feed`) on a cron/scheduled task. It's a safe upsert, not a
destructive reload.

**GTFS-RT polling** (this project defaults to 120s via
`SATP_GTFS_RT_POLL_SECONDS` - see the storage-cap changelog entry below for
why): this is what populates vehicle positions, schedule deviations, and
bunching detection. Two ways to run it:

```bash
# Option A: built-in APScheduler loop, blocks forever
python -m app.poller  # single one-off poll
python -c "from app.poller import run_scheduler; run_scheduler()"  # recurring

# Option B: external scheduler (cron, systemd timer, etc.) calling a single poll
python -m app.poller
```

Prefer option B (external scheduler) for anything long-running in production -
it survives a poll cycle hanging or crashing without taking the whole process
down, which `run_scheduler()`'s in-process loop won't.

**Daily rollup + pruning** (00:10 America/Chicago, also via `run_scheduler()`
or callable directly as `app.rollup_service.run_daily_maintenance()`): rolls
yesterday's raw data into `daily_route_stats`, then prunes raw rows older
than `SATP_RAW_DATA_RETENTION_DAYS` (default 5). See the changelog entry
below for why this exists.

**A note on data freshness:** the Dashboard only reflects however long the
poller has actually been running continuously. A fresh deployment that's only
been polling for a few hours will show a sparse or empty bunching-events card
and low `n=` sample counts on the reliability lookups - that's expected, not
broken. The bunching events card's empty state says as much directly, rather
than looking like a bug. The reliability numbers (and especially bunching
frequency, which needs enough headway samples to be statistically meaningful)
get materially more trustworthy after the poller's been running for days/weeks,
not hours.

### Changelog: fixed a storage runaway bug (2026-08-03)

Early testing surfaced that `schedule_deviations` was growing at ~510K
rows/hour (~2+ GB/day) - VIA's TripUpdates feed sends predictions for every
remaining stop on a trip, not just the next one, and the original code
inserted a new row for each one on every poll, even for stops the vehicle
was still an hour away from. Fixed by upserting on
`(trip_id, stop_sequence, service_date, event_type)` instead of always
inserting - each stop-visit now gets exactly one row that refines as
predictions firm up and stops changing once the vehicle passes it. Verified
by watching a single stop's row across 4 consecutive polls: same row `id`
throughout, value stable, and total table growth dropped from ~17,000
new rows/poll to ~40. The pre-fix table (638K redundant rows) was truncated
as part of this fix - no real historical signal was lost, since none of those
rows represented data older than a few hours.

### Changelog: rollup + pruning for Neon free-tier storage cap (2026-08-04)

Modeled raw time-series growth (`vehicle_position_snapshots` +
`schedule_deviations`) against Neon's free-tier 500 MB cap and found
keeping raw data forever wasn't sustainable - at the original 45s poll
interval those two tables alone were growing ~30 MB/day (measured directly:
39 MB + 22 MB over ~46 hours of real polling), which would exhaust the cap
within days. Implemented the standard time-series fix rather than raw
retention:

- **Poll interval 45s -> 120s** (`SATP_GTFS_RT_POLL_SECONDS`) - a ~2.7x
  reduction in raw write rate, still frequent enough to catch bunching
  events, which in every real event detected so far played out over
  multiple minutes.
- **`daily_route_stats`** - one row per route per service day
  (`on_time_pct`, `avg_delay_seconds`, `bunching_event_count`,
  `sample_count`), computed by a nightly job (00:10 America/Chicago) from
  that day's raw rows.
- **5-day raw retention** - a nightly job bulk-deletes
  `vehicle_position_snapshots`/`schedule_deviations` rows older than 5 days.
  `headway_samples` and `bunching_events` are never pruned - low-volume and
  the actual event log, respectively. The rollup job always runs before the
  prune job (same nightly cycle, rollup first), so a day is never pruned
  before it's been rolled up.
- **Reliability queries now blend both** - `/api/reliability/segment` and
  `/api/reliability/worst-offenders` query raw data directly for the last 5
  days and fall back to `daily_route_stats` for anything older, combining
  sample counts/weighted averages across the split so the existing
  confidence-flag behavior carries through unchanged either way.

Verified against the real Neon instance: rolled up a real day (88 routes)
and cross-checked one route's rollup row against a direct raw aggregation
query (sample count, avg delay, and on-time % matched exactly); confirmed
`/api/reliability/worst-offenders` returns identical numbers whether queried
over a 2-day window (raw-only path) or a 35-day window spanning into the
rollup range (blended path), since there was no older data yet for the
rollup portion to add; confirmed the prune job deletes 0 rows when nothing
is actually older than the retention window. Current table sizes (`\dt+`
equivalent): `stop_times` (static schedule) is actually the single largest
table at 146 MB, well ahead of either time-series table - the raw+rollup
fix caps the part of storage that grows unboundedly over time, not the
static schedule data, which is upserted in place and doesn't grow with
uptime.

### Changelog: skip the database entirely on empty overnight poll cycles (2026-08-05)

Queried the real static schedule rather than assuming fixed hours: every
service day (weekday, Saturday, Sunday all checked separately) has a
genuine ~2-hour window with zero scheduled trips (e.g. weekdays 1:56 AM -
3:42 AM America/Chicago) - verified directly against `stop_times`, not just
inferred from `calendar.txt`'s min/max. The poller was still polling and
writing every 120s through that window purely to record ~400 parked/off-
service vehicles nobody reads history for, which also meant Neon's compute
(idles after 5 min with no connections) never got the chance to scale to
zero.

Fixed by checking each poll's *own* fetched data, not a predicted schedule:
if zero vehicles have an active `trip_id` and TripUpdates is empty,
`poll_once()` returns before ever touching the database session - not just
skipping writes, skipping the connection itself, since SQLAlchemy doesn't
open one until the first query. Deliberately not the "predict next
departure from the static schedule and sleep until then" design considered
initially - that requires trusting the static schedule's timing exactly
right (this project doesn't even ingest `calendar_dates.txt`, so it has no
way to know about schedule exceptions in advance) and risks sleeping through
an earlier-than-expected resumption. Checking the real feed every cycle
instead means a feed outage during genuine service hours and an early
resumption both self-correct on the very next poll rather than depending on
a prediction.

Verified against the real system: confirmed via SQLAlchemy connection-pool
events that an empty-feed poll checks out zero Postgres connections (a
normal poll checks out one, confirming the instrumentation itself is
valid); confirmed a poll with off-service vehicles present but zero active
trips still skips correctly; confirmed a poll with vehicles empty but
TripUpdates non-empty correctly does *not* skip; ran a skipped cycle
immediately followed by a real one (service happened to resume mid-
verification) and got a clean 4,018-deviation write with no stale state.
The nightly rollup/prune job is on its own independent APScheduler cron
trigger and was untouched.

## Roadmap (explicitly out of scope for this version)

These were deliberately not built, to keep this version's scope to reliability
tracking rather than trip planning:

- **Multi-route trip comparison** - comparing a direct route against an
  alternative with a transfer (A→B direct vs. A→C→B). This version is
  single-route, no-transfer only, by design.
- **Route optimization / suggestion** - recommending a faster alternative
  route using a graph/pathfinding algorithm over live reliability data. Real,
  substantial future work; not attempted here.
- **Turn-by-turn "where's my bus" ETAs** - already solved well by Google Maps
  and the Transit app; this project is a historical-reliability complement to
  those, not a competitor.
- **Causal analysis of delays** (weather, traffic signals, etc.) - this
  version reports GTFS-RT-vs-schedule comparisons only, no causal claims.
- **`calendar_dates.txt` (service exceptions) is not ingested** - only
  `calendar.txt`'s recurring weekly pattern is parsed (`app/gtfs/static.py`,
  `app/models/gtfs_static.py`). Holiday closures, single-day added/removed
  service, and other calendar exceptions VIA might publish aren't reflected
  anywhere in this system - the static schedule refresh would only catch a
  service change if VIA republishes `calendar.txt`/`trips.txt` outright, not
  a targeted exception. Known gap, not fixed yet.

## Notable finding

Route 103 (Primo Zarzamora) has run at roughly 40% on-time performance across
500+ real observations during development of this project - a concrete,
honestly-surfaced example of exactly what this tool is for.

## License / attribution

Transit data is provided by VIA Metropolitan Transit under their [Developer's
License Agreement](https://www.viainfo.net/developers-resources/). VIA does
not guarantee the accuracy, completeness, or availability of the underlying
data.
