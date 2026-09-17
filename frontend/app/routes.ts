import { type RouteConfig, index, layout, route } from "@react-router/dev/routes";

export default [
  route("login", "routes/login.tsx"),
  route("player/:fileId", "routes/player.tsx"),
  layout("routes/app-layout.tsx", [
    index("routes/home.tsx"),
    route("downloads", "routes/downloads.tsx"),
    route("downloads/new", "routes/new-download-redirect.tsx"),
    route("video", "routes/video.tsx"),
    route("video/new", "routes/new-video-redirect.tsx"),
    route("history", "routes/history.tsx"),
    route("files", "routes/files.tsx"),
    route("cookies", "routes/cookies.tsx"),
    route("settings", "routes/settings.tsx"),
  ]),
] satisfies RouteConfig;
