import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import type { FormEvent } from "react";
import { Icon } from "../components/icons";
import { EmptyState, ErrorState, LoadingState, PageHeader, PanelHeading } from "../components/workspace";
import { confirmFileDeletion, createDirectory, fileContentUrl, getFileOperation, getFiles, previewDirectoryDeletion, previewFileDeletion, type DirectoryEntry, type FileEntry, type FileRecord, type FileOperation } from "../lib/api";
import { useLanguage } from "../lib/i18n";

function formatBytes(bytes: number | null | undefined) {
  if (bytes === null || bytes === undefined || Number.isNaN(bytes)) return "—";
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let index = -1;
  do { value /= 1024; index += 1; } while (value >= 1024 && index < units.length - 1);
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`;
}

function formatDate(mtimeNs: number | null | undefined, language: "zh-CN" | "en") {
  if (mtimeNs === null || mtimeNs === undefined) return "—";
  const date = new Date(mtimeNs / 1_000_000);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(language === "zh-CN" ? "zh-CN" : "en", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function isDirectory(entry: FileEntry): entry is DirectoryEntry {
  return entry.type === "directory";
}

function fileName(entry: FileRecord) {
  return entry.name || entry.display_name || entry.relative_path || "—";
}

function FileRow({ entry, onOpen, onDelete, onDeleteDirectory, selected, onToggle }: { entry: FileEntry; onOpen: (directory: string) => void; onDelete: (entry: FileRecord) => void; onDeleteDirectory: (entry: DirectoryEntry) => void; selected: boolean; onToggle: (id: string) => void }) {
  const { t, language } = useLanguage();
  if (isDirectory(entry)) {
    return <article className="task-row">
      <div className="task-main">
        <div className="task-title-line"><Icon name="folder" size={16} /><strong>{entry.name}</strong><span className="task-status">{t("folder")}</span></div>
        <div className="task-source">{entry.relative_path}</div>
        <div className="task-meta"><span>{t("modified")}: {formatDate(entry.mtime_ns, language)}</span></div>
      </div>
      <div className="task-actions"><button className="icon-button" onClick={() => onOpen(entry.relative_path)} title={t("folder")} aria-label={t("folder")}><Icon name="chevron" size={17} /></button><button className="icon-button task-action-danger" onClick={() => onDeleteDirectory(entry)} title={language === "zh-CN" ? "删除目录" : "Delete folder"} aria-label={language === "zh-CN" ? "删除目录" : "Delete folder"}><Icon name="close" size={16} /></button></div>
    </article>;
  }
  const available = entry.is_complete !== false && entry.availability !== "missing" && Boolean(entry.id);
  return <article className="task-row">
    {available && <input type="checkbox" className="file-select" checked={selected} onChange={() => onToggle(entry.id)} aria-label={`${language === "zh-CN" ? "选择" : "Select"} ${fileName(entry)}`} />}
    <div className="task-main">
      <div className="task-title-line"><Icon name="download" size={16} /><strong title={fileName(entry)}>{fileName(entry)}</strong>{entry.kind && <span className="task-status">{entry.kind}</span>}</div>
      <div className="task-source" title={entry.relative_path || undefined}>{entry.relative_path || t("sourceUnknown")}</div>
      <div className="task-meta"><span>{t("size")}: {formatBytes(entry.size_bytes)}</span><span>{t("modified")}: {formatDate(entry.mtime_ns, language)}</span></div>
    </div>
    <div className="task-actions">{available ? <>{(entry.mime_type?.startsWith("video/") || entry.mime_type?.startsWith("audio/")) && <Link className="icon-button" to={`/player/${encodeURIComponent(entry.id)}`} title={t("player")} aria-label={t("player")}><Icon name="video" size={16} /></Link>}<a className="icon-button" href={fileContentUrl(entry.id)} download={fileName(entry)} title={t("downloadFile")} aria-label={t("downloadFile")}><Icon name="download" size={16} /></a><button className="icon-button task-action-danger" onClick={() => onDelete(entry)} title={language === "zh-CN" ? "删除文件" : "Delete file"} aria-label={language === "zh-CN" ? "删除文件" : "Delete file"}><Icon name="close" size={16} /></button></> : <span className="phase-note">{t("notAvailable")}</span>}</div>
  </article>;
}

export default function Files() {
  const { t, language } = useLanguage();
  const [directory, setDirectory] = useState("");
  const [items, setItems] = useState<FileEntry[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [appliedQuery, setAppliedQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [deleting, setDeleting] = useState(false);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [deleteProgress, setDeleteProgress] = useState("");
  const [folderName, setFolderName] = useState("");
  const [folderOpen, setFolderOpen] = useState(false);
  const [folderError, setFolderError] = useState("");

  const load = useCallback(async (cursor: string | null = null, append = false) => {
    if (append) setLoadingMore(true); else setLoading(true);
    try {
      const response = await getFiles({ directory, query: appliedQuery, cursor, limit: 30 });
      setItems((current) => append ? [...current, ...response.items] : response.items);
      setNextCursor(response.next_cursor || null);
      setError(false);
    } catch {
      if (!append) setError(true);
    } finally {
      if (append) setLoadingMore(false); else setLoading(false);
    }
  }, [directory, appliedQuery]);

  useEffect(() => { void load(); }, [load]);

  const crumbs = useMemo(() => directory.split("/").filter(Boolean).map((part, index, parts) => ({ name: part, path: parts.slice(0, index + 1).join("/") })), [directory]);
  const openDirectory = (path: string) => { setDirectory(path); setNextCursor(null); setSelectedIds([]); };
  const submitSearch = (event: FormEvent<HTMLFormElement>) => { event.preventDefault(); setAppliedQuery(query.trim()); setNextCursor(null); setSelectedIds([]); };
  const toggleSelected = (id: string) => setSelectedIds((current) => current.includes(id) ? current.filter((value) => value !== id) : current.length < 100 ? [...current, id] : current);
  const waitForDeletion = async (initial: FileOperation) => {
    let result = initial;
    for (let attempts = 0; !["completed", "partial"].includes(result.status) && attempts < 120; attempts += 1) {
      setDeleteProgress(`${result.items.length}/${result.total}`);
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      result = await getFileOperation(result.id);
    }
    await load();
    setSelectedIds([]);
    if (result.status !== "completed") throw new Error("Deletion incomplete");
  };
  const deleteFiles = async (entries: FileRecord[]) => {
    if (deleting) return;
    setDeleting(true); setDeleteError(""); setDeleteProgress("");
    try {
      const preview = await previewFileDeletion(entries.map((entry) => entry.id));
      const message = language === "zh-CN"
        ? `永久删除 ${entries.length} 个文件（${formatBytes(preview.total_bytes)}）？此操作无法从 Grabbit 恢复。`
        : `Permanently delete ${entries.length} file(s) (${formatBytes(preview.total_bytes)})? Grabbit cannot recover them.`;
      if (window.confirm(message)) {
        await waitForDeletion(await confirmFileDeletion(preview.preview_id));
      }
    } catch { setDeleteError(language === "zh-CN" ? "删除失败：文件可能已改变或仍被任务占用，请刷新后重试。" : "Deletion failed: the file may have changed or be in use. Refresh and try again."); }
    finally { setDeleting(false); setDeleteProgress(""); }
  };
  const deleteDirectory = async (entry: DirectoryEntry) => {
    if (deleting) return;
    setDeleting(true); setDeleteError(""); setDeleteProgress("");
    try {
      const preview = await previewDirectoryDeletion(entry.relative_path);
      const message = language === "zh-CN"
        ? `永久删除目录“${entry.name}”及其下 ${preview.file_count} 个文件、${preview.directory_count} 个目录（${formatBytes(preview.total_bytes)}）？此操作无法从 Grabbit 恢复。`
        : `Permanently delete “${entry.name}” and its ${preview.file_count} file(s), ${preview.directory_count} folder(s) (${formatBytes(preview.total_bytes)})? Grabbit cannot recover them.`;
      if (window.confirm(message)) await waitForDeletion(await confirmFileDeletion(preview.preview_id));
    } catch { setDeleteError(language === "zh-CN" ? "删除目录失败：内容可能已改变、目录过大或正被任务占用，请刷新后重试。" : "Folder deletion failed: contents may have changed, the folder may be too large, or a task may be using it. Refresh and retry."); }
    finally { setDeleting(false); setDeleteProgress(""); }
  };
  const addFolder = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault(); setFolderError("");
    try { await createDirectory(directory, folderName.trim()); setFolderName(""); setFolderOpen(false); await load(); }
    catch { setFolderError(language === "zh-CN" ? "无法创建目录；请检查名称是否有效或是否重名。" : "Could not create directory; check its name or whether it already exists."); }
  };

  return <>
    <PageHeader title={t("files")} description={t("filesDescription")} />
    <section className="content-panel">
      <div className="panel-toolbar">
        <PanelHeading icon="folder">{directory || t("rootFolder")}</PanelHeading>
        <div className="file-search"><button className="filter-button" onClick={() => setFolderOpen((value) => !value)}><Icon name="plus" size={15} />{language === "zh-CN" ? "新建目录" : "New folder"}</button><form onSubmit={submitSearch}><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("search")} aria-label={t("search")} /><button className="filter-button" type="submit"><Icon name="search" size={15} />{t("search")}</button></form></div>
      </div>
      {folderOpen && <form className="file-create-form" onSubmit={(event) => void addFolder(event)}><label>{language === "zh-CN" ? "目录名称" : "Folder name"}<input value={folderName} maxLength={255} onChange={(event) => setFolderName(event.target.value)} required autoFocus /></label><button className="button button-primary" type="submit">{language === "zh-CN" ? "创建" : "Create"}</button>{folderError && <span className="inline-error" role="alert">{folderError}</span>}</form>}
      {directory && <div className="file-breadcrumbs"><button className="back-link" onClick={() => openDirectory("")}>{t("rootFolder")}</button>{crumbs.map((crumb) => <span key={crumb.path}> / <button className="back-link" onClick={() => openDirectory(crumb.path)}>{crumb.name}</button></span>)}</div>}
      {deleteError && <div className="form-error task-list-error" role="alert">{deleteError}</div>}
      {deleting && selectedIds.length === 0 && <div className="file-selection-bar" role="status">{language === "zh-CN" ? "删除中" : "Deleting"} {deleteProgress}</div>}
      {selectedIds.length > 0 && <div className="file-selection-bar"><span>{language === "zh-CN" ? `已选择 ${selectedIds.length} 个文件` : `${selectedIds.length} file(s) selected`}</span><button className="button button-secondary" onClick={() => setSelectedIds([])} disabled={deleting}>{language === "zh-CN" ? "清除选择" : "Clear"}</button><button className="button button-danger" onClick={() => void deleteFiles(items.filter((entry): entry is FileRecord => !isDirectory(entry) && selectedIds.includes(entry.id)))} disabled={deleting}>{deleting ? `${language === "zh-CN" ? "删除中" : "Deleting"} ${deleteProgress}` : (language === "zh-CN" ? "删除选中文件" : "Delete selected")}</button></div>}
      {loading ? <LoadingState /> : error ? <ErrorState onRetry={() => void load()} /> : items.length === 0 ?
        <EmptyState icon="folder" title={t("noFiles")} description={t("noFilesDescription")} /> :
        <>
          <div className="task-list">{items.map((entry) => <FileRow key={entry.relative_path} entry={entry} onOpen={openDirectory} onDelete={(file) => void deleteFiles([file])} onDeleteDirectory={(folder) => void deleteDirectory(folder)} selected={!isDirectory(entry) && selectedIds.includes(entry.id)} onToggle={toggleSelected} />)}</div>
          {nextCursor && <div className="settings-footer"><button className="button button-secondary" onClick={() => void load(nextCursor, true)} disabled={loadingMore}>{loadingMore ? t("loading") : (language === "zh-CN" ? "加载更多" : "Load more")}</button></div>}
        </>
      }
    </section>
  </>;
}
