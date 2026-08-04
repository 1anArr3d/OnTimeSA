import type {
  BunchingEventOut,
  DirectionSummary,
  LiveVehicle,
  ReliabilityStats,
  RouteShapeDirection,
  RouteSummary,
  StopSummary,
} from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(API_BASE_URL + path);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
    }
  }
  const response = await fetch(url.toString());
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.detail ?? `Request failed: ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  listRoutes: () => get<RouteSummary[]>("/api/routes"),

  listRouteStops: (routeId: string) => get<StopSummary[]>(`/api/routes/${encodeURIComponent(routeId)}/stops`),

  listRouteDirections: (routeId: string) =>
    get<DirectionSummary[]>(`/api/routes/${encodeURIComponent(routeId)}/directions`),

  getRouteShape: (routeId: string) =>
    get<RouteShapeDirection[]>(`/api/routes/${encodeURIComponent(routeId)}/shape`),

  getSegmentReliability: (params: {
    route_id: string;
    start_stop_id: string;
    end_stop_id: string;
    start_date?: string;
    end_date?: string;
  }) => get<ReliabilityStats>("/api/reliability/segment", params),

  getWorstOffenders: (params: {
    group_by?: "route" | "stop";
    route_id?: string;
    start_date?: string;
    end_date?: string;
    limit?: number;
    min_samples?: number;
  }) => get<ReliabilityStats[]>("/api/reliability/worst-offenders", params),

  listBunchingEvents: (params: {
    route_id?: string;
    start_date?: string;
    end_date?: string;
    limit?: number;
  }) => get<BunchingEventOut[]>("/api/bunching-events", params),

  listLiveVehicles: (maxAgeSeconds?: number) =>
    get<LiveVehicle[]>("/api/vehicles/live", { max_age_seconds: maxAgeSeconds }),
};
