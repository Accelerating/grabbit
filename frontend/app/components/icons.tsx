type IconProps = { name: string; size?: number };
export function Icon({ name, size = 18 }: IconProps) {
  const common = { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const, "aria-hidden": true };
  const paths: Record<string, React.ReactNode> = {
    download: <><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M4 21h16" /></>,
    video: <><rect x="3" y="5" width="15" height="14" rx="2" /><path d="m18 10 3-2v8l-3-2" /></>,
    history: <><path d="M3 12a9 9 0 1 0 3-6.7" /><path d="M3 4v5h5" /><path d="M12 7v5l3 2" /></>,
    folder: <><path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h5l2 2h8A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z" /></>,
    cookie: <><path d="M19.5 13a4.5 4.5 0 0 0-5-5 4.5 4.5 0 1 0-6 6 4.5 4.5 0 0 0 6-6" /><path d="M8.5 12h.01M12 15.5h.01M13.5 11h.01" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="m19.4 15 .1.1a1.8 1.8 0 1 1-2.5 2.5l-.1-.1a1.8 1.8 0 0 0-3.1 1.3v.2a1.8 1.8 0 1 1-3.6 0v-.2a1.8 1.8 0 0 0-3.1-1.3l-.1.1a1.8 1.8 0 1 1-2.5-2.5l.1-.1A1.8 1.8 0 0 0 3.3 12a1.8 1.8 0 0 0-1.7-1.8 1.8 1.8 0 1 1 0-3.6h.2a1.8 1.8 0 0 0 1.3-3.1L3 3.4A1.8 1.8 0 1 1 5.5.9l.1.1A1.8 1.8 0 0 0 8.7 0h.2a1.8 1.8 0 1 1 3.6 0v.2a1.8 1.8 0 0 0 3.1 1.3l.1-.1A1.8 1.8 0 1 1 18.2 4l-.1.1a1.8 1.8 0 0 0 1.3 3.1h.2a1.8 1.8 0 1 1 0 3.6h-.2a1.8 1.8 0 0 0 0 3.1Z" /></>,
    menu: <><path d="M4 6h16M4 12h16M4 18h16" /></>, close: <><path d="m6 6 12 12M18 6 6 18" /></>,
    sun: <><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" /></>,
    moon: <><path d="M20.5 15.2A8.5 8.5 0 0 1 8.8 3.5 8.6 8.6 0 1 0 20.5 15.2Z" /></>, globe: <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" /></>,
    chevron: <path d="m9 18 6-6-6-6" />, search: <><circle cx="10.8" cy="10.8" r="6.8" /><path d="m16 16 5 5" /></>, refresh: <><path d="M20 11a8 8 0 0 0-14.7-4L3 10" /><path d="M3 5v5h5" /><path d="M4 13a8 8 0 0 0 14.7 4L21 14" /><path d="M21 19v-5h-5" /></>, plus: <><path d="M12 5v14M5 12h14" /></>, pause: <><path d="M8 5v14M16 5v14" /></>, play: <path d="m8 5 11 7-11 7z" />, logout: <><path d="M10 17l5-5-5-5M15 12H3M21 19V5a2 2 0 0 0-2-2h-5" /></>, disk: <><path d="M4 4h13l3 3v13H4z" /><path d="M8 4v6h8V4M8 20v-6h8v6" /></>,
  };
  return <svg {...common}>{paths[name] || paths.settings}</svg>;
}
