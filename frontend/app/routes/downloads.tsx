import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { Icon } from "../components/icons";
import { CreateDialog } from "../components/create-dialog";
import NewDownload from "./new-download";
import { ErrorState, LoadingState, PageHeader, PanelHeading } from "../components/workspace";
import { getQueue, getSystemSummary, getTasks, getTaskCandidates, selectMagnetFiles, taskAction, type TorrentCandidate, type QueueResponse, type SystemSummary, type Task, type TaskAction } from "../lib/api";
import { useLanguage, type TranslationKey } from "../lib/i18n";
import { useEvents } from "../lib/events";

const statusKeys: Record<string, TranslationKey> = {
  queued: "waiting", resolving: "resolving", awaiting_selection: "selecting", downloading: "downloading",
  postprocessing: "postprocessing", retry_wait: "retryWaiting", paused: "paused", stopped: "stopped",
  completed: "completed", failed: "failed", cancelled: "cancelled",
};

function formatBytes(bytes: number | null | undefined) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let index = -1;
  do { value /= 1024; index += 1; } while (value >= 1024 && index < units.length - 1);
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatSpeed(bytes: number | null | undefined) {
  return bytes === null || bytes === undefined ? "—" : `${formatBytes(bytes)}/s`;
}

function formatEta(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || seconds < 0) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function titleFor(task: Task, sourceUnknown: string) {
  return task.title?.trim() || task.source_url?.trim() || sourceUnknown;
}

function TaskRow({ task, pending, onAction, onSelected }: { task: Task; pending: boolean; onAction: (task: Task, action: TaskAction) => void; onSelected: () => void }) {
  const { t, language } = useLanguage();
  const progress = task.progress;
  const percent = typeof progress?.percent === "number" ? Math.max(0, Math.min(100, progress.percent)) : null;
  const available = new Set(task.allowed_actions || []);
  const actions: TaskAction[] = ["pause", "resume", "retry", "cancel"];
  const actionLabels: Record<TaskAction, TranslationKey> = { pause: "pause", resume: "resume", retry: "retry", cancel: "cancel", stop: "stopped" };
  return <article className="task-row">
    <div className="task-main">
      <div className="task-title-line"><span className={`task-status-dot status-${task.status}`} /><strong title={titleFor(task, t("sourceUnknown"))}>{titleFor(task, t("sourceUnknown"))}</strong><span className={`task-status status-${task.status}`}>{t(statusKeys[task.status] || "status")}</span></div>
      <div className="task-source" title={task.source_url || undefined}>{task.source_url?.startsWith("magnet:") ? (language === "zh-CN" ? "磁力链接" : "Magnet link") : task.source_url || t("sourceUnknown")}</div>
      <div className="task-progress"><div className="task-progress-track"><span style={{ width: `${percent ?? 0}%` }} /></div><span>{percent === null ? "—" : `${percent.toFixed(percent % 1 ? 1 : 0)}%`}</span></div>
      <div className="task-meta"><span>{formatBytes(progress?.downloaded_bytes)} {t("bytesDownloaded")}{progress?.total_bytes ? ` / ${formatBytes(progress.total_bytes)}` : ""}</span><span>{formatSpeed(progress?.speed_bps)}</span><span>{t("eta")}: {formatEta(progress?.eta_seconds)}</span></div>
      {(task.status === "failed" || task.status === "retry_wait") && (task.error_summary || task.error_code) && <div className="task-error" role="alert">
        <strong>{language === "zh-CN" ? "失败原因" : "Failure details"}</strong>
        <span>{task.error_summary || (language === "zh-CN" ? "下载引擎未返回详细信息" : "The download engine did not return details")}</span>
        {task.error_code && <code>{task.error_code}</code>}
      </div>}
    </div>
    <div className="task-actions" aria-label={t("actions")}>
      {actions.filter((action) => available.has(action)).map((action) => <button key={action} className={`icon-button task-action ${action === "cancel" ? "task-action-danger" : ""}`} disabled={pending} onClick={() => onAction(task, action)} title={t(actionLabels[action])} aria-label={t(actionLabels[action])}><Icon name={action === "retry" ? "refresh" : action === "cancel" ? "close" : action === "resume" ? "play" : action} size={15} /></button>)}
    </div>
    {task.status === "awaiting_selection" && <MagnetSelection taskId={task.id} onSelected={onSelected} />}
  </article>;
}

function MagnetSelection({ taskId, onSelected }: { taskId: string; onSelected: () => void }) {
  const { language } = useLanguage();
  const zh = language === "zh-CN";
  const [files, setFiles] = useState<TorrentCandidate[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [revision, setRevision] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = async (next?: string | null) => {
    setBusy(true); setError(null);
    try {
      const page = await getTaskCandidates(taskId, next);
      setFiles((current) => next ? [...current, ...page.items] : page.items);
      setCursor(page.next_cursor); setRevision(page.metadata_revision);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, [taskId]);
  const confirm = async () => {
    if (!revision || !selected.length) return;
    setBusy(true); setError(null);
    try { await selectMagnetFiles(taskId, selected, revision); onSelected(); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  return <div className="magnet-selection"><strong>{zh ? "选择要下载的文件" : "Select files to download"}</strong>
    <div className="magnet-files">{files.map((file) => <label className="checkbox-row" key={file.index}><input type="checkbox" checked={selected.includes(file.index)} onChange={(event) => setSelected((items) => event.target.checked ? [...items, file.index] : items.filter((index) => index !== file.index))} /><span>{file.path} ({formatBytes(file.size_bytes)})</span></label>)}</div>
    {error && <div className="form-error">{error}</div>}
    <div className="magnet-selection-actions">{cursor && <button className="button button-secondary" type="button" disabled={busy} onClick={() => void load(cursor)}>{zh ? "加载更多" : "Load more"}</button>}<button className="button button-primary" type="button" disabled={busy || !selected.length} onClick={() => void confirm()}>{zh ? "确认选择" : "Confirm selection"}</button></div>
  </div>;
}

function QueueSummary({ queue, blockedReason }: { queue: QueueResponse | null; blockedReason?: string | null }) {
  const { t } = useLanguage();
  if (!queue && !blockedReason) return null;
  const items = queue?.items || [];
  const reason = queue?.blocked_reason || blockedReason;
  return <div className="queue-inline"><div><Icon name="download" size={15} /><strong>{t("queue")}</strong><span>{items.length ? `${items.length} ${t("waiting").toLowerCase()}` : t("noQueue")}</span></div>{reason && <span className="queue-blocked">{t("queueBlocked")}: {reason}</span>}</div>;
}

function Downloads() {
  const { t, language } = useLanguage();
  const location = useLocation();
  const navigate = useNavigate();
  const creating = new URLSearchParams(location.search).has("new");
  const events = useEvents();
  const loadSequence = useRef(0);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [queue, setQueue] = useState<QueueResponse | null>(null);
  const [summary, setSummary] = useState<SystemSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [pendingId, setPendingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState(false);

  const load = useCallback(async (initial = false) => {
    const sequence = ++loadSequence.current;
    if (initial) setLoading(true);
    const [taskResult, queueResult, summaryResult] = await Promise.allSettled([getTasks("general"), getQueue(), getSystemSummary()]);
    if (sequence !== loadSequence.current) return;
    if (taskResult.status === "fulfilled") { setTasks(taskResult.value.items); setError(false); } else if (initial) setError(true);
    if (queueResult.status === "fulfilled") setQueue(queueResult.value);
    if (summaryResult.status === "fulfilled") setSummary(summaryResult.value);
    // A live refresh may supersede the initial request. Whichever accepted
    // request finishes last owns the UI state and must dismiss the spinner.
    setLoading(false);
  }, []);

  useEffect(() => { void load(true); }, [load]);
  useEffect(() => { if (events.revision) void load(); }, [events.revision, load]);
  useEffect(() => { const timer = window.setInterval(() => void load(), events.state === "connected" ? 15000 : 5000); return () => window.clearInterval(timer); }, [events.state, load]);

  const activeCount = summary?.running_tasks ?? tasks.filter((task) => ["resolving", "downloading", "postprocessing"].includes(task.status)).length;

  const handleAction = async (task: Task, action: TaskAction) => {
    setPendingId(task.id); setActionError(false);
    try { const updated = await taskAction(task.id, action); setTasks((current) => current.map((item) => item.id === task.id ? updated : item)); await load(); }
    catch { setActionError(true); }
    finally { setPendingId(null); }
  };

  return <>
    <PageHeader title={t("generalDownloads")} description={t("downloadsDescription")} action={<Link className="button button-primary" to="/downloads?new=1"><Icon name="plus" size={17} />{t("newDownload")}</Link>} />
    <section className="metrics-grid">
      <div className="metric-card"><span>{t("activeTasks")}</span><strong>{activeCount}</strong><small>{t("queue")}</small></div>
      <div className="metric-card"><span>{t("downloadSpeed")}</span><strong>{formatSpeed(summary?.download_speed_bps)}</strong><small>{summary?.download_speed_bps ? "" : t("notAvailable")}</small></div>
      <div className="metric-card"><span>{t("diskAvailable")}</span><strong>{formatBytes(summary?.disk_free_bytes)}</strong><small>{summary ? "" : t("notAvailable")}</small></div>
    </section>
    <section className="content-panel">
      <div className="panel-toolbar"><PanelHeading>{language === "zh-CN" ? "进行中 / 失败" : "Active / failed"}</PanelHeading><span aria-live="polite" className="stream-state">{events.state === "connected" ? (language === "zh-CN" ? "实时更新" : "Live") : (language === "zh-CN" ? "连接中，定时刷新" : "Reconnecting; polling")}</span><button className="filter-button" onClick={() => void load()}><Icon name="refresh" size={15} />{t("refresh")}</button></div>
      <QueueSummary queue={queue} blockedReason={summary?.blocked_reason} />
      {actionError && <div className="inline-error task-list-error">{t("taskActionFailed")}</div>}
      {loading ? <LoadingState /> : error ? <ErrorState onRetry={() => void load(true)} /> : tasks.length === 0 ? <div className="empty-state"><div className="empty-icon"><Icon name="download" size={24} /></div><h2>{t("noDownloads")}</h2><p>{t("noDownloadsDescription")}</p><Link className="button button-secondary" to="/downloads?new=1"><Icon name="plus" size={16} />{t("newDownload")}</Link></div> : <div className="task-list">{tasks.map((task) => <TaskRow key={task.id} task={task} pending={pendingId === task.id} onAction={handleAction} onSelected={() => void load()} />)}</div>}
    </section>
    {creating && <CreateDialog title={t("newDownload")} description={t("downloadsDescription")} onClose={() => navigate("/downloads")}><NewDownload onCreated={() => { navigate("/downloads"); void load(); }} onCancel={() => navigate("/downloads")} /></CreateDialog>}
  </>;
}

export default Downloads;
