import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { BunchingEventOut } from "../api/types";
import { BunchingSeverityBadge } from "./StatusBadge";
import { ErrorState, LoadingState } from "./States";

const PREVIEW_COUNT = 5;
const FETCH_LIMIT = 100; // enough to report an accurate count for any realistic window

function formatDuration(startIso: string, endIso: string): string {
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  const minutes = Math.max(1, Math.round(ms / 60000));
  return `${minutes} min`;
}

/**
 * Folded into the Dashboard rather than given its own nav page/route -
 * bunching events are often 0-few per day, and a whole page that's usually
 * empty reads as "broken" rather than "working correctly." As a card here,
 * a quiet day just looks like a small, calm card next to the rest of the
 * dashboard instead of a dead-looking page.
 */
export function BunchingSummary({ startDate, endDate }: { startDate: string; endDate: string }) {
  const [events, setEvents] = useState<BunchingEventOut[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listBunchingEvents({ start_date: startDate, end_date: endDate, limit: FETCH_LIMIT })
      .then((result) => {
        if (!cancelled) {
          setEvents(result);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load bunching events");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [startDate, endDate]);

  return (
    <div className="card">
      <div style={{ fontWeight: 700, marginBottom: 4 }}>Bunching events</div>
      <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 0, marginBottom: 12 }}>
        Consecutive vehicles on the same route running well under their scheduled headway - detected from real
        GTFS-RT headway comparisons, not just proximity (terminal layovers are filtered out).
      </p>

      {loading && <LoadingState label="Loading..." />}
      {error && <ErrorState message={error} />}

      {!loading && !error && events && events.length === 0 && (
        <p style={{ fontSize: 13, color: "var(--text-secondary)" }}>
          No bunching detected in this window - either a good sign, or check back after more data accumulates.
        </p>
      )}

      {!loading && !error && events && events.length > 0 && (
        <>
          <p style={{ fontSize: 13, marginTop: 0 }}>
            <strong>{events.length}</strong> event{events.length === 1 ? "" : "s"} in this window
            {events.length >= FETCH_LIMIT ? " (100+, showing recent)" : ""}.
          </p>
          <table>
            <thead>
              <tr>
                <th>Route</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Near stop</th>
                <th>Severity</th>
              </tr>
            </thead>
            <tbody>
              {events.slice(0, PREVIEW_COUNT).map((e) => (
                <tr key={e.id}>
                  <td>{e.route_short_name ?? e.route_id}</td>
                  <td>{new Date(e.start_time).toLocaleString()}</td>
                  <td>{formatDuration(e.start_time, e.end_time)}</td>
                  <td>{e.nearest_stop_name ?? "—"}</td>
                  <td>
                    <BunchingSeverityBadge severity={e.severity} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {events.length > PREVIEW_COUNT && (
            <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 8, marginBottom: 0 }}>
              +{events.length - PREVIEW_COUNT} more not shown.
            </p>
          )}
        </>
      )}
    </div>
  );
}
