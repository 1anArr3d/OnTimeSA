export function LoadingState({ label = "Loading..." }: { label?: string }) {
  return <div style={{ padding: 32, color: "var(--text-muted)", fontSize: 13 }}>{label}</div>;
}

export function ErrorState({ message }: { message: string }) {
  return (
    <div
      className="card"
      style={{ borderColor: "var(--status-critical)", color: "var(--status-critical)", fontSize: 13 }}
    >
      {message}
    </div>
  );
}

export function EmptyState({ message }: { message: string }) {
  return <div style={{ padding: 32, color: "var(--text-muted)", fontSize: 13, textAlign: "center" }}>{message}</div>;
}
