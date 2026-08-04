import type { Confidence } from "../api/types";

/**
 * Low-confidence data must never render identically to solid data - this is
 * the visible half of the backend's sample-count flag. Icon + label per the
 * status-palette rule (never color alone), always showing the sample count.
 */
export function ConfidenceBadge({ confidence, sampleCount }: { confidence: Confidence; sampleCount: number }) {
  if (confidence === "low") {
    return (
      <span
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          color: "var(--status-warning)",
          background: "color-mix(in srgb, var(--status-warning) 15%, transparent)",
          border: "1px solid var(--status-warning)",
          borderRadius: 4,
          padding: "2px 8px",
          fontSize: 12,
          fontWeight: 600,
        }}
        title={`Only ${sampleCount} observations - treat this number as directional, not precise.`}
      >
        ⚠ Low confidence (n={sampleCount})
      </span>
    );
  }
  return <span style={{ color: "var(--text-muted)", fontSize: 12 }}>n={sampleCount}</span>;
}
