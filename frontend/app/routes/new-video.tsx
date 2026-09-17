import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Checkbox } from "../components/ui/checkbox";
import { Field, FieldContent, FieldDescription, FieldLabel } from "../components/ui/field";
import { Input } from "../components/ui/input";
import { NativeSelect, NativeSelectOption } from "../components/ui/native-select";
import { cancelVideoAnalysis, createCollectionVideoTasks, createVideoTask, createVideoAnalysis, getVideoAnalysis, getVideoAnalysisItems, getCookieProfiles, loadVideoAnalysisPage, type CookieProfile, type VideoAnalysis, type VideoAnalysisItem } from "../lib/api";
import { useLanguage } from "../lib/i18n";

const copy = {
  en: { url: "Video URL", cookie: "Cookie profile", none: "Do not use Cookies", resolve: "Analyze video", working: "Analyzing…", cancel: "Cancel analysis", failed: "Could not analyze this video. Check the URL, tool installation and site access.", expired: "Analysis expired; analyze the URL again.", duration: "Duration", formats: "Available formats", subtitles: "Subtitle languages", noSubtitles: "No subtitles reported by this source.", noFormats: "No format details returned.", pending: "You can select collection entries and a shared download policy; each entry becomes an independent task.", website: "YouTube videos/playlists and Bilibili videos/multi-part pages.", mode: "Output", video: "Video", audio: "Audio only", maxHeight: "Maximum height", subdir: "Download subdirectory", add: "Add to queue", duplicate: "Allow duplicate source", createFailed: "Could not create task. Check for an existing download or try again." },
  "zh-CN": { url: "视频网址", cookie: "Cookie 配置", none: "不使用 Cookie", resolve: "解析视频", working: "解析中…", cancel: "取消解析", failed: "视频解析失败，请检查网址、工具安装和站点访问。", expired: "解析结果已过期，请重新解析网址。", duration: "时长", formats: "可用格式", subtitles: "字幕语言", noSubtitles: "来源未返回可用字幕。", noFormats: "未返回格式详情。", pending: "可选择合集条目与统一下载策略；每个条目会创建独立任务。", website: "支持 YouTube 单视频／播放列表和 B站单视频／多 P。", mode: "输出类型", video: "视频", audio: "仅音频", maxHeight: "最高画质", subdir: "下载子目录", add: "加入队列", duplicate: "允许重复来源", createFailed: "创建任务失败，请检查是否已有相同来源后重试。" },
} as const;

function defaultSubtitleLanguages(result: VideoAnalysis) {
  return result.collection ? ["zh.*", "en.*"] : result.subtitle_languages.filter((value) => /^(zh|en)(?:-|$)/i.test(value)).slice(0, 10);
}

export default function NewVideo({ onCreated }: { onCreated?: () => void } = {}) {
  const { t, language } = useLanguage();
  const c = copy[language];
  const navigate = useNavigate();
  const timer = useRef<number | null>(null);
  const generation = useRef(0);
  const [activeAnalysisId, setActiveAnalysisId] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [cookieId, setCookieId] = useState("");
  const [profiles, setProfiles] = useState<CookieProfile[]>([]);
  const [result, setResult] = useState<VideoAnalysis | null>(null);
  const [analysisId, setAnalysisId] = useState<string | null>(null);
  const [collectionItems, setCollectionItems] = useState<VideoAnalysisItem[]>([]);
  const [selectedItemIds, setSelectedItemIds] = useState<string[]>([]);
  const [collectionCursor, setCollectionCursor] = useState<number | null>(null);
  const [nextSourceIndex, setNextSourceIndex] = useState<number | null>(null);
  const [collectionSubtitles, setCollectionSubtitles] = useState(true);
  const [selectedSubtitleLanguages, setSelectedSubtitleLanguages] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);
  const [mode, setMode] = useState<"video" | "audio">("video");
  const [height, setHeight] = useState(1080);
  const [subdir, setSubdir] = useState("videos");
  const [allowDuplicates, setAllowDuplicates] = useState(false);

  const showCompleted = async (id: string, value: VideoAnalysis) => {
    setResult(value); setAnalysisId(id); setSelectedSubtitleLanguages(defaultSubtitleLanguages(value)); setBusy(false); setActiveAnalysisId(null);
    if (value.collection) {
      const page = await getVideoAnalysisItems(id);
      setCollectionItems(page.items); setSelectedItemIds(page.items.filter((item) => item.available).map((item) => item.id));
      setCollectionCursor(page.next_cursor); setNextSourceIndex(page.next_source_index);
    }
  };

  useEffect(() => { void getCookieProfiles().then((response) => setProfiles(response.items)).catch(() => setProfiles([])); }, []);
  useEffect(() => {
    const id = sessionStorage.getItem("grabbit-video-analysis-id");
    if (!id) return;
    let cancelled = false;
    const currentGeneration = ++generation.current;
    setActiveAnalysisId(id);
    const poll = async () => {
      try {
        const job = await getVideoAnalysis(id);
        if (cancelled || generation.current !== currentGeneration) return;
        setUrl(job.url); setCookieId(job.cookie_profile_id || "");
        if (job.status === "completed" && job.result) { await showCompleted(id, job.result); }
        else if (["failed", "cancelled", "expired"].includes(job.status)) { setError(job.status === "expired" ? c.expired : job.status === "failed" ? `${c.failed} (${job.error_code || "ANALYSIS_FAILED"})` : ""); setBusy(false); setActiveAnalysisId(null); sessionStorage.removeItem("grabbit-video-analysis-id"); }
        else { setBusy(true); timer.current = window.setTimeout(() => { void poll(); }, 1500); }
      } catch { if (!cancelled) { setBusy(false); setError(c.failed); sessionStorage.removeItem("grabbit-video-analysis-id"); } }
    };
    void poll();
    return () => { cancelled = true; if (timer.current !== null) window.clearTimeout(timer.current); };
  }, [c.failed]);

  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(""); setResult(null); setAnalysisId(null); setCollectionItems([]); setSelectedItemIds([]);
    const currentGeneration = ++generation.current;
    try {
      const job = await createVideoAnalysis(url.trim(), cookieId || null);
      if (generation.current !== currentGeneration) { void cancelVideoAnalysis(job.id); return; }
      setActiveAnalysisId(job.id);
      sessionStorage.setItem("grabbit-video-analysis-id", job.id);
      const poll = async () => {
        const current = await getVideoAnalysis(job.id);
        if (generation.current !== currentGeneration) return;
        if (current.status === "completed" && current.result) { await showCompleted(job.id, current.result); }
        else if (["failed", "cancelled", "expired"].includes(current.status)) { setError(current.status === "expired" ? c.expired : current.status === "failed" ? `${c.failed} (${current.error_code || "ANALYSIS_FAILED"})` : ""); setBusy(false); setActiveAnalysisId(null); sessionStorage.removeItem("grabbit-video-analysis-id"); }
        else timer.current = window.setTimeout(() => { void poll().catch(() => { setBusy(false); setError(c.failed); }); }, 1500);
      };
      void poll().catch(() => { setBusy(false); setError(c.failed); });
    } catch { setBusy(false); setError(c.failed); }
  };
  const cancel = async () => {
    generation.current += 1;
    if (timer.current !== null) window.clearTimeout(timer.current);
    setBusy(false); setActiveAnalysisId(null);
    sessionStorage.removeItem("grabbit-video-analysis-id");
    if (activeAnalysisId) { try { await cancelVideoAnalysis(activeAnalysisId); } catch { setError(c.failed); } }
  };

  const create = async () => {
    if (!result) return;
    setCreating(true); setError("");
    try {
      const options = { cookie_profile_id: cookieId || null, mode, max_height: height, download_subdir: subdir.trim(), allow_duplicates: allowDuplicates, subtitle_languages: mode === "audio" ? [] : result.collection ? collectionSubtitles ? ["zh.*", "en.*"] : [] : selectedSubtitleLanguages };
      if (result.collection) {
        if (!analysisId || selectedItemIds.length === 0) return;
        await createCollectionVideoTasks(analysisId, { ...options, item_ids: selectedItemIds });
      } else {
        await createVideoTask({ ...options, url: url.trim(), title: result.title });
      }
      sessionStorage.removeItem("grabbit-video-analysis-id");
      onCreated ? onCreated() : navigate("/video");
    } catch { setError(c.createFailed); }
    finally { setCreating(false); }
  };
  const loadMore = async () => {
    if (!analysisId) return;
    setBusy(true); setError("");
    try {
      if (collectionCursor != null) {
        const page = await getVideoAnalysisItems(analysisId, collectionCursor);
        setCollectionItems((current) => [...current, ...page.items]);
        setSelectedItemIds((current) => [...current, ...page.items.filter((item) => item.available).map((item) => item.id)].slice(0, 500));
        setCollectionCursor(page.next_cursor); setNextSourceIndex(page.next_source_index);
      } else if (nextSourceIndex != null) {
        await loadVideoAnalysisPage(analysisId);
        let job = await getVideoAnalysis(analysisId);
        for (let attempt = 0; ["queued", "running"].includes(job.status) && attempt < 80; attempt += 1) {
          await new Promise((resolve) => window.setTimeout(resolve, 1500));
          job = await getVideoAnalysis(analysisId);
        }
        if (job.status !== "completed") throw new Error("Collection page failed");
        const page = await getVideoAnalysisItems(analysisId, collectionItems.at(-1)?.ordinal);
        setCollectionItems((current) => [...current, ...page.items]);
        setSelectedItemIds((current) => [...current, ...page.items.filter((item) => item.available).map((item) => item.id)].slice(0, 500));
        setCollectionCursor(page.next_cursor); setNextSourceIndex(page.next_source_index);
      }
    } catch { setError(c.failed); }
    finally { setBusy(false); }
  };
  const selectedSite = (() => { try { const host = new URL(url).hostname.toLowerCase(); return host.includes("youtube") || host === "youtu.be" ? "youtube" : host.includes("bilibili") || host === "b23.tv" ? "bilibili" : ""; } catch { return ""; } })();

  return <div className="creation-stack">
    <Card className="creation-card">
      <CardHeader><CardTitle>{c.resolve}</CardTitle><CardDescription>{c.website}</CardDescription></CardHeader>
      <CardContent><form className="creation-fields" onSubmit={(event) => void submit(event)}>
        <Field><FieldLabel htmlFor="video-url">{c.url}</FieldLabel><Input id="video-url" type="url" value={url} onChange={(event) => { setUrl(event.target.value); setCookieId(""); setResult(null); }} required placeholder="https://www.bilibili.com/video/BV…" /></Field>
        <Field><FieldLabel htmlFor="video-cookie">{c.cookie}</FieldLabel><NativeSelect className="w-full" id="video-cookie" value={cookieId} onChange={(event) => { setCookieId(event.target.value); setResult(null); }}><NativeSelectOption value="">{c.none}</NativeSelectOption>{profiles.filter((profile) => !selectedSite || profile.site_key === selectedSite).map((profile) => <NativeSelectOption key={profile.id} value={profile.id}>{profile.name}{profile.is_default ? " ★" : ""}</NativeSelectOption>)}</NativeSelect></Field>
        {error && <div className="form-error" role="alert">{error}</div>}
        <Button type="submit" disabled={busy}>{busy ? c.working : c.resolve}</Button>
        {busy && <Button type="button" variant="outline" onClick={() => void cancel()}>{c.cancel}</Button>}
        <p className="creation-hint">{c.pending}</p>
      </form></CardContent>
    </Card>
    <Card className="creation-card">
      <CardHeader><CardTitle>{result ? result.title : c.formats}</CardTitle><CardDescription>{result ? result.collection ? `${result.site} · ${collectionItems.length} ${language === "zh-CN" ? "个条目已加载" : "items loaded"}` : `${result.site} · ${c.duration}: ${result.duration == null ? "—" : `${Math.floor(result.duration / 60)}:${String(Math.floor(result.duration % 60)).padStart(2, "0")}`}` : c.website}</CardDescription></CardHeader>
      <CardContent className="creation-fields" aria-live="polite">
        {busy ? <p>{c.working}</p> : result ? <>
          {result.collection ? <div><h3 className="creation-section-title">{language === "zh-CN" ? `选择条目（${selectedItemIds.length}/500）` : `Select entries (${selectedItemIds.length}/500)`}</h3><div className="collection-items">{collectionItems.map((item) => <label key={item.id}><Checkbox checked={selectedItemIds.includes(item.id)} disabled={!item.available || (!selectedItemIds.includes(item.id) && selectedItemIds.length >= 500)} onCheckedChange={(checked) => setSelectedItemIds((current) => checked === true ? [...current, item.id].slice(0, 500) : current.filter((id) => id !== item.id))} /><span>{item.ordinal}. {item.title}{!item.available && ` · ${language === "zh-CN" ? "地址不可用" : "URL unavailable"}`}</span></label>)}</div>{(collectionCursor != null || nextSourceIndex != null) && <Button type="button" variant="outline" onClick={() => void loadMore()}>{language === "zh-CN" ? "加载更多条目" : "Load more entries"}</Button>}</div> : <><div><h3 className="creation-section-title">{c.formats}</h3>{result.formats.length ? <ul className="creation-format-list">{result.formats.map((format) => <li key={format.id}>{format.height ? `${format.height}p` : "audio"} · {format.ext} · {format.vcodec} / {format.acodec} <code>{format.id}</code></li>)}</ul> : <p>{c.noFormats}</p>}</div><div><h3 className="creation-section-title">{c.subtitles}</h3>{result.subtitle_languages.length ? <div className="subtitle-options">{result.subtitle_languages.slice(0, 10).map((value) => <label key={value}><Checkbox checked={selectedSubtitleLanguages.includes(value)} onCheckedChange={(checked) => setSelectedSubtitleLanguages((current) => checked === true ? [...current, value] : current.filter((item) => item !== value))} /><span>{value}</span></label>)}</div> : <p>{c.noSubtitles}</p>}</div></>}
          {result.collection && mode === "video" && <Field orientation="horizontal" className="creation-check"><Checkbox id="collection-subtitles" checked={collectionSubtitles} onCheckedChange={(checked) => setCollectionSubtitles(checked === true)} /><FieldContent><FieldLabel htmlFor="collection-subtitles">{language === "zh-CN" ? "尝试中英文字幕" : "Try Chinese and English subtitles"}</FieldLabel></FieldContent></Field>}
          <Field><FieldLabel htmlFor="video-mode">{c.mode}</FieldLabel><NativeSelect className="w-full" id="video-mode" value={mode} onChange={(event) => { const value = event.target.value as "video" | "audio"; setMode(value); setSubdir(value === "audio" ? "audio" : "videos"); }}><NativeSelectOption value="video">{c.video}</NativeSelectOption><NativeSelectOption value="audio">{c.audio}</NativeSelectOption></NativeSelect></Field>
          {mode === "video" && <Field><FieldLabel htmlFor="video-height">{c.maxHeight}</FieldLabel><NativeSelect className="w-full" id="video-height" value={height} onChange={(event) => setHeight(Number(event.target.value))}>{[720, 1080, 1440, 2160].map((value) => <NativeSelectOption key={value} value={value}>{value}p</NativeSelectOption>)}</NativeSelect></Field>}
          <Field><FieldLabel htmlFor="video-subdir">{c.subdir}</FieldLabel><Input id="video-subdir" value={subdir} onChange={(event) => setSubdir(event.target.value)} /></Field>
          <Field orientation="horizontal" className="creation-check"><Checkbox id="video-duplicate" checked={allowDuplicates} onCheckedChange={(value) => setAllowDuplicates(value === true)} /><FieldContent><FieldLabel htmlFor="video-duplicate">{c.duplicate}</FieldLabel></FieldContent></Field>
          <Button type="button" disabled={creating || !subdir.trim() || (result.collection && selectedItemIds.length === 0)} onClick={() => void create()}>{creating ? t("loading") : result.collection ? `${c.add} (${selectedItemIds.length})` : c.add}</Button>
        </> : <p className="creation-hint">{c.website}</p>}
      </CardContent>
    </Card>
  </div>;
}
