// Mirrors the on-time band in backend/app/config.py (on_time_early/late_threshold_seconds).
const ON_TIME_EARLY_SECONDS = -60;
const ON_TIME_LATE_SECONDS = 300;
const VERY_LATE_SECONDS = 900; // 15 min
const VERY_EARLY_SECONDS = -300; // 5 min

export type DelayStatus = "unknown" | "on_time" | "late" | "very_late" | "early";

export function classifyDelay(delaySeconds: number | null | undefined): DelayStatus {
  if (delaySeconds === null || delaySeconds === undefined) return "unknown";
  if (delaySeconds >= ON_TIME_EARLY_SECONDS && delaySeconds <= ON_TIME_LATE_SECONDS) return "on_time";
  if (delaySeconds > VERY_LATE_SECONDS) return "very_late";
  if (delaySeconds > ON_TIME_LATE_SECONDS) return "late";
  if (delaySeconds < VERY_EARLY_SECONDS) return "early";
  return "early";
}

export const DELAY_STATUS_COLOR: Record<DelayStatus, string> = {
  unknown: "var(--status-unknown)",
  on_time: "var(--status-good)",
  late: "var(--status-warning)",
  very_late: "var(--status-critical)",
  // Distinct from "late" - same warning tier of severity, opposite direction.
  // Was previously also status-warning, making early/late indistinguishable
  // on the map (the whole point of the color coding).
  early: "var(--status-serious)",
};

export const DELAY_STATUS_LABEL: Record<DelayStatus, string> = {
  unknown: "Unknown",
  on_time: "On time",
  late: "Late",
  very_late: "Very late",
  early: "Early",
};

export function formatDelay(delaySeconds: number | null | undefined): string {
  if (delaySeconds === null || delaySeconds === undefined) return "—";
  const minutes = Math.round(delaySeconds / 60);
  if (minutes === 0) return "on time";
  return minutes > 0 ? `${minutes} min late` : `${Math.abs(minutes)} min early`;
}
