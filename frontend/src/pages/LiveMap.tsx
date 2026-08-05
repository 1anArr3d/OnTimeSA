import { useEffect, useState } from "react";
import L from "leaflet";
import { CircleMarker, MapContainer, Marker, Pane, Polyline, Popup, TileLayer } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import { api } from "../api/client";
import type { LiveVehicle, RouteShapeDirection, RouteSummary, StopSummary } from "../api/types";
import { classifyDelay, DELAY_STATUS_COLOR, DELAY_STATUS_LABEL, formatDelay } from "../lib/delayStatus";
import { ErrorState } from "../components/States";

const SAN_ANTONIO_CENTER: [number, number] = [29.4241, -98.4936];
const POLL_INTERVAL_MS = 30_000;

const LEGEND_ITEMS: Array<keyof typeof DELAY_STATUS_LABEL> = ["on_time", "late", "very_late", "early", "unknown"];

/**
 * Icon shape reflects GTFS-RT current_status, not just whether bearing
 * happens to be present (a stopped vehicle still reports its last heading,
 * so "has a bearing" isn't a reliable stand-in for "is moving"):
 * - STOPPED_AT: circle - vehicle is stationary at a stop, no direction to show.
 * - IN_TRANSIT_TO / INCOMING_AT (or unrecognized): directional chevron
 *   rotated to bearing when known, falling back to a plain circle only if
 *   bearing itself is missing.
 *
 * "Unknown" vehicles (no active trip - e.g. hundreds of buses parked at the
 * overnight depot, which VIA's feed reports at nearly identical
 * coordinates) get a much smaller, low-opacity dot with no stroke/shadow.
 * At full size, a dense cluster of these compounds into a solid dark blob
 * (overlapping dark outlines + drop-shadows stacking) - since these carry
 * zero route/delay signal anyway, de-emphasizing them fixes the blob
 * without needing full marker clustering.
 */
function createVehicleIcon(
  color: string,
  bearing: number | null,
  isUnknown: boolean,
  currentStatus: string | null
): L.DivIcon {
  if (isUnknown) {
    return L.divIcon({
      html: `<svg width="9" height="9" viewBox="0 0 9 9"><circle cx="4.5" cy="4.5" r="3.5" fill="${color}" fill-opacity="0.6" /></svg>`,
      className: "vehicle-icon",
      iconSize: [9, 9],
      iconAnchor: [4.5, 4.5],
    });
  }

  const hasHeading = bearing !== null && bearing !== undefined;
  const isStopped = currentStatus === "STOPPED_AT";
  const showChevron = !isStopped && hasHeading;
  // A lighter, thinner stroke than pure black/white - enough contrast
  // against OSM's pale basemap without the heavier version's shadow-stacking
  // blob effect when several markers overlap.
  const html = showChevron
    ? `<svg width="24" height="24" viewBox="0 0 24 24" style="transform: rotate(${bearing}deg)">
         <polygon points="12,3 19,20 12,16 5,20" fill="${color}" stroke="#4a4a4a" stroke-width="1" stroke-linejoin="round" />
       </svg>`
    : `<svg width="18" height="18" viewBox="0 0 18 18">
         <circle cx="9" cy="9" r="6.5" fill="${color}" stroke="#4a4a4a" stroke-width="1.25" />
       </svg>`;
  const size = showChevron ? 24 : 18;
  return L.divIcon({
    html,
    className: "vehicle-icon",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

function VehiclePopup({ v }: { v: LiveVehicle }) {
  const status = classifyDelay(v.delay_seconds);
  return (
    <div style={{ fontSize: 13, lineHeight: 1.5 }}>
      <strong>Vehicle {v.vehicle_id}</strong>
      {v.route_short_name && (
        <div>
          Route {v.route_short_name}
          {v.trip_headsign && <> · {v.trip_headsign}</>}
        </div>
      )}
      <div>
        {DELAY_STATUS_LABEL[status]} ({formatDelay(v.delay_seconds)})
      </div>
      {v.current_status && <div style={{ color: "#666" }}>{v.current_status.replaceAll("_", " ")}</div>}

      {v.current_stop_name && (
        <div style={{ marginTop: 6, paddingTop: 6, borderTop: "1px solid #ddd" }}>
          <div>
            <strong>Current/nearest stop:</strong> {v.current_stop_name}
          </div>
          {v.scheduled_time && (
            <div style={{ color: "#666" }}>
              Scheduled {v.scheduled_time}{v.actual_time && <> · actual {v.actual_time}</>}
            </div>
          )}
        </div>
      )}
      {v.next_stop_name && (
        <div style={{ marginTop: 4 }}>
          <strong>Next stop:</strong> {v.next_stop_name}
          {v.next_stop_scheduled_time && <> ({v.next_stop_scheduled_time})</>}
        </div>
      )}
    </div>
  );
}

export function LiveMap() {
  const [vehicles, setVehicles] = useState<LiveVehicle[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);

  const [routes, setRoutes] = useState<RouteSummary[]>([]);
  const [selectedRouteId, setSelectedRouteId] = useState("");
  const [routeStops, setRouteStops] = useState<StopSummary[]>([]);
  const [routeShapes, setRouteShapes] = useState<RouteShapeDirection[]>([]);

  useEffect(() => {
    api.listRoutes().then(setRoutes).catch(() => {});
  }, []);

  // Stop markers and the route line are scoped to one selected route, not
  // rendered for all ~6,100 stops / 358 shapes at once - city-wide clutter
  // would bury the vehicles, which are the actual signal on this view.
  useEffect(() => {
    if (!selectedRouteId) {
      setRouteStops([]);
      setRouteShapes([]);
      return;
    }
    api.listRouteStops(selectedRouteId).then(setRouteStops).catch(() => setRouteStops([]));
    api.getRouteShape(selectedRouteId).then(setRouteShapes).catch(() => setRouteShapes([]));
  }, [selectedRouteId]);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const data = await api.listLiveVehicles();
        if (!cancelled) {
          setVehicles(data);
          setError(null);
          setLastUpdated(new Date());
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load live vehicles");
      }
    }

    poll();
    const interval = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  const positioned = vehicles.filter((v) => v.latitude !== null && v.longitude !== null);
  // De-dupe stops that appear in both directions at the same physical location.
  const uniqueRouteStops = Array.from(new Map(routeStops.map((s) => [s.stop_id, s])).values());

  return (
    <div style={{ position: "relative", height: "calc(100vh - 49px)" }}>
      <MapContainer center={SAN_ANTONIO_CENTER} zoom={12} style={{ height: "100%", width: "100%" }}>
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />

        {/* Dedicated pane with a lower z-index than the default overlayPane
            (400) that stop/vehicle markers render in - stops and shape load
            via two independent async fetches, so whichever resolves later
            would otherwise paint on top regardless of JSX order. A pane
            guarantees stacking order independent of fetch timing. */}
        <Pane name="route-line" style={{ zIndex: 350 }}>
          {routeShapes.map((shape) => (
            <Polyline
              key={shape.direction_id}
              positions={shape.points.map((p) => [p.lat, p.lon] as [number, number])}
              pathOptions={{ color: "var(--route-line)", weight: 5, opacity: 0.9 }}
            />
          ))}
        </Pane>

        {uniqueRouteStops.map(
          (s) =>
            s.stop_lat !== null &&
            s.stop_lon !== null && (
              <CircleMarker
                key={s.stop_id}
                center={[s.stop_lat, s.stop_lon]}
                radius={5}
                pathOptions={{ color: "#fff", fillColor: "var(--series-1)", fillOpacity: 1, weight: 2 }}
              >
                <Popup>
                  <div style={{ fontSize: 13 }}>{s.stop_name}</div>
                </Popup>
              </CircleMarker>
            )
        )}

        {positioned.map((v) => {
          const status = classifyDelay(v.delay_seconds);
          const color = DELAY_STATUS_COLOR[status];
          return (
            <Marker
              key={v.vehicle_id}
              position={[v.latitude as number, v.longitude as number]}
              icon={createVehicleIcon(color, v.bearing, status === "unknown", v.current_status)}
              eventHandlers={
                v.route_id
                  ? {
                      click: () => setSelectedRouteId(v.route_id as string),
                    }
                  : undefined
              }
            >
              <Popup>
                <VehiclePopup v={v} />
              </Popup>
            </Marker>
          );
        })}
      </MapContainer>

      <div
        className="card"
        style={{
          position: "absolute",
          top: 16,
          left: 16,
          zIndex: 1000,
          fontSize: 12,
          minWidth: 220,
        }}
      >
        <div style={{ fontWeight: 700, marginBottom: 4 }}>Route stops & shape</div>
        <div style={{ color: "var(--text-muted)", marginBottom: 8 }}>Pick a route, or click any vehicle</div>
        <select
          value={selectedRouteId}
          onChange={(e) => setSelectedRouteId(e.target.value)}
          style={{ width: "100%" }}
        >
          <option value="">None</option>
          {routes.map((r) => (
            <option key={r.route_id} value={r.route_id}>
              {r.route_short_name ?? r.route_id} - {r.route_long_name}
            </option>
          ))}
        </select>
      </div>

      <div
        className="card"
        style={{
          position: "absolute",
          top: 16,
          right: 16,
          zIndex: 1000,
          fontSize: 12,
          minWidth: 160,
        }}
      >
        <div style={{ fontWeight: 700, marginBottom: 8 }}>Vehicle status</div>
        {LEGEND_ITEMS.map((key) => (
          <div key={key} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
            <span
              style={{
                width: 10,
                height: 10,
                borderRadius: "50%",
                background: DELAY_STATUS_COLOR[key],
                display: "inline-block",
              }}
            />
            <span>{DELAY_STATUS_LABEL[key]}</span>
          </div>
        ))}
        <div style={{ marginTop: 8, color: "var(--text-muted)" }}>
          {positioned.length} vehicles{lastUpdated && <> · updated {lastUpdated.toLocaleTimeString()}</>}
        </div>
      </div>

      <div
        className="card"
        style={{
          position: "absolute",
          top: 232,
          right: 16,
          zIndex: 1000,
          fontSize: 12,
          minWidth: 160,
          maxWidth: 220,
          color: "var(--text-muted)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <svg width="14" height="14" viewBox="0 0 24 24" style={{ flexShrink: 0 }}>
            <polygon points="12,3 19,20 12,16 5,20" fill="#888" stroke="#4a4a4a" strokeWidth="1" strokeLinejoin="round" />
          </svg>
          <span>Chevron, pointing the way it's heading - in transit / approaching a stop</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <svg width="13" height="13" viewBox="0 0 18 18" style={{ flexShrink: 0 }}>
            <circle cx="9" cy="9" r="6.5" fill="#888" stroke="#4a4a4a" strokeWidth="1.25" />
          </svg>
          <span>Circle - stopped at a stop right now</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <svg width="9" height="9" viewBox="0 0 9 9" style={{ flexShrink: 0 }}>
            <circle cx="4.5" cy="4.5" r="3.5" fill="#888" fillOpacity="0.6" />
          </svg>
          <span>Small dot - no active trip (e.g. parked overnight)</span>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span
            style={{
              width: 16,
              height: 3,
              borderRadius: 2,
              background: "var(--route-line)",
              display: "inline-block",
              flexShrink: 0,
            }}
          />
          <span>Route shape - the road path a selected route's vehicles actually follow, from VIA's published schedule (not a live GPS trace)</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: "50%",
              background: "var(--series-1)",
              border: "2px solid #fff",
              display: "inline-block",
              flexShrink: 0,
            }}
          />
          <span>Stops along the selected route</span>
        </div>
      </div>

      {error && (
        <div style={{ position: "absolute", bottom: 16, left: 16, zIndex: 1000, maxWidth: 360 }}>
          <ErrorState message={error} />
        </div>
      )}
    </div>
  );
}
