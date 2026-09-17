export type ApiError = Error & { code?: string; status?: number };
let csrfToken: string | null = null;

async function readError(response: Response): Promise<ApiError> {
  let message = response.statusText || "Request failed";
  let code: string | undefined;
  try { const body = await response.json() as { error?: { message?: string; code?: string } }; message = body.error?.message || message; code = body.error?.code; } catch { /* non-JSON response */ }
  const error = new Error(message) as ApiError;
  error.code = code; error.status = response.status;
  return error;
}

export async function apiFetch<T>(path: string, options: RequestInit = {}, csrfRetried = false): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (method !== "GET" && method !== "HEAD") {
    if (!csrfToken) {
      try { const session = await fetch("/api/v1/auth/session", { credentials: "same-origin", headers: { Accept: "application/json" } }); if (session.ok) csrfToken = (await session.json() as { csrf_token?: string }).csrf_token || null; } catch { /* request below reports connection errors */ }
    }
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    const error = await readError(response);
    if (!csrfRetried && method !== "GET" && method !== "HEAD" && error.code === "CSRF_FAILED") {
      csrfToken = null;
      try { await getSession(); } catch { throw error; }
      return apiFetch<T>(path, options, true);
    }
    throw error;
  }
  const token = response.headers.get("X-CSRF-Token");
  if (token) csrfToken = token;
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export type User = { id: string; username: string };
export type Session = { user: User; csrf_token: string; expires_at?: string };
export async function getSession() { const session = await apiFetch<Session>("/auth/session"); csrfToken = session.csrf_token; return session; }
export async function login(username: string, password: string) { const session = await apiFetch<Session>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }); csrfToken = session.csrf_token; return session; }
export async function logout() { await apiFetch<void>("/auth/logout", { method: "POST" }); csrfToken = null; }

export type TaskProgress = {
  scope?: string | null;
  percent: number | null;
  downloaded_bytes?: number | null;
  total_bytes?: number | null;
  speed_bps?: number | null;
  eta_seconds?: number | null;
  total_is_estimate?: boolean;
};

export type Task = {
  id: string;
  kind: string;
  title?: string | null;
  source_url?: string | null;
  status: string;
  phase?: string | null;
  progress?: TaskProgress | null;
  allowed_actions?: string[];
  blocked_reason?: string | null;
  error_summary?: string | null;
  error_code?: string | null;
  warnings?: string[];
  cookie_profile_id?: string | null;
  cookie_name_snapshot?: string | null;
  source_site?: string | null;
  created_at?: string | null;
  revision?: number;
  updated_at?: string | null;
};

export type TaskListResponse = { items: Task[]; next_cursor?: string | null };
export type HistoryResponse = { items: Task[]; next_cursor?: string | null };
export type QueueItem = Task & { position?: number; resource_id?: string };
export type QueueResponse = { items: QueueItem[]; revision?: number; blocked_reason?: string | null };
export type SystemSummary = {
  download_speed_bps?: number | null;
  upload_speed_bps?: number | null;
  disk_free_bytes?: number | null;
  memory_available_bytes?: number | null;
  running_tasks?: number;
  queued_items?: number;
  blocked_reason?: string | null;
  server_time?: string;
};

function normalizeItems<T>(value: T[] | { items?: T[] }): T[] {
  return Array.isArray(value) ? value : value.items || [];
}

export async function getTasks(kind = "general") {
  const response = await apiFetch<TaskListResponse | Task[]>(`/tasks?kind=${encodeURIComponent(kind)}`);
  return { items: normalizeItems(response), next_cursor: Array.isArray(response) ? null : response.next_cursor };
}

export async function getHistory(options: { kind?: string; cursor?: string | null; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (options.kind) params.set("kind", options.kind);
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.limit) params.set("limit", String(options.limit));
  const suffix = params.toString();
  const response = await apiFetch<HistoryResponse | Task[]>(`/history${suffix ? `?${suffix}` : ""}`);
  return { items: normalizeItems(response), next_cursor: Array.isArray(response) ? null : response.next_cursor };
}

export type FileRecord = {
  type?: "file";
  id: string;
  name?: string | null;
  display_name?: string | null;
  relative_path?: string | null;
  size_bytes?: number | null;
  mtime_ns?: number | null;
  kind?: string | null;
  mime_type?: string | null;
  availability?: string | null;
  is_complete?: boolean;
  created_at?: string | null;
  updated_at?: string | null;
  probe_status?: "unprobed" | "queued" | "running" | "completed" | "failed";
  media?: { duration_seconds: number | null; format_name: string; streams: { codec_type: string; codec_name?: string; width?: number; height?: number; channels?: number; sample_rate?: string }[] } | null;
};
export type DirectoryEntry = { type: "directory"; name: string; relative_path: string; mtime_ns?: number | null };
export type FileEntry = FileRecord | DirectoryEntry;
export type FileListResponse = { items: FileEntry[]; next_cursor?: string | null; directory?: string };

export async function getFiles(options: { directory?: string; cursor?: string | null; query?: string; limit?: number } = {}) {
  const params = new URLSearchParams();
  if (options.directory) params.set("directory", options.directory);
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.query) params.set("query", options.query);
  if (options.limit) params.set("limit", String(options.limit));
  const suffix = params.toString();
  const response = await apiFetch<FileListResponse | FileRecord[]>(`/files${suffix ? `?${suffix}` : ""}`);
  return { items: normalizeItems(response), next_cursor: Array.isArray(response) ? null : response.next_cursor };
}

/** A same-origin URL; the browser sends the authenticated session cookie on navigation. */
export function fileContentUrl(fileId: string) {
  return `/api/v1/files/${encodeURIComponent(fileId)}/content`;
}
export function fileInlineUrl(fileId: string) { return `${fileContentUrl(fileId)}?disposition=inline`; }
export const getFileDetail = (fileId: string) => apiFetch<FileRecord>(`/files/${encodeURIComponent(fileId)}`);
export const queueFileProbe = (fileId: string) => apiFetch<FileRecord>(`/files/${encodeURIComponent(fileId)}/probe`, { method: "POST" });
export type SubtitleTrack = { id: string; label: string; language: string };
export const getSubtitleTracks = (fileId: string) => apiFetch<{ items: SubtitleTrack[] }>(`/files/${encodeURIComponent(fileId)}/subtitles`);
export const subtitleTrackUrl = (fileId: string, trackId: string) => `/api/v1/files/${encodeURIComponent(fileId)}/subtitles/${encodeURIComponent(trackId)}`;
export type FileDeletionPreview = { preview_id: string; files: { id: string; name: string; size_bytes: number }[]; total_bytes: number; expires_at: number };
export type FileOperation = { id: string; kind: string; status: "queued" | "running" | "completed" | "partial"; total: number; items: { id: string; status: string }[]; created_at: string; finished_at: string | null };
export const previewFileDeletion = (fileIds: string[]) => apiFetch<FileDeletionPreview>("/files/deletion-preview", { method: "POST", body: JSON.stringify({ file_ids: fileIds }) });
export type DirectoryDeletionPreview = { preview_id: string; directory: string; file_count: number; directory_count: number; total_bytes: number; expires_at: number };
export const previewDirectoryDeletion = (directory: string) => apiFetch<DirectoryDeletionPreview>("/directories/deletion-preview", { method: "POST", body: JSON.stringify({ directory }) });
export const confirmFileDeletion = (previewId: string) => apiFetch<FileOperation>("/files/delete", { method: "POST", body: JSON.stringify({ preview_id: previewId, confirmed: true }) });
export const getFileOperation = (operationId: string) => apiFetch<FileOperation>(`/files/operations/${encodeURIComponent(operationId)}`);
export const createDirectory = (parent: string, name: string) => apiFetch<{ relative_path: string; name: string }>("/directories", { method: "POST", body: JSON.stringify({ parent, name }) });

export async function createGeneralTasks(body: { sources: string[]; download_subdir: string; allow_duplicates: boolean }) {
  const response = await apiFetch<TaskListResponse | Task[]>("/tasks/general", { method: "POST", body: JSON.stringify(body) });
  return { items: normalizeItems(response) };
}

export type TorrentCandidate = { index: number; path: string; size_bytes: number };
export type TorrentUpload = { torrent_id: string; summary: { name: string; file_count: number; total_size_bytes: number }; files: { items: TorrentCandidate[]; next_cursor: string | null; total: number } };
export async function uploadTorrent(file: File) {
  const form = new FormData(); form.set("file", file);
  return apiFetch<TorrentUpload>("/torrents", { method: "POST", body: form });
}
export async function getTorrentFiles(id: string, cursor?: string | null) {
  return apiFetch<{ items: TorrentCandidate[]; next_cursor: string | null; total: number }>(`/torrents/${encodeURIComponent(id)}/files?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}
export async function createTorrentTask(body: { torrent_id: string; selected_indices: number[]; download_subdir: string; allow_duplicates: boolean }) {
  return apiFetch<Task>("/tasks/torrent", { method: "POST", body: JSON.stringify(body) });
}

export async function getTaskCandidates(taskId: string, cursor?: string | null) {
  return apiFetch<{ items: TorrentCandidate[]; next_cursor: string | null; metadata_revision: number; total: number }>(`/tasks/${encodeURIComponent(taskId)}/files?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
}
export async function selectMagnetFiles(taskId: string, selectedIndices: number[], metadataRevision: number) {
  return apiFetch<Task>(`/tasks/${encodeURIComponent(taskId)}/selection`, { method: "PUT", body: JSON.stringify({ selected_indices: selectedIndices, metadata_revision: metadataRevision }) });
}

export async function getQueue() {
  return apiFetch<QueueResponse>("/queue");
}

export async function getSystemSummary() {
  return apiFetch<SystemSummary>("/system/summary");
}

export type TaskAction = "pause" | "resume" | "cancel" | "retry" | "stop";
export async function taskAction(taskId: string, action: TaskAction) {
  return apiFetch<Task>(`/tasks/${encodeURIComponent(taskId)}/${action}`, { method: "POST", body: JSON.stringify({}) });
}

export type CookieProfile = { id: string; name: string; site_key: string; domains: string[]; content_version: number; entry_count: number; last_result_code: string | null; last_used_at: string | null; updated_at: string; is_default: boolean };
export const getCookieProfiles = () => apiFetch<{ items: CookieProfile[] }>("/cookies");
export const createCookieProfile = (form: FormData) => apiFetch<CookieProfile>("/cookies", { method: "POST", body: form });
export const replaceCookieContent = (id: string, form: FormData) => apiFetch<CookieProfile>(`/cookies/${encodeURIComponent(id)}/content`, { method: "PUT", body: form });
export const updateCookieProfile = (id: string, body: { name: string; domains: string[] }) => apiFetch<CookieProfile>(`/cookies/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) });
export const setCookieDefault = (site: string, profileId: string | null) => apiFetch<void>(`/cookies/defaults/${encodeURIComponent(site)}`, { method: "PUT", body: JSON.stringify({ cookie_profile_id: profileId }) });
export const deleteCookieProfile = (id: string) => apiFetch<void>(`/cookies/${encodeURIComponent(id)}`, { method: "DELETE" });

export type VideoFormat = { id: string; ext: string; height: number | null; vcodec: string; acodec: string; filesize: number | null };
export type VideoAnalysis = { site: string; title: string; collection?: false; duration: number | null; thumbnail: string | null; extractor: string; formats: VideoFormat[]; subtitle_languages: string[] } | { site: string; title: string; collection: true; next_index: number | null };
export const resolveVideo = (url: string, cookieProfileId: string | null) => apiFetch<VideoAnalysis>("/video/resolve", { method: "POST", body: JSON.stringify({ url, cookie_profile_id: cookieProfileId }) });
export type VideoAnalysisJob = { id: string; url: string; cookie_profile_id: string | null; status: "queued" | "running" | "completed" | "failed" | "cancelled" | "expired"; result: VideoAnalysis | null; error_code: string | null };
export const createVideoAnalysis = (url: string, cookieProfileId: string | null) => apiFetch<VideoAnalysisJob>("/video/analyses", { method: "POST", body: JSON.stringify({ url, cookie_profile_id: cookieProfileId }) });
export const getVideoAnalysis = (id: string) => apiFetch<VideoAnalysisJob>(`/video/analyses/${encodeURIComponent(id)}`);
export const cancelVideoAnalysis = (id: string) => apiFetch<VideoAnalysisJob>(`/video/analyses/${encodeURIComponent(id)}/cancel`, { method: "POST" });
export type VideoAnalysisItem = { id: string; ordinal: number; title: string; duration_seconds: number | null; available: boolean };
export const getVideoAnalysisItems = (id: string, cursor?: number | null) => apiFetch<{ items: VideoAnalysisItem[]; next_cursor: number | null; next_source_index: number | null }>(`/video/analyses/${encodeURIComponent(id)}/items?limit=100${cursor ? `&cursor=${cursor}` : ""}`);
export const loadVideoAnalysisPage = (id: string) => apiFetch<VideoAnalysisJob>(`/video/analyses/${encodeURIComponent(id)}/next-page`, { method: "POST" });
export const createCollectionVideoTasks = (id: string, body: { item_ids: string[]; cookie_profile_id: string | null; mode: "video" | "audio"; max_height: number; subtitle_languages: string[]; download_subdir: string; allow_duplicates: boolean }) => apiFetch<{ group_id: string; items: Task[] }>(`/video/analyses/${encodeURIComponent(id)}/tasks`, { method: "POST", body: JSON.stringify(body) });
export const retryTaskSubtitles = (id: string) => apiFetch<{ id: string; task_id: string; status: "queued" | "running" }>(`/tasks/${encodeURIComponent(id)}/retry-subtitles`, { method: "POST" });
export const createVideoTask = (body: { url: string; title: string; cookie_profile_id: string | null; mode: "video" | "audio"; max_height: number; download_subdir: string; allow_duplicates: boolean; subtitle_languages: string[] }) => apiFetch<Task>("/video/tasks", { method: "POST", body: JSON.stringify(body) });
export const changeTaskCookie = (id: string, revision: number, cookieProfileId: string | null) => apiFetch<Task>(`/tasks/${encodeURIComponent(id)}/cookie`, { method: "PATCH", body: JSON.stringify({ revision, cookie_profile_id: cookieProfileId }) });

export type DeletionPreview = { task_id: string; task_revision: number; files: { id: string; name: string; size_bytes: number | null }[]; total_bytes: number; delete_files_supported: boolean };
export const previewTaskDeletion = (id: string) => apiFetch<DeletionPreview>(`/tasks/${encodeURIComponent(id)}/deletion-preview`, { method: "POST" });
export const deleteTaskRecord = (id: string, revision: number) => apiFetch<void>(`/tasks/${encodeURIComponent(id)}?revision=${revision}`, { method: "DELETE" });
