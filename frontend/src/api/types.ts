// Mirrors backend/app/schemas.py - keep in sync manually (no shared codegen yet).

export type Confidence = "low" | "high";
export type ReliabilityScope = "segment" | "route" | "stop";

export interface ReliabilityStats {
  scope: ReliabilityScope;
  route_id: string | null;
  route_short_name: string | null;
  route_long_name: string | null;
  direction_id: number | null;

  start_stop_id: string | null;
  start_stop_name: string | null;
  end_stop_id: string | null;
  end_stop_name: string | null;

  start_date: string;
  end_date: string;

  sample_count: number;
  avg_delay_seconds: number | null;
  on_time_pct: number | null;
  bunching_event_count: number;
  bunching_events_per_day: number;

  confidence: Confidence;
}

export interface RouteSummary {
  route_id: string;
  route_short_name: string | null;
  route_long_name: string | null;
}

export interface StopSummary {
  stop_id: string;
  stop_name: string | null;
  stop_lat: number | null;
  stop_lon: number | null;
  direction_id: number | null;
  stop_sequence: number | null;
}

export interface DirectionSummary {
  direction_id: number;
  headsign: string | null;
}

export interface ShapePoint {
  lat: number;
  lon: number;
}

export interface RouteShapeDirection {
  direction_id: number;
  points: ShapePoint[];
}

export type BunchingSeverity = "low" | "medium" | "high";

export interface BunchingEventOut {
  id: number;
  route_id: string;
  route_short_name: string | null;
  direction_id: number | null;
  start_time: string;
  end_time: string;
  location_lat: number | null;
  location_lon: number | null;
  nearest_stop_id: string | null;
  nearest_stop_name: string | null;
  observed_headway_seconds: number;
  scheduled_headway_seconds: number;
  severity: BunchingSeverity;
}

export type VehicleStatus = "STOPPED_AT" | "INCOMING_AT" | "IN_TRANSIT_TO" | null;

export interface LiveVehicle {
  vehicle_id: string;
  trip_id: string | null;
  route_id: string | null;
  route_short_name: string | null;
  direction_id: number | null;
  trip_headsign: string | null;
  latitude: number | null;
  longitude: number | null;
  bearing: number | null;
  current_status: VehicleStatus;
  vehicle_timestamp: string | null;
  delay_seconds: number | null;
  current_stop_id: string | null;
  current_stop_name: string | null;
  next_stop_id: string | null;
  next_stop_name: string | null;
  next_stop_scheduled_time: string | null;
  scheduled_time: string | null;
  actual_time: string | null;
}
