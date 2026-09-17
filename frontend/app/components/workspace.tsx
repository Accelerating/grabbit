import { Link, NavLink, Navigate, Outlet, useLocation, useNavigate } from "react-router";
import { useEffect, useState } from "react";
import { useAuth } from "../lib/auth";
import { useLanguage } from "../lib/i18n";
import { useTheme } from "../lib/theme";
import { Icon } from "./icons";
import { getQueue, getSystemSummary } from "../lib/api";
import { useEvents } from "../lib/events";

const navItems = [
  { path: "/downloads", key: "generalDownloads" as const, icon: "download" },
  { path: "/video", key: "videoDownloads" as const, icon: "video" },
  { path: "/history", key: "history" as const, icon: "history" },
  { path: "/files", key: "files" as const, icon: "folder" },
  { path: "/cookies", key: "cookies" as const, icon: "cookie" },
  { path: "/settings", key: "settings" as const, icon: "settings" },
];

function formatBytes(bytes: number | null | undefined) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let index = -1;
  do { value /= 1024; index += 1; } while (value >= 1024 && index < units.length - 1);
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatRate(bytes: number | null | undefined) {
  return bytes === null || bytes === undefined ? "—" : `${formatBytes(bytes)}/s`;
}

export function AppLayout() {
  const { session, loading, signOut } = useAuth();
  const events = useEvents();
  const { t, language, setLanguage } = useLanguage();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [queueCount, setQueueCount] = useState<number | null>(null);
  const [summary, setSummary] = useState<{ download_speed_bps?: number | null; upload_speed_bps?: number | null; disk_free_bytes?: number | null } | null>(null);
  useEffect(() => {
    if (!session) return;
    const loadWorkspaceStats = async () => {
      const [queueResult, summaryResult] = await Promise.allSettled([getQueue(), getSystemSummary()]);
      if (queueResult.status === "fulfilled") setQueueCount(queueResult.value.items?.length ?? 0);
      if (summaryResult.status === "fulfilled") setSummary(summaryResult.value);
    };
    void loadWorkspaceStats();
    const timer = window.setInterval(() => void loadWorkspaceStats(), events.state === "connected" ? 15000 : 5000);
    return () => window.clearInterval(timer);
  }, [session?.user.id, events.state, events.revision]);
  if (loading) return <div className="loading-screen"><div className="spinner" /><span>{t("loading")}</span></div>;
  if (!session) { if (location.pathname !== "/login") return <Navigate to="/login" replace />; return <div className="loading-screen"><span>{t("unauthenticated")}</span></div>; }
  const doSignOut = async () => { await signOut(); navigate("/login"); };
  return <div className="app-shell">
    <div className={`mobile-scrim ${drawerOpen ? "is-open" : ""}`} onClick={() => setDrawerOpen(false)} aria-hidden="true" />
    <aside className={`sidebar ${drawerOpen ? "is-open" : ""}`} aria-label={t("navigation")}>
      <div className="brand"><span className="brand-mark">G</span><span><strong>{t("appName")}</strong><small>{t("appTagline")}</small></span><button className="icon-button sidebar-close" onClick={() => setDrawerOpen(false)} aria-label={t("closeNavigation")}><Icon name="close" /></button></div>
      <div className="sidebar-heading">{t("overview")}</div>
      <nav className="main-nav">{navItems.map((item) => <NavLink key={item.path} to={item.path} onClick={() => setDrawerOpen(false)} className={({ isActive }) => `nav-item ${isActive ? "active" : ""}`}><Icon name={item.icon} /><span>{t(item.key)}</span>{item.path === "/downloads" && <span className="nav-badge">{queueCount ?? "—"}</span>}</NavLink>)}</nav>
      <div className="sidebar-bottom"><div className="queue-card"><div className="queue-icon"><Icon name="download" size={16} /></div><div><strong>{t("queue")}</strong><span>{t("queueDescription")}</span></div><Icon name="chevron" size={16} /></div><div className="sidebar-footnote"><span className="status-dot" />{t("apiContract")}: {events.state === "connected" ? (language === "zh-CN" ? "实时" : "Live") : (language === "zh-CN" ? "重连中" : "Reconnecting")}</div></div>
    </aside>
    <main className="main-area">
      <header className="topbar"><button className="icon-button menu-button" onClick={() => setDrawerOpen(true)} aria-label={t("openNavigation")}><Icon name="menu" /></button><div className="topbar-stats"><span><i className="stat-dot blue" />{t("downloadSpeed")} <b>{events.state === "connected" ? formatRate(summary?.download_speed_bps) : "—"}</b></span><span><i className="stat-dot green" />{t("uploadSpeed")} <b>{events.state === "connected" ? formatRate(summary?.upload_speed_bps) : "—"}</b></span><span className="disk-stat"><Icon name="disk" size={15} />{t("diskAvailable")} <b>{formatBytes(summary?.disk_free_bytes)}</b></span></div><div className="topbar-actions"><button className="topbar-control" onClick={() => setLanguage(language === "zh-CN" ? "en" : "zh-CN")} aria-label={t("language")}><Icon name="globe" size={16} />{language === "zh-CN" ? "中" : "EN"}</button><button className="topbar-control" onClick={toggleTheme} aria-label={t("theme")}><Icon name={theme === "light" ? "moon" : "sun"} size={16} /></button><div className="account-menu"><span className="avatar">{session.user.username.slice(0, 1).toUpperCase()}</span><span className="account-name">{session.user.username}<small>{t("administrator")}</small></span><button className="icon-button" onClick={() => void doSignOut()} aria-label={t("signOut")}><Icon name="logout" size={16} /></button></div></div></header>
      <div className="page-wrap"><Outlet /></div>
    </main>
  </div>;
}

export function PageHeader({ title, description, action }: { title: string; description?: string; action?: React.ReactNode }) {
  return <div className="page-header"><div><div className="eyebrow">GRABBIT / WORKSPACE</div><h1>{title}</h1>{description && <p>{description}</p>}</div>{action && <div className="page-header-action">{action}</div>}</div>;
}

export function PanelHeading({ children, icon }: { children: React.ReactNode; icon?: string }) {
  return <span className="panel-heading">{icon && <Icon name={icon} size={16} />}<span>{children}</span></span>;
}

export function EmptyState({ icon = "download", title, description, action }: { icon?: string; title: string; description: string; action?: React.ReactNode }) {
  return <div className="empty-state"><div className="empty-icon"><Icon name={icon} size={24} /></div><h2>{title}</h2><p>{description}</p>{action}</div>;
}

export function LoadingState() { const { t } = useLanguage(); return <div className="panel-state"><div className="spinner" /><span>{t("loading")}</span></div>; }
export function ErrorState({ onRetry }: { onRetry?: () => void }) { const { t } = useLanguage(); return <div className="panel-state error-state"><div className="error-symbol">!</div><strong>{t("error")}</strong><span>{t("connectionError")}</span>{onRetry && <button className="button button-secondary" onClick={onRetry}>{t("retry")}</button>}</div>; }

export function WorkspacePlaceholder({ icon, title, description, actionLabel, actionTo }: { icon: string; title: string; description: string; actionLabel?: string; actionTo?: string }) {
  const { t } = useLanguage();
  return <><PageHeader title={title} description={description} action={actionLabel && actionTo ? <Link className="button button-primary" to={actionTo}><Icon name="plus" size={17} />{actionLabel}</Link> : undefined} /><section className="content-panel"><EmptyState icon={icon} title={t("noTasksTitle")} description={t("comingSoonDescription")} /></section></>;
}
