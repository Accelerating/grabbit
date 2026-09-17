import { createContext, useContext, useEffect, useMemo, useState } from "react";

export type Language = "zh-CN" | "en";
export type TranslationKey = keyof typeof resources.en;

const resources = {
  en: {
    appName: "Grabbit", appTagline: "Personal download workspace", generalDownloads: "Downloads", videoDownloads: "Video downloads", history: "History", files: "Files", cookies: "Cookies", settings: "Settings", navigation: "Navigation", openNavigation: "Open navigation", closeNavigation: "Close navigation", language: "Language", theme: "Theme", light: "Light", dark: "Dark", administrator: "Administrator", signOut: "Sign out", signIn: "Sign in", username: "Username", password: "Password", signInTitle: "Sign in to Grabbit", signInDescription: "Manage downloads from one quiet workspace.", signInHint: "Your administrator account is created on the server.", invalidCredentials: "The username or password is incorrect.", unavailable: "The service is temporarily unavailable. Try again shortly.", signingIn: "Signing in…", overview: "Overview", activeTasks: "Active tasks", downloadSpeed: "Download speed", uploadSpeed: "Upload speed", diskAvailable: "Disk available", queue: "Global queue", queueDescription: "All download work shares one ordered queue.", newDownload: "New download", newVideoDownload: "New video download", search: "Search", filter: "Filter", all: "All", unfinished: "Unfinished", waiting: "Waiting", resolving: "Resolving", selecting: "Awaiting selection", downloading: "Downloading", paused: "Paused", failed: "Failed", completed: "Completed", warning: "Completed with warning", cancelled: "Cancelled", noDownloads: "No downloads yet", noDownloadsDescription: "Add a URL, magnet link, or torrent to begin.", noHistory: "No history yet", noHistoryDescription: "Finished, failed, and cancelled tasks will appear here.", noFiles: "No files in this folder", noFilesDescription: "Completed downloads and indexed files will appear here.", noCookies: "No cookie profiles", noCookiesDescription: "Add a cookies.txt profile when a site requires it.", noQueue: "The queue is clear", noQueueDescription: "Waiting downloads will be ordered here.", noTasksTitle: "Nothing to show", noTasksDescription: "The server returned no records for this view.", loading: "Loading…", refresh: "Refresh", retry: "Retry", error: "Something went wrong", connectionError: "Could not connect to the Grabbit service.", details: "Details", status: "Status", type: "Type", source: "Source", updated: "Updated", name: "Name", size: "Size", modified: "Modified", actions: "Actions", comingSoon: "This workspace is ready for the next backend phase.", comingSoonDescription: "The interface is connected to the API contract; records will appear once the service is running.", cookiesDescription: "Keep site credentials separate and choose them per video task.", filesDescription: "Browse the controlled download directory without exposing the server filesystem.", settingsDescription: "Control queue capacity, bandwidth, and account preferences.", historyDescription: "Review successful, warned, failed, and cancelled tasks.", videoDescription: "Resolve a source first, then choose the available format and subtitles.", downloadsDescription: "HTTP, HTTPS, magnet links, and torrent downloads share one queue.", menu: "Menu", close: "Close", apiContract: "API status", unauthenticated: "Not signed in", sessionExpired: "Your session has expired. Please sign in again.", breadcrumbHome: "Home", folder: "Folder", rootFolder: "Download root", copyPath: "Copy path", notAvailable: "Not available", preferences: "Preferences", appearance: "Appearance", defaultLight: "Light theme is the default. Your choice is saved on this device.", account: "Account", security: "Security", dependencyServices: "Dependency services", systemInformation: "System information", maxActiveTasks: "Maximum active tasks", bandwidth: "Bandwidth limits", notConnected: "Not connected", savedLocally: "Saved locally", unsupportedPage: "Page not found", unsupportedPageDescription: "This route does not exist in Grabbit.", backToDownloads: "Back to downloads", player: "Player", downloadFile: "Download file", mediaUnavailable: "This file cannot be played yet.", mediaUnavailableDescription: "Only complete files with supported media metadata can be played.", pause: "Pause", resume: "Resume", cancel: "Cancel", submitDownload: "Start download", sourceUrls: "URLs or magnet links", sourceUrlsHint: "One source per line. HTTP(S), magnet links, and torrent URLs are supported.", downloadSubdirectory: "Download subdirectory", downloadSubdirectoryHint: "A controlled relative folder under the download root.", allowDuplicates: "Allow duplicate sources", allowDuplicatesHint: "Create a task even when the source already exists.", creating: "Creating…", createdTasks: "download tasks created", invalidSources: "Enter at least one source.", taskTitle: "Download task", sourceUnknown: "Source unavailable", bytesDownloaded: "downloaded", eta: "ETA", queuePosition: "Queue", queueBlocked: "Queue is blocked", taskActionFailed: "The task action failed.", postprocessing: "Finalizing", retryWaiting: "Waiting to retry", stopped: "Stopped", refreshed: "Updated just now",
  },
  "zh-CN": {
    appName: "Grabbit", appTagline: "个人下载工作台", generalDownloads: "普通下载", videoDownloads: "视频下载", history: "下载历史", files: "文件管理", cookies: "Cookie 管理", settings: "设置", navigation: "导航", openNavigation: "打开导航", closeNavigation: "关闭导航", language: "语言", theme: "主题", light: "浅色", dark: "深色", administrator: "管理员", signOut: "退出登录", signIn: "登录", username: "用户名", password: "密码", signInTitle: "登录 Grabbit", signInDescription: "在一个安静的工作台管理下载任务。", signInHint: "管理员账号由服务器命令创建。", invalidCredentials: "用户名或密码不正确。", unavailable: "服务暂时不可用，请稍后重试。", signingIn: "登录中…", overview: "概览", activeTasks: "运行中任务", downloadSpeed: "下载速度", uploadSpeed: "上传速度", diskAvailable: "可用磁盘", queue: "全局队列", queueDescription: "所有下载工作共享一个有序队列。", newDownload: "新建下载", newVideoDownload: "新建视频下载", search: "搜索", filter: "筛选", all: "全部", unfinished: "未完成", waiting: "等待中", resolving: "解析中", selecting: "待选择", downloading: "下载中", paused: "已暂停", failed: "失败", completed: "已完成", warning: "完成但有警告", cancelled: "已取消", noDownloads: "还没有下载任务", noDownloadsDescription: "添加 URL、磁力链接或种子文件开始下载。", noHistory: "还没有历史记录", noHistoryDescription: "已完成、有警告、失败和取消的任务会显示在这里。", noFiles: "此文件夹为空", noFilesDescription: "已完成下载和索引文件会显示在这里。", noCookies: "还没有 Cookie 配置", noCookiesDescription: "网站需要登录时，可以添加 cookies.txt 配置。", noQueue: "队列为空", noQueueDescription: "等待中的下载会按顺序显示在这里。", noTasksTitle: "暂无内容", noTasksDescription: "服务端没有返回此视图对应的记录。", loading: "加载中…", refresh: "刷新", retry: "重试", error: "出了点问题", connectionError: "无法连接到 Grabbit 服务。", details: "详情", status: "状态", type: "类型", source: "来源", updated: "更新时间", name: "名称", size: "大小", modified: "修改时间", actions: "操作", comingSoon: "工作台已准备好接入下一阶段后端能力。", comingSoonDescription: "界面遵循 API 契约；服务启动后，真实记录会显示在这里。", cookiesDescription: "分开保存站点凭据，并在创建视频任务时单独选择。", filesDescription: "浏览受控下载目录，不暴露服务器文件系统。", settingsDescription: "管理队列容量、带宽限制和账号偏好。", historyDescription: "查看成功、有警告、失败和取消的任务。", videoDescription: "先解析来源，再选择实际可用格式和字幕。", downloadsDescription: "HTTP、HTTPS、磁力链接和种子下载共享一个队列。", menu: "菜单", close: "关闭", apiContract: "API 状态", unauthenticated: "未登录", sessionExpired: "会话已过期，请重新登录。", breadcrumbHome: "首页", folder: "文件夹", rootFolder: "下载根目录", copyPath: "复制路径", notAvailable: "暂无", preferences: "偏好设置", appearance: "外观", defaultLight: "默认使用浅色主题，你的选择会保存在此设备。", account: "账号", security: "安全", dependencyServices: "依赖服务", systemInformation: "系统信息", maxActiveTasks: "最大并发任务数", bandwidth: "带宽限制", notConnected: "未连接", savedLocally: "已保存在本地", unsupportedPage: "页面不存在", unsupportedPageDescription: "Grabbit 中没有这个路由。", backToDownloads: "返回下载", player: "播放器", downloadFile: "下载文件", mediaUnavailable: "此文件暂时无法播放。", mediaUnavailableDescription: "只有完整且媒体信息受支持的文件才能播放。", pause: "暂停", resume: "继续", cancel: "取消", submitDownload: "开始下载", sourceUrls: "URL 或磁力链接", sourceUrlsHint: "每行一个来源，支持 HTTP(S)、磁力链接和种子 URL。", downloadSubdirectory: "下载子目录", downloadSubdirectoryHint: "下载根目录下受控的相对路径。", allowDuplicates: "允许重复来源", allowDuplicatesHint: "即使来源已经存在，也创建新的任务。", creating: "创建中…", createdTasks: "个下载任务已创建", invalidSources: "请至少输入一个来源。", taskTitle: "下载任务", sourceUnknown: "来源不可用", bytesDownloaded: "已下载", eta: "预计剩余", queuePosition: "队列", queueBlocked: "队列已阻断", taskActionFailed: "任务操作失败。", postprocessing: "整理中", retryWaiting: "等待重试", stopped: "已停止", refreshed: "刚刚更新",
  },
} as const;

type Translator = (key: TranslationKey) => (typeof resources.en)[TranslationKey];
const phaseOneDownloadCopy: Record<Language, Partial<Record<TranslationKey, string>>> = {
  en: {
    downloadsDescription: "HTTP(S), magnet, and torrent downloads share one queue.",
    noDownloadsDescription: "Add a supported source to begin.",
    sourceUrls: "HTTP(S) or magnet links",
    sourceUrlsHint: "One HTTP(S) or magnet link per line. Upload a .torrent file below.",
  },
  "zh-CN": {
    downloadsDescription: "HTTP(S)、磁力链接与种子下载共享一个队列。",
    noDownloadsDescription: "添加受支持的来源开始下载。",
    sourceUrls: "HTTP(S) 或磁力链接",
    sourceUrlsHint: "每行一个 HTTP(S) 或磁力链接。种子文件请在下方上传。",
  },
};
const LanguageContext = createContext<{ language: Language; setLanguage: (language: Language) => void; t: Translator } | null>(null);

function initialLanguage(): Language {
  if (typeof window === "undefined") return "zh-CN";
  const saved = window.localStorage.getItem("grabbit.language");
  if (saved === "en" || saved === "zh-CN") return saved;
  return window.navigator.language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [language, setLanguageState] = useState<Language>(initialLanguage);
  const setLanguage = (next: Language) => {
    setLanguageState(next);
    if (typeof window !== "undefined") {
      window.localStorage.setItem("grabbit.language", next);
      document.documentElement.lang = next;
    }
  };
  useEffect(() => setLanguage(language), []);
  const value = useMemo(() => ({ language, setLanguage, t: ((key: TranslationKey) => phaseOneDownloadCopy[language][key] || resources[language][key]) as Translator }), [language]);
  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>;
}

export function useLanguage() {
  const value = useContext(LanguageContext);
  if (!value) throw new Error("useLanguage must be used within LanguageProvider");
  return value;
}
