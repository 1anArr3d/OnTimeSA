import { NavLink } from "react-router-dom";

const LINKS = [
  { to: "/", label: "Live Map" },
  { to: "/dashboard", label: "Dashboard" },
  { to: "/commute", label: "Check My Commute" },
];

export function NavBar() {
  return (
    <nav
      style={{
        display: "flex",
        alignItems: "center",
        gap: 24,
        padding: "12px 24px",
        borderBottom: "1px solid var(--border)",
        background: "var(--surface-1)",
      }}
    >
      <span style={{ fontWeight: 700, marginRight: 16 }}>SA Transit Pulse</span>
      {LINKS.map((link) => (
        <NavLink
          key={link.to}
          to={link.to}
          end={link.to === "/"}
          style={({ isActive }) => ({
            color: isActive ? "var(--series-1)" : "var(--text-secondary)",
            fontWeight: isActive ? 600 : 400,
            fontSize: 13,
            textDecoration: "none",
          })}
        >
          {link.label}
        </NavLink>
      ))}
    </nav>
  );
}
