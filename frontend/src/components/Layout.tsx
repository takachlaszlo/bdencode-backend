import {
  Activity,
  Archive,
  AudioWaveform,
  CirclePlus,
  Gauge,
  ListOrdered,
  Menu,
  Settings,
  X,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet } from "react-router";
import clsx from "clsx";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

const navigation = [
  { to: "/", label: "Áttekintés", icon: Gauge, end: true },
  { to: "/new", label: "Új kódolás", icon: CirclePlus },
  { to: "/queue", label: "Várólista", icon: ListOrdered },
  { to: "/archive", label: "Elkészült munkák", icon: Archive },
  { to: "/comparisons", label: "Összehasonlítások", icon: AudioWaveform },
  { to: "/settings", label: "Rendszer", icon: Settings },
];

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: 5000,
    retry: 2,
  });

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Ugrás a tartalomhoz</a>
      <button
        type="button"
        className="mobile-menu-button"
        aria-label="Menü megnyitása"
        aria-expanded={mobileOpen}
        aria-controls="primary-sidebar"
        onClick={() => setMobileOpen(true)}
      >
        <Menu size={22} aria-hidden="true" />
      </button>

      <aside id="primary-sidebar" className={clsx("sidebar", mobileOpen && "sidebar--open")}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <span />
          </span>
          <div>
            <strong>BDEncode</strong>
            <small>Studio Console</small>
          </div>
          <button
            type="button"
            className="sidebar-close"
            aria-label="Menü bezárása"
            onClick={() => setMobileOpen(false)}
          >
            <X size={20} />
          </button>
        </div>

        <nav className="primary-nav" aria-label="Fő navigáció">
          {navigation.map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) => clsx("nav-link", isActive && "nav-link--active")}
            >
              <Icon size={19} strokeWidth={1.8} aria-hidden="true" />
              <span>{label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-status" role="status" aria-live="polite">
          <div className="sidebar-status__top">
            <span
              className={clsx(
                "status-dot",
                health.isSuccess ? "status-dot--online" : health.isError ? "status-dot--error" : "status-dot--pending",
              )}
              aria-hidden="true"
            />
            <span>{health.isSuccess ? "Szerver elérhető" : health.isError ? "Kapcsolati hiba" : "Kapcsolódás…"}</span>
          </div>
          {health.data && (
            <small>
              {health.data.active_job_id ? "1 aktív munka" : "Nincs aktív munka"} · {health.data.queued_jobs} várakozik
            </small>
          )}
        </div>

        <div className="sidebar-footer">
          <Activity size={16} aria-hidden="true" />
          <span>80% CPU-védelem aktív</span>
        </div>
      </aside>

      {mobileOpen && <button type="button" className="sidebar-scrim" aria-label="Menü bezárása" onClick={() => setMobileOpen(false)} />}

      <main id="main-content" className="main-content" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  );
}
