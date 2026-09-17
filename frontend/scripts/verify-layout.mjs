import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { chromium } from "playwright";

const origin = "http://127.0.0.1:5178";
const chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const server = spawn("pnpm", ["dev", "--host", "127.0.0.1", "--port", "5178"], { cwd: process.cwd(), stdio: "ignore" });
let browser;

async function ready() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try { const response = await fetch(origin); if (response.ok) return; } catch { /* starting */ }
    await delay(300);
  }
  throw new Error("Frontend development server did not start");
}

async function mockApi(page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path === "/api/v1/auth/session") body = { user: { id: "layout-test", username: "admin" }, csrf_token: "layout-test" };
    else if (path === "/api/v1/system/summary") body = { download_speed_bps: 0, upload_speed_bps: 0, disk_free_bytes: 1000000000, running_tasks: 0 };
    else if (path === "/api/v1/queue") body = { items: [], revision: 1 };
    else if (path === "/api/v1/files") body = { items: [{ type: "directory", name: "videos", relative_path: "videos", mtime_ns: 1789638000000000000 }], next_cursor: null };
    else if (path === "/api/v1/cookies") body = { items: [] };
    else if (path === "/api/v1/tasks") body = { items: [] };
    else if (path === "/api/v1/settings") body = { editable: { max_active_tasks: 1, download_limit_bps: 0, upload_limit_bps: 0, disk_min_free_bytes: 1073741824, default_video_height: 1080, default_subtitle_languages: ["zh", "en"], allow_auto_subtitles: true }, read_only: { download_root: "/srv/grabbit/downloads" }, revision: 1 };
    else if (path === "/api/v1/dependencies") body = { items: { aria2: { status: "missing", version: null }, ffmpeg: { status: "missing", version: null } }, install_supported: true };
    else if (path === "/api/v1/dependencies/install-jobs") body = { items: [{ id: "mock", action: "aria2", status: "failed", log: "The package manager was unavailable. ".repeat(30), error_code: "INSTALL_FAILED" }] };
    else if (path === "/api/v1/events") return route.fulfill({ status: 200, contentType: "text/event-stream", body: "event: snapshot\ndata: {}\n\n" });
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
}

async function assertNoOverflow(page, name) {
  const sizes = await page.evaluate(() => ({ viewport: innerWidth, content: document.documentElement.scrollWidth }));
  assert.ok(sizes.content <= sizes.viewport + 1, `${name}: horizontal overflow ${JSON.stringify(sizes)}`);
}

try {
  await ready();
  browser = await chromium.launch({ executablePath: chrome, headless: true });
  const screenshots = await mkdtemp(join(tmpdir(), "grabbit-layout-"));
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    const page = await browser.newPage({ viewport });
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));
    await mockApi(page);
    await page.goto(`${origin}/files`);
    await page.locator(".panel-heading").waitFor();
    const alignment = await page.locator(".panel-heading").evaluate((heading) => {
      const icon = heading.querySelector("svg").getBoundingClientRect();
      const label = heading.querySelector("span").getBoundingClientRect();
      return Math.abs((icon.top + icon.bottom) / 2 - (label.top + label.bottom) / 2);
    });
    assert.ok(alignment <= 3, `files heading alignment: ${alignment}px`);
    await assertNoOverflow(page, "files");
    await page.screenshot({ path: join(screenshots, `files-${viewport.width}.png`) });

    for (const path of ["/downloads", "/video", "/history"]) {
      await page.goto(`${origin}${path}`);
      await page.locator(".panel-heading").waitFor();
      const fontSize = await page.locator(".panel-heading").evaluate((heading) => Number.parseFloat(getComputedStyle(heading).fontSize));
      assert.equal(fontSize, 14, `${path}: inconsistent panel heading size`);
      await assertNoOverflow(page, path);
    }

    await page.goto(`${origin}/settings`);
    await page.getByRole("button", { name: /安装\/更新 aria2|Install\/update aria2/ }).waitFor();
    await assertNoOverflow(page, "settings installation");
    await page.screenshot({ path: join(screenshots, `settings-${viewport.width}.png`) });

    await page.goto(`${origin}/cookies`);
    await page.getByRole("button", { name: /添加 Cookie 配置|Add Cookie profile/ }).first().click();
    await page.locator("[data-slot=dialog-content]").waitFor();
    await page.waitForTimeout(250);
    await assertNoOverflow(page, "cookie dialog");
    await page.screenshot({ path: join(screenshots, `cookies-${viewport.width}.png`) });

    await page.goto(`${origin}/downloads?new=1`);
    await page.locator("[data-slot=dialog-content]").waitFor();
    await page.waitForTimeout(250);
    const checkbox = await page.locator("[data-slot=checkbox]").first().boundingBox();
    assert.ok(checkbox && checkbox.width <= 24 && checkbox.height <= 24, `download checkbox size: ${JSON.stringify(checkbox)}`);
    await assertNoOverflow(page, "download dialog");
    await page.screenshot({ path: join(screenshots, `downloads-${viewport.width}.png`) });
    assert.deepEqual(pageErrors, []);
    await page.close();
  }
  process.stdout.write(`Layout checks passed; screenshots: ${screenshots}\n`);
} finally {
  await browser?.close();
  server.kill("SIGTERM");
}
