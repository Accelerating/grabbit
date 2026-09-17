import { isRouteErrorResponse, Links, Meta, Outlet, Scripts, ScrollRestoration } from "react-router";
import type { Route } from "./+types/root";
import "./app.css";
import { LanguageProvider } from "./lib/i18n";
import { ThemeProvider } from "./lib/theme";
import { AuthProvider } from "./lib/auth";
import { EventProvider } from "./lib/events";

export const links: Route.LinksFunction = () => [{ rel: "icon", href: "/favicon.ico" }];

export function Layout({ children }: { children: React.ReactNode }) {
  return <html lang="zh-CN"><head><meta charSet="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><Meta /><Links /></head><body>{children}<ScrollRestoration /><Scripts /></body></html>;
}

export default function App() {
  return <LanguageProvider><ThemeProvider><AuthProvider><EventProvider><Outlet /></EventProvider></AuthProvider></ThemeProvider></LanguageProvider>;
}

export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  let message = "Error";
  let details = "An unexpected error occurred.";
  if (isRouteErrorResponse(error)) { message = error.status === 404 ? "404" : "Error"; details = error.statusText || details; }
  else if (import.meta.env.DEV && error instanceof Error) details = error.message;
  return <main className="error-page"><div className="error-card"><span className="eyebrow">Grabbit</span><h1>{message}</h1><p>{details}</p><a className="button button-primary" href="/">Go home</a></div></main>;
}
