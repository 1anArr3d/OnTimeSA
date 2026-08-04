import { useEffect, useMemo, useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api/client";
import type { ReliabilityStats } from "../api/types";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { BunchingSummary } from "../components/BunchingSummary";
import { ErrorState, LoadingState, EmptyState } from "../components/States";
import { formatDelay } from "../lib/delayStatus";
import { isoDateDaysAgo, todayIso } from "../lib/dates";

type GroupBy = "route" | "stop";
type RangePreset = "7" | "30" | "90";

export function Dashboard() {
  const [groupBy, setGroupBy] = useState<GroupBy>("route");
  const [rangeDays, setRangeDays] = useState<RangePreset>("30");
  const [data, setData] = useState<ReliabilityStats[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const startDate = useMemo(() => isoDateDaysAgo(Number(rangeDays)), [rangeDays]);
  const endDate = useMemo(() => todayIso(), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .getWorstOffenders({ group_by: groupBy, start_date: startDate, end_date: endDate, limit: 10 })
      .then((result) => {
        if (!cancelled) {
          setData(result);
          setError(null);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load reliability data");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [groupBy, startDate, endDate]);

  const chartData = (data ?? []).map((row) => ({
    label: groupBy === "route" ? row.route_short_name ?? row.route_id ?? "?" : row.end_stop_name ?? row.end_stop_id ?? "?",
    on_time_pct: row.on_time_pct ?? 0,
    raw: row,
  }));

  return (
    <div className="page">
      <h1>Reliability Dashboard</h1>
      <p className="page-subtitle">
        Worst on-time performance over the last {rangeDays} days, ranked from real GTFS-RT vs. schedule comparisons.
      </p>

      <div style={{ display: "flex", gap: 12, marginBottom: 20 }}>
        <div className="card" style={{ display: "flex", gap: 4, padding: 4 }}>
          {(["route", "stop"] as GroupBy[]).map((g) => (
            <button
              key={g}
              className={groupBy === g ? "" : "secondary"}
              onClick={() => setGroupBy(g)}
              style={{ textTransform: "capitalize" }}
            >
              By {g}
            </button>
          ))}
        </div>
        <div className="card" style={{ display: "flex", gap: 4, padding: 4 }}>
          {(["7", "30", "90"] as RangePreset[]).map((r) => (
            <button key={r} className={rangeDays === r ? "" : "secondary"} onClick={() => setRangeDays(r)}>
              {r}d
            </button>
          ))}
        </div>
      </div>

      {loading && <LoadingState label="Crunching reliability numbers..." />}
      {error && <ErrorState message={error} />}
      {!loading && !error && data && data.length === 0 && (
        <EmptyState message="Not enough data yet for this window - check back after more polling cycles." />
      )}

      {!loading && !error && data && data.length > 0 && (
        <>
          <div className="card" style={{ marginBottom: 20 }}>
            <ResponsiveContainer width="100%" height={Math.max(220, chartData.length * 44)}>
              <BarChart data={chartData} layout="vertical" margin={{ left: 24, right: 24 }}>
                <CartesianGrid horizontal={false} stroke="var(--gridline)" />
                <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 11, fill: "var(--text-muted)" }} unit="%" />
                <YAxis
                  type="category"
                  dataKey="label"
                  width={140}
                  tick={{ fontSize: 12, fill: "var(--text-primary)" }}
                />
                <Tooltip
                  formatter={(value, _name, item) => {
                    const numericValue = typeof value === "number" ? value : Number(value);
                    const sampleCount = (item?.payload as { raw?: ReliabilityStats })?.raw?.sample_count ?? "?";
                    return [`${numericValue.toFixed(1)}% on-time (n=${sampleCount})`, item?.payload?.label ?? ""];
                  }}
                  contentStyle={{ background: "var(--surface-1)", border: "1px solid var(--border)", fontSize: 12 }}
                />
                <Bar dataKey="on_time_pct" radius={[0, 4, 4, 0]} maxBarSize={24}>
                  {chartData.map((entry) => (
                    <Cell key={entry.label} fill="var(--series-1)" />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>

          <p style={{ fontSize: 12, color: "var(--text-muted)", marginTop: -8, marginBottom: 12 }}>
            Average delay and on-time % can disagree - a route with trips running both well early and well late
            averages out close to zero while still being unreliable. On-time % (within{" "}
            {"-1 to +5 min of schedule"}) is the more trustworthy reliability signal of the two.
          </p>

          <table>
            <thead>
              <tr>
                <th>{groupBy === "route" ? "Route" : "Stop"}</th>
                <th>On-time %</th>
                <th>Avg delay</th>
                <th>Bunching events</th>
                <th>Confidence</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row) => (
                <tr key={row.route_id ?? row.end_stop_id}>
                  <td>{groupBy === "route" ? row.route_short_name ?? row.route_id : row.end_stop_name ?? row.end_stop_id}</td>
                  <td>{row.on_time_pct?.toFixed(1) ?? "—"}%</td>
                  <td>{formatDelay(row.avg_delay_seconds)}</td>
                  <td>{row.bunching_event_count}</td>
                  <td>
                    <ConfidenceBadge confidence={row.confidence} sampleCount={row.sample_count} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      <div style={{ marginTop: 20 }}>
        <BunchingSummary startDate={startDate} endDate={endDate} />
      </div>
    </div>
  );
}
