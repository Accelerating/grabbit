import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { CreateDialog } from "../components/create-dialog";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardFooter } from "../components/ui/card";
import { Field, FieldDescription, FieldLabel } from "../components/ui/field";
import { Input } from "../components/ui/input";
import { NativeSelect, NativeSelectOption } from "../components/ui/native-select";
import { Textarea } from "../components/ui/textarea";
import { EmptyState, ErrorState, LoadingState, PageHeader, PanelHeading } from "../components/workspace";
import { createCookieProfile, deleteCookieProfile, getCookieProfiles, replaceCookieContent, setCookieDefault, updateCookieProfile, type CookieProfile } from "../lib/api";
import { useLanguage } from "../lib/i18n";

const copy = {
  en: { add: "Add Cookie profile", name: "Name", site: "Site", domains: "Allowed domains", domainsHint: "Comma-separated; cookies outside this scope are rejected.", file: "Upload cookies.txt", paste: "Or paste cookies.txt", limit: "Netscape format, maximum 1 MiB. Import cannot verify whether login still works.", save: "Save profile", replace: "Replace content", edit: "Edit name / domains", makeDefault: "Set as default", clearDefault: "Remove default", default: "Default", entries: "entries", version: "version", lastUsed: "Last used", noResult: "Not used yet", delete: "Delete", deleteConfirm: "Delete this profile? Active tasks using it will block deletion.", required: "Provide the required fields and either a file or pasted content.", chooseOne: "Choose a file or paste content, not both.", tooLarge: "Cookie content exceeds 1 MiB.", failed: "Request failed. Check the fields and try again.", inUse: "An active task uses this profile. Stop or change that task first.", updated: "Profile updated.", created: "Profile added.", deleted: "Profile deleted.", changed: "Default updated.", private: "Cookie values cannot be viewed or exported after import.", refresh: "Refresh" },
  "zh-CN": { add: "添加 Cookie 配置", name: "名称", site: "站点", domains: "允许的域名", domainsHint: "逗号分隔；范围外的 Cookie 会被拒绝。", file: "上传 cookies.txt", paste: "或粘贴 cookies.txt", limit: "Netscape 格式，最大 1 MiB。导入无法验证登录是否仍有效。", save: "保存配置", replace: "替换内容", edit: "修改名称／域名", makeDefault: "设为默认", clearDefault: "取消默认", default: "默认", entries: "条", version: "版本", lastUsed: "最近使用", noResult: "尚未使用", delete: "删除", deleteConfirm: "删除此配置？正在使用它的任务会阻止删除。", required: "请填写必填项，并上传文件或粘贴内容。", chooseOne: "上传文件和粘贴内容只能选一种。", tooLarge: "Cookie 内容超过 1 MiB。", failed: "操作失败，请检查填写内容后重试。", inUse: "有活动任务正在使用此配置，请先停止任务或更换配置。", updated: "配置已更新。", created: "配置已添加。", deleted: "配置已删除。", changed: "默认配置已更新。", private: "导入后不能查看或导出 Cookie 内容。", refresh: "刷新" },
} as const;

function makeContent(file: File | null, pasted: string, language: "en" | "zh-CN") {
  const c = copy[language];
  if (Boolean(file) === Boolean(pasted.trim())) throw new Error(file ? c.chooseOne : c.required);
  if ((file?.size || 0) > 1048576 || new TextEncoder().encode(pasted).length > 1048576) throw new Error(c.tooLarge);
  const form = new FormData();
  if (file) form.set("file", file); else form.set("text", pasted);
  return form;
}

function ProfileRow({ profile, busy, act }: { profile: CookieProfile; busy: boolean; act: (operation: () => Promise<unknown>, success: string) => Promise<boolean> }) {
  const { language } = useLanguage();
  const c = copy[language];
  const [name, setName] = useState(profile.name);
  const [domains, setDomains] = useState(profile.domains.join(","));
  const [file, setFile] = useState<File | null>(null);
  const [pasted, setPasted] = useState("");
  const [localError, setLocalError] = useState("");
  const replace = async (event: FormEvent) => {
    event.preventDefault(); setLocalError("");
    try {
      const form = makeContent(file, pasted, language);
      if (await act(() => replaceCookieContent(profile.id, form), c.updated)) { setFile(null); setPasted(""); }
    } catch (error) { setLocalError((error as Error).message); }
  };
  return <article className="cookie-profile">
    <div className="cookie-profile-heading"><div><h2>{profile.name} {profile.is_default && <span className="task-status status-completed">{c.default}</span>}</h2><p>{profile.site_key} · {profile.domains.join(", ")}</p></div><span className="task-status">{profile.entry_count} {c.entries} · {c.version} {profile.content_version}</span></div>
    <div className="cookie-meta">{c.lastUsed}: {profile.last_used_at ? new Intl.DateTimeFormat(language, { dateStyle: "medium", timeStyle: "short" }).format(new Date(profile.last_used_at)) : c.noResult}{profile.last_result_code ? ` · ${profile.last_result_code}` : ""}</div>
    <div className="cookie-actions"><button className="button button-secondary" disabled={busy} onClick={() => void act(() => setCookieDefault(profile.site_key, profile.is_default ? null : profile.id), c.changed)}>{profile.is_default ? c.clearDefault : c.makeDefault}</button><button className="button button-secondary" disabled={busy} onClick={() => { if (window.confirm(c.deleteConfirm)) void act(() => deleteCookieProfile(profile.id), c.deleted); }}>{c.delete}</button></div>
    <details className="cookie-details"><summary>{c.edit}</summary><form className="cookie-inline-form" onSubmit={(event) => { event.preventDefault(); void act(() => updateCookieProfile(profile.id, { name: name.trim(), domains: domains.split(",").map((item) => item.trim()).filter(Boolean) }), c.updated); }}><label>{c.name}<input value={name} maxLength={255} onChange={(event) => setName(event.target.value)} required /></label><label>{c.domains}<input value={domains} onChange={(event) => setDomains(event.target.value)} required /></label><button className="button button-secondary" disabled={busy}>{c.save}</button></form></details>
    <details className="cookie-details"><summary>{c.replace}</summary><form className="cookie-inline-form" onSubmit={(event) => void replace(event)}><label>{c.file}<input type="file" accept=".txt,text/plain" onChange={(event) => setFile(event.target.files?.[0] || null)} /></label><label>{c.paste}<textarea value={pasted} onChange={(event) => setPasted(event.target.value)} /></label>{localError && <span className="inline-error" role="alert">{localError}</span>}<button className="button button-secondary" disabled={busy}>{c.replace}</button></form></details>
  </article>;
}

export default function Cookies() {
  const { t, language } = useLanguage();
  const c = copy[language];
  const [profiles, setProfiles] = useState<CookieProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [site, setSite] = useState("youtube");
  const [domains, setDomains] = useState("youtube.com");
  const [otherSite, setOtherSite] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [pasted, setPasted] = useState("");
  const load = useCallback(async () => { setLoading(true); try { setProfiles((await getCookieProfiles()).items); setLoadError(false); } catch { setLoadError(true); } finally { setLoading(false); } }, []);
  useEffect(() => { void load(); }, [load]);
  const act = async (operation: () => Promise<unknown>, success: string) => {
    setBusy(true); setError(""); setMessage("");
    try { await operation(); setProfiles((await getCookieProfiles()).items); setMessage(success); return true; }
    catch (cause) { setError((cause as { code?: string }).code === "COOKIE_IN_USE" ? c.inUse : c.failed); return false; }
    finally { setBusy(false); }
  };
  const create = async (event: FormEvent) => {
    event.preventDefault(); setError("");
    if (!name.trim() || !(site === "other" ? otherSite.trim() : site.trim()) || !domains.trim()) { setError(c.required); return; }
    let form: FormData;
    try { form = makeContent(file, pasted, language); } catch (cause) { setError((cause as Error).message); return; }
    form.set("name", name.trim()); form.set("site_key", site === "other" ? otherSite.trim() : site.trim()); form.set("domains", domains.trim());
    if (await act(() => createCookieProfile(form), c.created)) { setName(""); setFile(null); setPasted(""); setCreateOpen(false); }
  };
  return <>
    <PageHeader title={t("cookies")} description={t("cookiesDescription")} action={<Button onClick={() => { setError(""); setCreateOpen(true); }}>{c.add}</Button>} />
    {message && <div className="inline-success cookie-page-message" role="status">{message}</div>}
    {error && !createOpen && <div className="form-error cookie-page-message" role="alert">{error}</div>}
    <section className="content-panel" aria-label={t("cookies")}>
      <div className="panel-toolbar"><PanelHeading icon="cookie">{t("cookies")} · {profiles.length}</PanelHeading><button className="filter-button" onClick={() => void load()} disabled={loading}>{c.refresh}</button></div>
      {loading ? <LoadingState /> : loadError ? <ErrorState onRetry={() => void load()} /> : profiles.length ? <div className="cookie-list">{profiles.map((profile) => <ProfileRow key={profile.id} profile={profile} busy={busy} act={act} />)}</div> : <EmptyState icon="cookie" title={t("noCookies")} description={t("noCookiesDescription")} action={<Button variant="outline" onClick={() => setCreateOpen(true)}>{c.add}</Button>} />}
    </section>
    {createOpen && <CreateDialog title={c.add} description={c.limit} onClose={() => { setCreateOpen(false); setError(""); }}>
      <Card className="creation-card"><form className="creation-form" onSubmit={(event) => void create(event)}>
        <CardContent className="creation-fields">
          <Field><FieldLabel htmlFor="cookie-name">{c.name}</FieldLabel><Input id="cookie-name" value={name} maxLength={255} onChange={(event) => setName(event.target.value)} required autoFocus /></Field>
          <Field><FieldLabel htmlFor="cookie-site">{c.site}</FieldLabel><NativeSelect className="w-full" id="cookie-site" value={site} onChange={(event) => { const value = event.target.value; setSite(value); if (value === "youtube") setDomains("youtube.com"); if (value === "bilibili") setDomains("bilibili.com"); }}><NativeSelectOption value="youtube">YouTube</NativeSelectOption><NativeSelectOption value="bilibili">Bilibili</NativeSelectOption><NativeSelectOption value="other">{language === "zh-CN" ? "其他站点" : "Other site"}</NativeSelectOption></NativeSelect></Field>
          {site === "other" && <Field><FieldLabel htmlFor="cookie-other-site">{c.site}</FieldLabel><Input id="cookie-other-site" value={otherSite} onChange={(event) => setOtherSite(event.target.value)} placeholder="example_site" required /></Field>}
          <Field><FieldLabel htmlFor="cookie-domains">{c.domains}</FieldLabel><Input id="cookie-domains" value={domains} onChange={(event) => setDomains(event.target.value)} required /><FieldDescription>{c.domainsHint}</FieldDescription></Field>
          <Field><FieldLabel htmlFor="cookie-file">{c.file}</FieldLabel><Input id="cookie-file" type="file" accept=".txt,text/plain" onChange={(event) => setFile(event.target.files?.[0] || null)} /></Field>
          <Field><FieldLabel htmlFor="cookie-paste">{c.paste}</FieldLabel><Textarea id="cookie-paste" value={pasted} onChange={(event) => setPasted(event.target.value)} placeholder="# Netscape HTTP Cookie File" /></Field>
          <p className="creation-hint">{c.private}</p>
          {error && <div className="form-error" role="alert">{error}</div>}
        </CardContent>
        <CardFooter className="creation-actions"><Button variant="outline" type="button" onClick={() => { setCreateOpen(false); setError(""); }}>{language === "zh-CN" ? "取消" : "Cancel"}</Button><Button disabled={busy} type="submit">{busy ? t("loading") : c.save}</Button></CardFooter>
      </form></Card>
    </CreateDialog>}
  </>;
}
