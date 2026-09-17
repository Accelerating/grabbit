import { useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router";
import { Icon } from "../components/icons";
import { ErrorState, LoadingState, PageHeader } from "../components/workspace";
import { useAuth } from "../lib/auth";
import { apiFetch, type ApiError } from "../lib/api";
import { useLanguage } from "../lib/i18n";

type SettingsResponse = {
  editable: {
    max_active_tasks: number;
    download_limit_bps: number;
    upload_limit_bps: number;
    disk_min_free_bytes: number;
    default_video_height: number;
    default_subtitle_languages: string[];
    allow_auto_subtitles: boolean;
  };
  read_only: { download_root: string };
  revision: number;
};
type DependencyResponse = { items: Record<string, { status: "available" | "missing" | "error"; version: string | null }>; install_supported: boolean };
type InstallJob = { id: string; action: "aria2" | "ffmpeg"; status: "queued" | "running" | "completed" | "failed"; log: string; error_code: string | null };

const copy = {
  en: {
    save: "Save settings", saved: "Settings saved", download: "Download", upload: "Upload", unlimited: "0 means unlimited", diskThreshold: "Minimum free disk space", videoDefaults: "Video defaults", maxHeight: "Default quality cap", subtitles: "Subtitle languages", autoSubtitles: "Allow automatic subtitles", root: "Download root", readOnly: "Read-only deployment setting", passwordTitle: "Change password", currentPassword: "Current password", newPassword: "New password", confirmPassword: "Confirm new password", changePassword: "Change password", passwordChanged: "Password changed. Sign in again with your new password.", passwordLength: "Use at least 6 characters.", passwordMismatch: "The new passwords do not match.", required: "This field is required.", conflict: "Settings changed in another window. The latest values have been loaded.", generic: "Could not save this change. Try again.", currentInvalid: "The current password is incorrect.", units: "bytes / second", gib: "GiB", settingsSaved: "Download runtime integration is pending.", subtitleHint: "Comma-separated language codes, for example zh,en.",
  },
  "zh-CN": {
    save: "保存设置", saved: "设置已保存", download: "下载", upload: "上传", unlimited: "0 表示不限速", diskThreshold: "最低可用磁盘空间", videoDefaults: "视频默认值", maxHeight: "默认画质上限", subtitles: "字幕语言", autoSubtitles: "允许自动字幕", root: "下载根目录", readOnly: "部署配置只读", passwordTitle: "修改密码", currentPassword: "当前密码", newPassword: "新密码", confirmPassword: "确认新密码", changePassword: "修改密码", passwordChanged: "密码已修改，请使用新密码重新登录。", passwordLength: "密码至少需要 6 个字符。", passwordMismatch: "两次输入的新密码不一致。", required: "此字段为必填项。", conflict: "设置已在其他窗口修改，已加载最新值。", generic: "保存失败，请稍后重试。", currentInvalid: "当前密码不正确。", units: "字节 / 秒", gib: "GiB", settingsSaved: "下载运行时接入仍待完成。", subtitleHint: "用逗号分隔语言代码，例如 zh,en。",
  },
} as const;

type FormState = { maxActiveTasks: string; downloadLimit: string; uploadLimit: string; diskMinGiB: string; defaultHeight: string; subtitles: string; autoSubtitles: boolean };
const toForm = (settings: SettingsResponse["editable"]): FormState => ({ maxActiveTasks: String(settings.max_active_tasks), downloadLimit: String(settings.download_limit_bps), uploadLimit: String(settings.upload_limit_bps), diskMinGiB: String(Math.round(settings.disk_min_free_bytes / 1073741824)), defaultHeight: String(settings.default_video_height), subtitles: settings.default_subtitle_languages.join(","), autoSubtitles: settings.allow_auto_subtitles });

export default function Settings() {
  const { t, language } = useLanguage();
  const text = copy[language];
  const { signOut } = useAuth();
  const navigate = useNavigate();
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [dependencies, setDependencies] = useState<DependencyResponse | null>(null);
  const [installJobs, setInstallJobs] = useState<InstallJob[]>([]);
  const [installError, setInstallError] = useState("");
  const [installBusy, setInstallBusy] = useState(false);
  const [form, setForm] = useState<FormState | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [saveError, setSaveError] = useState("");
  const [passwords, setPasswords] = useState({ current: "", next: "", confirm: "" });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordMessage, setPasswordMessage] = useState("");
  const [passwordError, setPasswordError] = useState("");

  const loadSettings = async () => {
    setLoading(true); setLoadError(false);
    try { const response = await apiFetch<SettingsResponse>("/settings"); setData(response); setForm(toForm(response.editable)); }
    catch { setLoadError(true); }
    finally { setLoading(false); }
  };
  useEffect(() => { void loadSettings(); }, []);
  useEffect(() => { void apiFetch<DependencyResponse>("/dependencies").then(setDependencies).catch(() => setDependencies(null)); }, []);
  useEffect(() => {
    const refresh = () => { void apiFetch<{ items: InstallJob[] }>("/dependencies/install-jobs").then((response) => {
      setInstallJobs(response.items);
      if (response.items[0]?.status === "completed") void apiFetch<DependencyResponse>("/dependencies").then(setDependencies);
    }).catch(() => undefined); };
    refresh(); const timer = window.setInterval(refresh, 5000); return () => window.clearInterval(timer);
  }, []);
  const installPackage = async (action: "aria2" | "ffmpeg") => {
    setInstallBusy(true); setInstallError("");
    try {
      const job = await apiFetch<InstallJob>("/dependencies/install-jobs", { method: "POST", body: JSON.stringify({ action }) });
      setInstallJobs((current) => [job, ...current].slice(0, 10));
    } catch (error) { setInstallError((error as ApiError).message || (language === "zh-CN" ? "安装请求失败" : "Installation request failed")); }
    finally { setInstallBusy(false); }
  };
  const update = (key: keyof FormState, value: string | boolean) => setForm((current) => current ? { ...current, [key]: value } : current);
  const values = useMemo(() => form ? {
    max_active_tasks: Number(form.maxActiveTasks), download_limit_bps: Number(form.downloadLimit), upload_limit_bps: Number(form.uploadLimit), disk_min_free_bytes: Math.round(Number(form.diskMinGiB) * 1073741824), default_video_height: Number(form.defaultHeight), default_subtitle_languages: form.subtitles.split(",").map((item) => item.trim()).filter(Boolean), allow_auto_subtitles: form.autoSubtitles,
  } : null, [form]);
  const saveSettings = async (event: FormEvent) => {
    event.preventDefault(); if (!data || !values) return; setSaving(true); setSaveMessage(""); setSaveError("");
    try { const response = await apiFetch<SettingsResponse>("/settings", { method: "PATCH", body: JSON.stringify({ revision: data.revision, ...values }) }); setData(response); setForm(toForm(response.editable)); setSaveMessage(`${text.saved} · ${text.settingsSaved}`); }
    catch (error) { const apiError = error as ApiError; if (apiError.code === "REVISION_CONFLICT") { setSaveError(text.conflict); await loadSettings(); } else setSaveError(text.generic); }
    finally { setSaving(false); }
  };
  const changePassword = async (event: FormEvent) => {
    event.preventDefault(); setPasswordMessage(""); setPasswordError("");
    if (!passwords.current || !passwords.next || !passwords.confirm) { setPasswordError(text.required); return; }
    if (passwords.next.length < 6) { setPasswordError(text.passwordLength); return; }
    if (passwords.next !== passwords.confirm) { setPasswordError(text.passwordMismatch); return; }
    setPasswordBusy(true);
    try { await apiFetch<void>("/auth/password", { method: "PATCH", body: JSON.stringify({ current_password: passwords.current, new_password: passwords.next }) }); setPasswordMessage(text.passwordChanged); setPasswords({ current: "", next: "", confirm: "" }); await signOut().catch(() => undefined); navigate("/login", { replace: true }); }
    catch (error) { setPasswordError((error as ApiError).status === 401 ? text.currentInvalid : text.generic); }
    finally { setPasswordBusy(false); }
  };
  if (loading) return <><PageHeader title={t("settings")} description={t("settingsDescription")} /><section className="content-panel"><LoadingState /></section></>;
  if (loadError || !data || !form) return <><PageHeader title={t("settings")} description={t("settingsDescription")} /><section className="content-panel"><ErrorState onRetry={() => void loadSettings()} /></section></>;
  return <><PageHeader title={t("settings")} description={t("settingsDescription")} /><div className="settings-grid"><form className="settings-card settings-form" onSubmit={saveSettings}><div className="settings-card-header"><div><h2>{t("preferences")}</h2><p>{t("settingsDescription")}</p></div><Icon name="settings" size={19} /></div><fieldset><legend>{text.download}</legend><div className="form-grid"><label>{t("maxActiveTasks")}<input type="number" min="1" max="4" value={form.maxActiveTasks} onChange={(event) => update("maxActiveTasks", event.target.value)} /><small>1–4</small></label><label>{text.download}<input type="number" min="0" value={form.downloadLimit} onChange={(event) => update("downloadLimit", event.target.value)} /><small>{text.units} · {text.unlimited}</small></label><label>{text.upload}<input type="number" min="0" value={form.uploadLimit} onChange={(event) => update("uploadLimit", event.target.value)} /><small>{text.units} · {text.unlimited}</small></label><label>{text.diskThreshold}<input type="number" min="0" value={form.diskMinGiB} onChange={(event) => update("diskMinGiB", event.target.value)} /><small>{text.gib}</small></label></div></fieldset><fieldset><legend>{text.videoDefaults}</legend><div className="form-grid"><label>{text.maxHeight}<select value={form.defaultHeight} onChange={(event) => update("defaultHeight", event.target.value)}><option value="720">720p</option><option value="1080">1080p</option><option value="1440">1440p</option><option value="2160">2160p</option></select></label><label>{text.subtitles}<input value={form.subtitles} onChange={(event) => update("subtitles", event.target.value)} /><small>{text.subtitleHint}</small></label></div><label className="toggle-row"><input type="checkbox" checked={form.autoSubtitles} onChange={(event) => update("autoSubtitles", event.target.checked)} /><span><b>{text.autoSubtitles}</b><small>{text.subtitleHint}</small></span></label></fieldset><div className="settings-footer">{saveError && <span className="inline-error">{saveError}</span>}{saveMessage && <span className="inline-success">{saveMessage}</span>}<button className="button button-primary" type="submit" disabled={saving}>{saving ? t("loading") : text.save}<Icon name="chevron" size={16} /></button></div></form><div className="settings-side"><section className="settings-card"><div className="settings-card-header"><div><h2>{text.root}</h2><p>{text.readOnly}</p></div><Icon name="folder" size={19} /></div><code className="read-only-path">{data.read_only.download_root}</code></section><section className="settings-card"><div className="settings-card-header"><div><h2>{t("dependencyServices")}</h2><p>{dependencies?.install_supported ? (language === "zh-CN" ? "Debian 13 可安装 aria2 和 ffmpeg；运行中的任务会先完成。" : "On Debian 13, aria2 and ffmpeg can be installed after active work finishes.") : (language === "zh-CN" ? "当前环境仅检测依赖。" : "Dependency detection only in this environment.")}</p></div><Icon name="settings" size={19} /></div><div className="dependency-list">{dependencies ? Object.entries(dependencies.items).map(([name, item]) => <div key={name}><strong>{name}</strong><span>{item.status === "available" ? (language === "zh-CN" ? "可用" : "Available") : item.status === "missing" ? (language === "zh-CN" ? "未安装" : "Missing") : (language === "zh-CN" ? "检测失败" : "Check failed")}</span><small>{item.version || "—"}</small></div>) : <span>{t("notAvailable")}</span>}</div>{dependencies?.install_supported && <div className="dependency-actions"><button type="button" className="button button-secondary" disabled={installBusy || installJobs.some((job) => job.status === "queued" || job.status === "running")} onClick={() => void installPackage("aria2")}>{language === "zh-CN" ? "安装/更新 aria2" : "Install/update aria2"}</button><button type="button" className="button button-secondary" disabled={installBusy || installJobs.some((job) => job.status === "queued" || job.status === "running")} onClick={() => void installPackage("ffmpeg")}>{language === "zh-CN" ? "安装/更新 ffmpeg" : "Install/update ffmpeg"}</button></div>}{installError && <p className="inline-error" role="alert">{installError}</p>}{installJobs[0] && <div className="dependency-job"><strong>{installJobs[0].action} · {installJobs[0].status}</strong>{installJobs[0].log && <pre>{installJobs[0].log}</pre>}{installJobs[0].error_code && <small>{installJobs[0].error_code}</small>}</div>}</section><form className="settings-card password-form" onSubmit={changePassword}><div className="settings-card-header"><div><h2>{text.passwordTitle}</h2><p>{t("security")}</p></div><Icon name="settings" size={19} /></div><label>{text.currentPassword}<input type="password" autoComplete="current-password" value={passwords.current} onChange={(event) => setPasswords({ ...passwords, current: event.target.value })} /></label><label>{text.newPassword}<input type="password" autoComplete="new-password" value={passwords.next} onChange={(event) => setPasswords({ ...passwords, next: event.target.value })} /><small>{text.passwordLength}</small></label><label>{text.confirmPassword}<input type="password" autoComplete="new-password" value={passwords.confirm} onChange={(event) => setPasswords({ ...passwords, confirm: event.target.value })} /></label>{passwordError && <div className="form-error" role="alert">{passwordError}</div>}{passwordMessage && <div className="inline-success" role="status">{passwordMessage}</div>}<button className="button button-secondary" type="submit" disabled={passwordBusy}>{passwordBusy ? t("loading") : text.changePassword}</button></form></div></div></>;
}
