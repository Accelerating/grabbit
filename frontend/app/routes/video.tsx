import { useCallback, useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { Icon } from "../components/icons";
import { CreateDialog } from "../components/create-dialog";
import NewVideo from "./new-video";
import { EmptyState, ErrorState, LoadingState, PageHeader, PanelHeading } from "../components/workspace";
import { changeTaskCookie, getCookieProfiles, getTasks, taskAction, type CookieProfile, type Task } from "../lib/api";
import { useEvents } from "../lib/events";
import { useLanguage } from "../lib/i18n";

function bytes(value: number | null | undefined) {
  return value == null ? "—" : value < 1048576 ? `${(value / 1024).toFixed(0)} KiB` : `${(value / 1048576).toFixed(1)} MiB`;
}

function progressLabel(task: Task, language: "zh-CN" | "en") {
  const progress = task.progress;
  if (!progress) return language === "zh-CN" ? "进度暂不可用" : "Progress unavailable";
  if (task.phase === "video_subtitles" || task.phase === "subtitle_retry") return language === "zh-CN" ? "正在处理字幕" : "Processing subtitles";
  const scope = progress.scope === "current_stream" ? (language === "zh-CN" ? "当前流" : "Current stream") : (language === "zh-CN" ? "任务" : "Task");
  const percent = progress.percent == null ? "" : ` · ${progress.percent.toFixed(1)}%`;
  const total = progress.total_bytes == null ? "" : ` / ${progress.total_is_estimate ? "≈" : ""}${bytes(progress.total_bytes)}`;
  const speed = progress.speed_bps == null ? "" : ` · ${bytes(progress.speed_bps)}/s`;
  const eta = progress.eta_seconds == null ? "" : ` · ${language === "zh-CN" ? "约" : "ETA"} ${Math.ceil(progress.eta_seconds)}s`;
  return progress.downloaded_bytes == null ? progress.percent == null ? (language === "zh-CN" ? "进度暂不可用" : "Progress unavailable") : `${scope}${percent}` : `${scope} · ${bytes(progress.downloaded_bytes)}${total}${percent}${speed}${eta}`;
}

export default function Video() {
  const { t, language } = useLanguage();
  const location = useLocation();
  const navigate = useNavigate();
  const creating = new URLSearchParams(location.search).has("new");
  const events = useEvents();
  const [items, setItems] = useState<Task[]>([]);
  const [profiles, setProfiles] = useState<CookieProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [actionError, setActionError] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const load = useCallback(async () => {
    try { setItems((await getTasks("video")).items); setError(false); }
    catch { setError(true); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load, events.revision]);
  useEffect(() => { void getCookieProfiles().then((response) => setProfiles(response.items)).catch(() => setProfiles([])); }, []);
  useEffect(() => { const timer = window.setInterval(() => void load(), events.state === "connected" ? 15000 : 5000); return () => window.clearInterval(timer); }, [events.state, load]);
  const act = async (task: Task, action: "stop" | "retry") => {
    setPending(task.id); setActionError(false);
    try { await taskAction(task.id, action); await load(); }
    catch { setActionError(true); }
    finally { setPending(null); }
  };
  const changeCookie = async (task: Task, profileId: string) => {
    setPending(task.id); setActionError(false);
    try { await changeTaskCookie(task.id, task.revision || 1, profileId || null); await load(); }
    catch { setActionError(true); }
    finally { setPending(null); }
  };
  return <>
    <PageHeader title={t("videoDownloads")} description={t("videoDescription")} action={<Link className="button button-primary" to="/video?new=1"><Icon name="plus" size={17} />{t("newVideoDownload")}</Link>} />
    <section className="content-panel">
      <div className="panel-toolbar"><PanelHeading>{language === "zh-CN" ? "进行中 / 失败" : "Active / failed"}</PanelHeading><button className="filter-button" onClick={() => void load()}>{t("refresh")}</button></div>
      {actionError && <div className="form-error task-list-error" role="alert">{t("taskActionFailed")}</div>}
      {loading ? <LoadingState /> : error ? <ErrorState onRetry={() => void load()} /> : items.length === 0 ?
        <EmptyState icon="video" title={t("noDownloads")} description={t("noDownloadsDescription")} action={<Link className="button button-secondary" to="/video?new=1"><Icon name="plus" size={16} />{t("newVideoDownload")}</Link>} /> :
        <div className="task-list">{items.map((task) => <article className="task-row" key={task.id}>
          <div className="task-main">
            <div className="task-title-line"><Icon name="video" size={16} /><strong>{task.title || t("taskTitle")}</strong><span className={`task-status status-${task.status}`}>{task.status}</span></div>
            <div className="task-source">{task.phase || task.error_code || task.status}</div>
            <div className="task-meta"><span>{progressLabel(task, language)}</span>{task.error_summary && <span>{task.error_summary}</span>}{task.warnings?.includes("SUBTITLE_FAILED") && <span>{language === "zh-CN" ? "字幕获取失败，媒体已保存" : "Subtitles failed; media was saved"}</span>}</div>
            {["queued", "retry_wait", "stopped", "failed"].includes(task.status) && <label className="video-cookie-choice">{language === "zh-CN" ? "Cookie 配置" : "Cookie profile"}<select value={task.cookie_profile_id || ""} disabled={pending === task.id} onChange={(event) => void changeCookie(task, event.target.value)}><option value="">{language === "zh-CN" ? "不使用" : "None"}</option>{profiles.filter((profile) => !task.source_site || profile.site_key === task.source_site).map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>}
          </div>
          <div className="task-actions">{task.allowed_actions?.includes("stop") && <button className="button button-secondary" disabled={pending === task.id} onClick={() => void act(task, "stop")}>{language === "zh-CN" ? "停止" : "Stop"}</button>}{task.allowed_actions?.includes("retry") && <button className="button button-secondary" disabled={pending === task.id} onClick={() => void act(task, "retry")}>{t("retry")}</button>}</div>
        </article>)}</div>}
    </section>
    {creating && <CreateDialog title={t("newVideoDownload")} description={t("videoDescription")} onClose={() => navigate("/video")}><NewVideo onCreated={() => { navigate("/video"); void load(); }} /></CreateDialog>}
  </>;
}
