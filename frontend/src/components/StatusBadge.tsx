const SEVERITY_COLOR: Record<string, string> = {
  low: "var(--status-warning)",
  medium: "var(--status-serious)",
  high: "var(--status-critical)",
};

export function BunchingSeverityBadge({ severity }: { severity: string }) {
  const color = SEVERITY_COLOR[severity] ?? "var(--status-unknown)";
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        color,
        fontSize: 12,
        fontWeight: 600,
        textTransform: "capitalize",
      }}
    >
      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, display: "inline-block" }} />
      {severity}
    </span>
  );
}

export function DelayDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <span style={{ width: 10, height: 10, borderRadius: "50%", background: color, display: "inline-block" }} />
      <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>{label}</span>
    </span>
  );
}
