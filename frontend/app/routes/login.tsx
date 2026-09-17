import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router";
import { Icon } from "../components/icons";
import { useAuth } from "../lib/auth";
import { useLanguage } from "../lib/i18n";
import { useTheme } from "../lib/theme";

export default function Login() {
  const { session, loading, signIn } = useAuth();
  const { t, language, setLanguage } = useLanguage();
  const { theme, toggleTheme } = useTheme();
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { if (!loading && session) navigate("/downloads", { replace: true }); }, [loading, session, navigate]);
  const submit = async (event: FormEvent) => { event.preventDefault(); setError(null); setBusy(true); try { await signIn(username.trim(), password); navigate("/downloads", { replace: true }); } catch (err) { setError((err as { status?: number }).status === 401 ? t("invalidCredentials") : t("unavailable")); } finally { setBusy(false); } };
  return <main className="login-page"><div className="login-topbar"><button className="topbar-control" onClick={() => setLanguage(language === "zh-CN" ? "en" : "zh-CN")}><Icon name="globe" size={16} />{language === "zh-CN" ? "中" : "EN"}</button><button className="topbar-control" onClick={toggleTheme} aria-label={t("theme")}><Icon name={theme === "light" ? "moon" : "sun"} size={16} /></button></div><div className="login-card"><div className="brand login-brand"><span className="brand-mark">G</span><span><strong>{t("appName")}</strong><small>{t("appTagline")}</small></span></div><div className="login-copy"><div className="eyebrow">WELCOME BACK</div><h1>{t("signInTitle")}</h1><p>{t("signInDescription")}</p></div><form onSubmit={submit} className="login-form"><label>{t("username")}<input autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required /></label><label>{t("password")}<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>{error && <div className="form-error" role="alert">{error}</div>}<button className="button button-primary login-submit" type="submit" disabled={busy}>{busy ? t("signingIn") : t("signIn")}<Icon name="chevron" size={17} /></button></form><p className="login-hint">{t("signInHint")}</p></div><div className="login-footer"><span>Grabbit</span><span>•</span><span>{new Date().getFullYear()}</span></div></main>;
}
