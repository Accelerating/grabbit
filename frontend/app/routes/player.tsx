import { useEffect, useState } from "react";
import { Link, Navigate, useParams } from "react-router";
import { Icon } from "../components/icons";
import { ErrorState, LoadingState } from "../components/workspace";
import { fileContentUrl, fileInlineUrl, getFileDetail, getSubtitleTracks, queueFileProbe, subtitleTrackUrl, type FileRecord, type SubtitleTrack } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useLanguage } from "../lib/i18n";

function durationLabel(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.round(seconds);
  return `${Math.floor(total / 3600) ? `${Math.floor(total / 3600)}:` : ""}${String(Math.floor(total / 60) % 60).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

export default function Player() {
  const { fileId } = useParams();
  const { session, loading: authLoading } = useAuth();
  const { t, language } = useLanguage();
  const [record, setRecord] = useState<FileRecord | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [playError, setPlayError] = useState(false);
  const [tracks, setTracks] = useState<SubtitleTrack[]>([]);
  useEffect(() => {
    if (!fileId || !session) return;
    let live = true;
    setLoading(true);
    void getFileDetail(fileId).then((value) => { if (live) { setRecord(value); setError(false); } }).catch(() => { if (live) setError(true); }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; };
  }, [fileId, session?.user.id]);
  useEffect(() => {
    if (!fileId || !session || !record || record.id !== fileId) return;
    const mime = record.mime_type || "";
    if (!mime.startsWith("video/") && !mime.startsWith("audio/")) return;
    if (record.availability === "missing" || record.is_complete === false) return;
    let live = true;
    if (!record.probe_status || record.probe_status === "unprobed") {
      void queueFileProbe(fileId).then((value) => { if (live) setRecord(value); }).catch(() => {});
    } else if (["queued", "running"].includes(record.probe_status)) {
      const timer = window.setTimeout(() => { void getFileDetail(fileId).then((value) => { if (live) setRecord(value); }).catch(() => {}); }, 1500);
      return () => { live = false; window.clearTimeout(timer); };
    }
    return () => { live = false; };
  }, [fileId, session?.user.id, record?.probe_status, record?.id]);
  useEffect(() => {
    if (!fileId || !session) return;
    let live = true;
    void getSubtitleTracks(fileId).then((response) => { if (live) setTracks(response.items); }).catch(() => { if (live) setTracks([]); });
    return () => { live = false; };
  }, [fileId, session?.user.id]);

  if (authLoading) return <LoadingState />;
  if (!session) return <Navigate to="/login" replace />;
  const mime = record?.mime_type || "";
  const playable = record?.is_complete !== false && record?.availability !== "missing" && (mime.startsWith("video/") || mime.startsWith("audio/")) && (record?.probe_status !== "completed" || Boolean(record.media?.streams.length));
  const title = record?.name || record?.display_name || t("player");
  const media = record?.media;
  return <div className="player-page">
    <div className="player-header"><Link to="/files" className="back-link"><Icon name="chevron" size={17} />{t("files")}</Link><span className="eyebrow">{t("player")}</span></div>
    <section className="content-panel player-panel">
      {loading ? <LoadingState /> : error || !record ? <ErrorState onRetry={() => { setLoading(true); setError(false); void getFileDetail(fileId || "").then(setRecord).catch(() => setError(true)).finally(() => setLoading(false)); }} /> : <>
        <h1>{title}</h1>
        {playable && fileId ? mime.startsWith("audio/") ? <audio controls preload="metadata" src={fileInlineUrl(fileId)} onError={() => setPlayError(true)} /> : <video controls preload="metadata" playsInline src={fileInlineUrl(fileId)} onError={() => setPlayError(true)}>{tracks.map((track) => <track key={track.id} kind="subtitles" src={subtitleTrackUrl(fileId, track.id)} srcLang={track.language} label={track.label} />)}</video> : <p>{t("mediaUnavailableDescription")}</p>}
        {record.probe_status === "queued" || record.probe_status === "running" ? <p className="player-probe-note" role="status">{language === "zh-CN" ? "正在读取媒体信息…" : "Reading media information…"}</p> : null}
        {record.probe_status === "failed" && <p className="player-probe-note">{language === "zh-CN" ? "无法读取媒体信息；仍可尝试浏览器播放或下载原文件。" : "Media information is unavailable; you can still try playback or download the file."}</p>}
        {media && <dl className="player-media-info"><div><dt>{language === "zh-CN" ? "时长" : "Duration"}</dt><dd>{durationLabel(media.duration_seconds)}</dd></div><div><dt>{language === "zh-CN" ? "封装" : "Container"}</dt><dd>{media.format_name || "—"}</dd></div>{media.streams.map((stream, index) => <div key={`${stream.codec_type}-${index}`}><dt>{stream.codec_type === "video" ? (language === "zh-CN" ? "视频" : "Video") : (language === "zh-CN" ? "音频" : "Audio")}</dt><dd>{stream.codec_name || "—"}{stream.width && stream.height ? ` · ${stream.width}×${stream.height}` : ""}{stream.channels ? ` · ${stream.channels} ch` : ""}</dd></div>)}</dl>}
        {playError && <p className="form-error" role="alert">{language === "zh-CN" ? "浏览器无法播放此文件的编码或容器。可以下载原文件后使用本地播放器。" : "This browser cannot play the file's codec or container. Download the original file to use a local player."}</p>}
        <div className="player-actions"><span>{mime || t("notAvailable")}</span>{fileId && <a className="button button-secondary" href={fileContentUrl(fileId)} download={title}><Icon name="download" size={16} />{t("downloadFile")}</a>}</div>
      </>}
    </section>
  </div>;
}
