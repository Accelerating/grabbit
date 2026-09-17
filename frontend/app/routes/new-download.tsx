import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { Checkbox } from "../components/ui/checkbox";
import { Field, FieldContent, FieldDescription, FieldLabel } from "../components/ui/field";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Textarea } from "../components/ui/textarea";
import { createGeneralTasks, createTorrentTask, getTorrentFiles, uploadTorrent, type TorrentCandidate, type TorrentUpload, type ApiError } from "../lib/api";
import { useLanguage } from "../lib/i18n";

export default function NewDownload({ onCreated, onCancel }: { onCreated?: () => void; onCancel?: () => void } = {}) {
  const { t, language } = useLanguage();
  const navigate = useNavigate();
  const [sources, setSources] = useState("");
  const [downloadSubdir, setDownloadSubdir] = useState("general");
  const [allowDuplicates, setAllowDuplicates] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [torrent, setTorrent] = useState<TorrentUpload | null>(null);
  const [candidates, setCandidates] = useState<TorrentCandidate[]>([]);
  const [selected, setSelected] = useState<number[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [torrentBusy, setTorrentBusy] = useState(false);
  const isZh = language === "zh-CN";

  const chooseTorrent = async (file?: File) => {
    if (!file) return;
    setTorrentBusy(true); setError(null); setTorrent(null);
    try {
      const result = await uploadTorrent(file);
      setTorrent(result); setCandidates(result.files.items);
      setSelected(result.files.items.map((item) => item.index));
      setNextCursor(result.files.next_cursor);
    } catch (cause) { setError((cause as ApiError).message || t("error")); }
    finally { setTorrentBusy(false); }
  };

  const loadMoreTorrent = async () => {
    if (!torrent || !nextCursor) return;
    setTorrentBusy(true);
    try {
      const page = await getTorrentFiles(torrent.torrent_id, nextCursor);
      setCandidates((items) => [...items, ...page.items]);
      setSelected((items) => [...items, ...page.items.map((item) => item.index)]);
      setNextCursor(page.next_cursor);
    } catch (cause) { setError((cause as ApiError).message || t("error")); }
    finally { setTorrentBusy(false); }
  };

  const submitTorrent = async () => {
    if (!torrent || !selected.length) return;
    setTorrentBusy(true); setError(null);
    try { await createTorrentTask({ torrent_id: torrent.torrent_id, selected_indices: selected, download_subdir: downloadSubdir.trim() || "general", allow_duplicates: allowDuplicates }); onCreated ? onCreated() : navigate("/downloads"); }
    catch (cause) { setError((cause as ApiError).message || t("error")); }
    finally { setTorrentBusy(false); }
  };

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const sourceList = sources.split(/\r?\n/).map((source) => source.trim()).filter(Boolean);
    if (!sourceList.length) { setError(t("invalidSources")); return; }
    setSubmitting(true); setError(null);
    try { await createGeneralTasks({ sources: sourceList, download_subdir: downloadSubdir.trim() || "general", allow_duplicates: allowDuplicates }); onCreated ? onCreated() : navigate("/downloads"); }
    catch (cause) { setError((cause as ApiError).message || t("error")); }
    finally { setSubmitting(false); }
  };

  return <div className="creation-stack">
    <Card className="creation-card">
      <CardHeader><CardTitle>{t("sourceUrls")}</CardTitle><CardDescription>{t("sourceUrlsHint")}</CardDescription></CardHeader>
      <form onSubmit={submit} className="creation-form">
        <CardContent className="creation-fields">
          <Field><FieldLabel htmlFor="download-sources">{t("sourceUrls")}</FieldLabel><Textarea id="download-sources" value={sources} onChange={(event) => setSources(event.target.value)} placeholder="https://example.com/file.zip" className="creation-textarea" autoFocus /></Field>
          <Field><FieldLabel htmlFor="download-subdir">{t("downloadSubdirectory")}</FieldLabel><Input id="download-subdir" value={downloadSubdir} onChange={(event) => setDownloadSubdir(event.target.value)} placeholder="general" /><FieldDescription>{t("downloadSubdirectoryHint")}</FieldDescription></Field>
          <Field orientation="horizontal" className="creation-check"><Checkbox id="download-duplicate" checked={allowDuplicates} onCheckedChange={(value) => setAllowDuplicates(value === true)} /><FieldContent><FieldLabel htmlFor="download-duplicate">{t("allowDuplicates")}</FieldLabel><FieldDescription>{t("allowDuplicatesHint")}</FieldDescription></FieldContent></Field>
          {error && <div className="form-error" role="alert">{error}</div>}
        </CardContent>
        <CardFooter className="creation-actions">{onCancel ? <Button variant="outline" type="button" onClick={onCancel}>{isZh ? "取消" : "Cancel"}</Button> : <Button variant="outline" asChild><Link to="/downloads">{t("backToDownloads")}</Link></Button>}<Button type="submit" disabled={submitting}>{submitting ? t("creating") : t("submitDownload")}</Button></CardFooter>
      </form>
    </Card>
    <Card className="creation-card">
      <CardHeader><CardTitle>{isZh ? "上传种子文件" : "Upload torrent"}</CardTitle><CardDescription>{isZh ? "选择文件后确认要下载的内容。" : "Choose the files to download before starting."}</CardDescription></CardHeader>
      <CardContent className="creation-fields">
        <Field><FieldLabel htmlFor="torrent-file">{isZh ? "种子文件" : "Torrent file"}</FieldLabel><Input id="torrent-file" type="file" accept=".torrent,application/x-bittorrent" onChange={(event) => void chooseTorrent(event.target.files?.[0])} disabled={torrentBusy} /></Field>
        {torrent && <><p className="creation-file-summary">{torrent.summary.name} · {torrent.summary.file_count} {isZh ? "个文件" : "files"}</p>
          <div className="creation-file-list">{candidates.map((file) => <div className="creation-file-row" key={file.index}><Checkbox id={`torrent-${file.index}`} checked={selected.includes(file.index)} onCheckedChange={(value) => setSelected((items) => value === true ? [...items, file.index] : items.filter((index) => index !== file.index))} /><Label htmlFor={`torrent-${file.index}`}>{file.path} ({file.size_bytes} B)</Label></div>)}</div>
          {nextCursor && <Button variant="outline" type="button" onClick={() => void loadMoreTorrent()} disabled={torrentBusy}>{isZh ? "加载更多文件" : "Load more files"}</Button>}
          <Button type="button" onClick={() => void submitTorrent()} disabled={torrentBusy || !selected.length || Boolean(nextCursor)}>{isZh ? "创建种子任务" : "Create torrent task"}</Button>
          {nextCursor && <p className="creation-hint">{isZh ? "请先加载全部文件再确认选择。" : "Load all files before confirming selection."}</p>}
        </>}
      </CardContent>
    </Card>
  </div>;
}
