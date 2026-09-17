import type { Config } from "@react-router/dev/config";

export default {
  // Grabbit is served as static client assets by FastAPI.
  ssr: false,
} satisfies Config;
