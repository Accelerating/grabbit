import { useCallback, useEffect, useState } from "react";
import { Icon } from "../components/icons";
import { EmptyState, ErrorState, LoadingState, PageHeader, PanelHeading } from "../components/workspace";
import { deleteTaskRecord, getHistory, previewTaskDeletion, retryTaskSubtitles, type Task } from "../lib/api";
import { useLanguage, type TranslationKey } from "../lib/i18n";

const terminalStatusKeys: Record<string, TranslationKey> = {
  completed: "completed", failed: "failed", cancelled: "cancelled", stopped: "stopped",
};

function taskTitle(task: Task, fallback: string) {
  return task.title?.trim() || task.source_url?.trim() || fallback;
}

function formatDate(value: string | null | undefined, language: "zh-CN" | "en") {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(language === "zh-CN" ? "zh-CN" : "en", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function HistoryRow({ task, onDelete, onRetrySubtitles }: { task: Task; onDelete: (task: Task) => void; onRetrySubtitles: (task: Task) => void }) {
  const { t, language } = useLanguage();
  const statusKey = terminalStatusKeys[task.status];
  return <article className="task-row">
    <div className="task-main">
      <div className="task-title-line">
        <span className={`task-status-dot status-${task.status}`} />
        <strong title={taskTitle(task, t("sourceUnknown"))}>{taskTitle(task, t("sourceUnknown"))}</strong>
        <span className={`task-status status-${task.status}`}>{statusKey ? t(statusKey) : task.status}</span>
      </div>
      <div className="task-source" title={task.source_url || undefined}>
        {task.source_url || (task.kind ? `${t("type")}: ${task.kind}` : t("sourceUnknown"))}
      </div>
      <div className="task-meta">
        <span>{t("updated")}: {formatDate(task.updated_at || task.created_at, language)}</span>
        {task.phase && <span>{task.phase}</span>}
        {task.error_summary && <span title={task.error_summary}>{task.error_summary}</span>}
        {task.warnings?.includes("SUBTITLE_FAILED") && <span>{language === "zh-CN" ? "字幕获取失败，媒体已保存" : "Subtitles failed; media was saved"}</span>}
      </div>
    </div>
    <div className="task-actions">{task.kind === "video" && task.status === "completed" && task.warnings?.includes("SUBTITLE_FAILED") && <button className="button button-secondary" disabled={task.phase === "subtitle_queued" || task.phase === "subtitle_retry"} onClick={() => onRetrySubtitles(task)}>{task.phase === "subtitle_queued" || task.phase === "subtitle_retry" ? (language === "zh-CN" ? "字幕重试中" : "Retrying subtitles") : (language === "zh-CN" ? "重试字幕" : "Retry subtitles")}</button>}<button className="button button-secondary" onClick={() => onDelete(task)}>{language === "zh-CN" ? "删除记录" : "Delete record"}</button></div>
  </article>;
}

export default function History() {
  const { t, language } = useLanguage();
  const [items, setItems] = useState<Task[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(false);
  const [actionError, setActionError] = useState("");
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async (cursor: string | null = null, append = false) => {
    if (append) setLoadingMore(true); else setLoading(true);
    try {
      const response = await getHistory({ cursor, limit: 30 });
      setItems((current) => append ? [...current, ...response.items] : response.items);
      setNextCursor(response.next_cursor || null);
      setError(false);
    } catch {
      if (!append) setError(true);
    } finally {
      if (append) setLoadingMore(false); else setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const removeRecord = async (task: Task) => {
    if (deleting) return;
    setDeleting(true); setActionError("");
    try {
      const preview = await previewTaskDeletion(task.id);
      const names = preview.files.slice(0, 5).map((file) => file.name).join("\n");
      const confirmText = language === "zh-CN"
        ? `删除“${taskTitle(task, t("taskTitle"))}”的历史记录？\n${preview.files.length} 个关联文件会保留在磁盘上。${names ? `\n${names}` : ""}`
        : `Delete the history record for “${taskTitle(task, t("taskTitle"))}”?\n${preview.files.length} associated files will remain on disk.${names ? `\n${names}` : ""}`;
      if (window.confirm(confirmText)) { await deleteTaskRecord(task.id, preview.task_revision); await load(); }
    } catch { setActionError(language === "zh-CN" ? "删除失败，任务可能已改变；请刷新后重试。" : "Deletion failed. The task may have changed; refresh and try again."); }
    finally { setDeleting(false); }
  };
  const retrySubtitles = async (task: Task) => {
    setActionError("");
    try { await retryTaskSubtitles(task.id); await load(); }
    catch { setActionError(language === "zh-CN" ? "无法重试字幕，请检查任务状态或稍后再试。" : "Could not retry subtitles; check the task state and try again."); }
  };

  return <>
    <PageHeader title={t("history")} description={t("historyDescription")} />
    <section className="content-panel">
      <div className="panel-toolbar">
        <PanelHeading>{t("all")}</PanelHeading>
        <button className="filter-button" onClick={() => void load()} disabled={loading}><Icon name="refresh" size={15} />{t("refresh")}</button>
      </div>
      {loading ? <LoadingState /> : error ? <ErrorState onRetry={() => void load()} /> : items.length === 0 ?
        <EmptyState icon="history" title={t("noHistory")} description={t("noHistoryDescription")} /> :
        <>
          {actionError && <div className="form-error task-list-error" role="alert">{actionError}</div>}
          <div className="task-list">{items.map((task) => <HistoryRow key={task.id} task={task} onDelete={(item) => void removeRecord(item)} onRetrySubtitles={(item) => void retrySubtitles(item)} />)}</div>
          {nextCursor && <div className="settings-footer"><button className="button button-secondary" onClick={() => void load(nextCursor, true)} disabled={loadingMore}>{loadingMore ? t("loading") : (language === "zh-CN" ? "加载更多" : "Load more")}</button></div>}
        </>
      }
    </section>
  </>;
}
