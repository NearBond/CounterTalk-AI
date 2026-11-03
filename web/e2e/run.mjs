// The browser checks (BUILD_PLAN batch 7): `npm run e2e`, locally (not in CI yet).
//
// Starts the API, the worker and Vite on a throwaway database (countertalk_e2e) and data folder,
// with MODEL_API_KEY empty so nothing can call Meta. Seeds them (scripts/e2e_setup.py makes a
// transcribed visit without Meta), runs the checks in Chrome or Edge with a fake microphone, and
// stops everything on every path: a failed check, an error, or Ctrl+C.
import { spawn, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { createWriteStream, existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { chromium } from "playwright-core";
import { CHECKS } from "./checks.mjs";

const WEB_DIR = resolve(import.meta.dirname, "..");
const BACKEND_DIR = resolve(WEB_DIR, "..", "backend");
const API_PORT = 8011;
const WEB_PORT = 5183;
const API = `http://127.0.0.1:${API_PORT}`;
const WEB = `http://127.0.0.1:${WEB_PORT}`;
// The compose database's server, as the backend tests use it; E2E_DATABASE_SERVER overrides it.
const SERVER = process.env.E2E_DATABASE_SERVER ?? "postgresql+psycopg://countertalk:countertalk@127.0.0.1:5432";
const BROWSERS = [
  process.env.E2E_BROWSER,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/google-chrome",
  "/usr/bin/microsoft-edge",
].filter(Boolean);
// E2E_ONLY=<text> runs only the checks whose name contains it.
const ONLY = process.env.E2E_ONLY;
const READY_MS = 90_000;
const CHECK_MS = 120_000;

const print = (line) => process.stdout.write(`${line}\n`);
const dataDir = mkdtempSync(join(tmpdir(), "countertalk-e2e-"));
// The checks' users' password (batch 16): new each run, written nowhere.
const PASSWORD = randomBytes(18).toString("base64url");
const env = {
  ...process.env,
  DATABASE_URL: `${SERVER}/countertalk_e2e`,
  DATA_DIR: dataDir,
  MODEL_API_KEY: "",
  DEBUG: "0",
  SECRET_KEY: randomBytes(32).toString("base64url"),
  API_PORT: String(API_PORT),
};
const children = [];
let browser;
let stopping = false;
/** Set when a server or the worker exits by itself: the run stops instead of testing a ghost. */
let died = null;

function start(name, command, args, cwd, more = {}) {
  const log = createWriteStream(join(dataDir, `${name}.log`));
  const child = spawn(command, args, {
    cwd,
    env: { ...env, ...more },
    stdio: ["ignore", "pipe", "pipe"],
    detached: process.platform !== "win32",
  });
  child.stdout.pipe(log);
  child.stderr.pipe(log);
  const closed = new Promise((done) => child.on("close", () => log.end(done)));
  child.on("exit", (code) => {
    if (!stopping) died ??= `${name} exited by itself (code ${code})`;
  });
  children.push({ child, closed });
}

async function stopAll() {
  stopping = true;
  for (const { child } of children) {
    if (child.exitCode !== null || child.pid === undefined) continue;
    // The whole tree: uv starts Python, which can start more.
    if (process.platform === "win32") spawnSync("taskkill", ["/T", "/F", "/PID", String(child.pid)]);
    else {
      try {
        process.kill(-child.pid, "SIGTERM");
      } catch {
        // already gone
      }
    }
  }
  // The logs are closed before their folder is removed (Windows refuses open files).
  await Promise.all(children.splice(0).map(({ closed }) => Promise.race([closed, new Promise((done) => setTimeout(done, 5_000))])));
}

/** Never throws: a cleanup that fails is reported, and the run's own result stands (R2). */
async function cleanUp(keepLogs) {
  await browser?.close().catch(() => {});
  await stopAll();
  if (keepLogs) {
    print(`logs kept in ${dataDir}`);
    return;
  }
  try {
    rmSync(dataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 500 });
  } catch (error) {
    print(`couldn't remove ${dataDir}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

async function waitFor(url, ok) {
  const until = Date.now() + READY_MS;
  for (;;) {
    if (died !== null) throw new Error(died);
    try {
      const response = await fetch(url);
      if (await ok(response)) return;
    } catch {
      // not up yet
    }
    if (Date.now() > until) throw new Error(`${url} wasn't ready in ${READY_MS / 1000} s (logs in ${dataDir})`);
    await new Promise((done) => setTimeout(done, 500));
  }
}

function withTimeout(promise, ms, name) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${name} took over ${ms / 1000} s`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** The context signed in as that user, through the app's own address (Vite passes /api on), so
 * its pages and its requests carry the session cookie (batch 16). */
async function signIn(context, email) {
  const response = await context.request.post(`${WEB}/api/auth/signin`, { data: { email, password: PASSWORD } });
  if (!response.ok()) throw new Error(`signing in as ${email} answered ${response.status()}`);
}

async function portFree(url) {
  try {
    await fetch(url);
    return false; // something already answers there: an earlier run left behind, say
  } catch {
    return true;
  }
}

async function main() {
  for (const url of [API, WEB]) {
    if (!(await portFree(url))) throw new Error(`${url} is already taken: stop what's running there`);
  }
  const setup = spawnSync("uv", ["run", "python", "scripts/e2e_setup.py"], {
    cwd: BACKEND_DIR,
    env: { ...env, E2E_PASSWORD: PASSWORD },
    encoding: "utf8",
  });
  if (setup.status !== 0) throw new Error(`e2e_setup.py failed:\n${setup.stderr}`);
  const seeded = JSON.parse(setup.stdout.trim().split("\n").at(-1));

  start("api", "uv", ["run", "uvicorn", "countertalk.main:app", "--port", String(API_PORT)], BACKEND_DIR);
  start("worker", "uv", ["run", "python", "-m", "countertalk.worker"], BACKEND_DIR);
  start("vite", process.execPath, [join(WEB_DIR, "node_modules", "vite", "bin", "vite.js"), "--port", String(WEB_PORT), "--strictPort"], WEB_DIR);
  await waitFor(`${API}/api/health`, async (response) => response.ok && (await response.json()).database === "ok");
  await waitFor(`${WEB}/`, async (response) => response.ok);

  // By its file: on Windows, `chrome.exe --version` would open the browser instead.
  const executablePath = BROWSERS.find((path) => existsSync(path));
  if (executablePath === undefined) throw new Error("Neither Chrome nor Edge was found; set E2E_BROWSER");
  browser = await chromium.launch({
    executablePath,
    headless: true,
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
  });

  const checks = ONLY === undefined ? CHECKS : CHECKS.filter((check) => check.name.includes(ONLY));
  let failed = 0;
  for (const check of checks) {
    const context = await browser.newContext({ permissions: ["microphone"] });
    const started = Date.now();
    try {
      if (died !== null) throw new Error(died);
      await signIn(context, check.as === "k6" ? seeded.k6.email : seeded.rep);
      await withTimeout(check.run({ browser, context, page: await context.newPage(), web: WEB, seeded }), CHECK_MS, check.name);
      print(`ok    ${check.name} (${((Date.now() - started) / 1000).toFixed(1)} s)`);
    } catch (error) {
      failed += 1;
      print(`FAIL  ${check.name}: ${error instanceof Error ? error.message : String(error)}`);
    } finally {
      await context.close().catch(() => {});
    }
  }
  print(`${checks.length - failed} of ${checks.length} checks passed`);
  return failed === 0 ? 0 : 1;
}

process.on("SIGINT", () => {
  void cleanUp(false).finally(() => process.exit(130));
});

let code = 1;
try {
  code = await main();
} catch (error) {
  print(`e2e stopped: ${error instanceof Error ? error.message : String(error)}`);
} finally {
  await cleanUp(code !== 0);
}
process.exit(code);
