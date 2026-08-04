import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../api/client";
import type { DirectionSummary, ReliabilityStats, RouteSummary, StopSummary } from "../api/types";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { ErrorState, LoadingState } from "../components/States";
import { formatDelay } from "../lib/delayStatus";
import { isoDateDaysAgo, todayIso } from "../lib/dates";

/**
 * VIA's trip_headsign sometimes includes a redundant leading route number
 * ("20 - Downtown/Centro Plaza"), sometimes doesn't ("Brooks Transit
 * Center") - strip it when present so the label doesn't repeat the route
 * the rider already picked. Falls back to the raw direction_id only if no
 * headsign was published at all.
 */
function formatDirectionLabel(direction: DirectionSummary, routeShortName: string | null | undefined): string {
  if (!direction.headsign) return `Direction ${direction.direction_id}`;
  if (routeShortName) {
    const prefixPattern = new RegExp(`^${routeShortName}\\s*-\\s*`, "i");
    return direction.headsign.replace(prefixPattern, "");
  }
  return direction.headsign;
}

export function CheckMyCommute() {
  const [routes, setRoutes] = useState<RouteSummary[]>([]);
  const [routeId, setRouteId] = useState("");
  const [stops, setStops] = useState<StopSummary[]>([]);
  const [directions, setDirections] = useState<DirectionSummary[]>([]);
  const [direction, setDirection] = useState<number | null>(null);
  const [startStopId, setStartStopId] = useState("");
  const [endStopId, setEndStopId] = useState("");
  const [rangeDays, setRangeDays] = useState<"7" | "30" | "90">("30");

  const [result, setResult] = useState<ReliabilityStats | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.listRoutes().then(setRoutes).catch(() => {});
  }, []);

  useEffect(() => {
    setStops([]);
    setDirections([]);
    setDirection(null);
    setStartStopId("");
    setEndStopId("");
    setResult(null);
    if (!routeId) return;
    api.listRouteStops(routeId).then(setStops).catch(() => {});
    api.listRouteDirections(routeId).then(setDirections).catch(() => {});
  }, [routeId]);

  const selectedRoute = routes.find((r) => r.route_id === routeId);

  const stopsForDirection = useMemo(
    () =>
      stops
        .filter((s) => s.direction_id === direction)
        .sort((a, b) => (a.stop_sequence ?? 0) - (b.stop_sequence ?? 0)),
    [stops, direction]
  );

  const startSequence = stopsForDirection.find((s) => s.stop_id === startStopId)?.stop_sequence ?? null;
  const endStopOptions = startSequence === null ? [] : stopsForDirection.filter((s) => (s.stop_sequence ?? 0) > startSequence);

  async function checkReliability() {
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      const stats = await api.getSegmentReliability({
        route_id: routeId,
        start_stop_id: startStopId,
        end_stop_id: endStopId,
        start_date: isoDateDaysAgo(Number(rangeDays)),
        end_date: todayIso(),
      });
      setResult(stats);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to look up reliability for this commute");
    } finally {
      setLoading(false);
    }
  }

  const canCheck = routeId && startStopId && endStopId;

  return (
    <div className="page">
      <h1>Check My Commute</h1>
      <p className="page-subtitle">
        Pick your route and the stops you board/exit at - see how that exact segment has actually performed, based on
        real GTFS-RT history (single route only, no transfers).
      </p>

      <div className="card" style={{ display: "flex", flexDirection: "column", gap: 12, maxWidth: 420 }}>
        <label>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Route</div>
          <select value={routeId} onChange={(e) => setRouteId(e.target.value)} style={{ width: "100%" }}>
            <option value="">Select a route…</option>
            {routes.map((r) => (
              <option key={r.route_id} value={r.route_id}>
                {r.route_short_name ?? r.route_id} - {r.route_long_name}
              </option>
            ))}
          </select>
        </label>

        {directions.length > 0 && (
          <label>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Direction</div>
            <select
              value={direction ?? ""}
              onChange={(e) => {
                setDirection(e.target.value === "" ? null : Number(e.target.value));
                setStartStopId("");
                setEndStopId("");
              }}
              style={{ width: "100%" }}
            >
              <option value="">Select a direction…</option>
              {directions.map((d) => (
                <option key={d.direction_id} value={d.direction_id}>
                  {formatDirectionLabel(d, selectedRoute?.route_short_name)}
                </option>
              ))}
            </select>
          </label>
        )}

        {direction !== null && (
          <label>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Board at</div>
            <select
              value={startStopId}
              onChange={(e) => {
                setStartStopId(e.target.value);
                setEndStopId("");
              }}
              style={{ width: "100%" }}
            >
              <option value="">Select a stop…</option>
              {stopsForDirection.map((s) => (
                <option key={s.stop_id} value={s.stop_id}>
                  {s.stop_name}
                </option>
              ))}
            </select>
          </label>
        )}

        {startStopId && (
          <label>
            <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Exit at</div>
            <select value={endStopId} onChange={(e) => setEndStopId(e.target.value)} style={{ width: "100%" }}>
              <option value="">Select a stop…</option>
              {endStopOptions.map((s) => (
                <option key={s.stop_id} value={s.stop_id}>
                  {s.stop_name}
                </option>
              ))}
            </select>
          </label>
        )}

        <label>
          <div style={{ fontSize: 12, color: "var(--text-muted)", marginBottom: 4 }}>Time window</div>
          <div style={{ display: "flex", gap: 4 }}>
            {(["7", "30", "90"] as const).map((r) => (
              <button key={r} className={rangeDays === r ? "" : "secondary"} onClick={() => setRangeDays(r)}>
                {r}d
              </button>
            ))}
          </div>
        </label>

        <button disabled={!canCheck || loading} onClick={checkReliability}>
          {loading ? "Checking…" : "Check reliability"}
        </button>
      </div>

      {loading && <div style={{ marginTop: 20 }}><LoadingState label="Aggregating historical delays for this segment..." /></div>}
      {error && <div style={{ marginTop: 20 }}><ErrorState message={error} /></div>}

      {result && !loading && (
        <div className="card" style={{ marginTop: 20, maxWidth: 480 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 12 }}>
            <div>
              <div style={{ fontWeight: 700 }}>
                Route {result.route_short_name} - {result.start_stop_name} → {result.end_stop_name}
              </div>
              <div style={{ fontSize: 12, color: "var(--text-muted)" }}>
                {result.start_date} to {result.end_date}
              </div>
            </div>
            <ConfidenceBadge confidence={result.confidence} sampleCount={result.sample_count} />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
            <StatTile label="Avg delay" value={formatDelay(result.avg_delay_seconds)} />
            <StatTile
              label="On-time %"
              value={result.on_time_pct !== null ? `${result.on_time_pct.toFixed(0)}%` : "—"}
            />
            <StatTile label="Bunching events" value={String(result.bunching_event_count)} />
            <StatTile label="Bunching / day" value={result.bunching_events_per_day.toFixed(2)} />
          </div>
        </div>
      )}
    </div>
  );
}

function StatTile({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.03em" }}>
        {label}
      </div>
      <div style={{ fontSize: 24, fontWeight: 700 }}>{value}</div>
    </div>
  );
}
