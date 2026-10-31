// @vitest-environment node
/**
 * The lint rules the guidelines rely on (§4.1, §4.3, §4.9, §4.10, §9.11) really fire.
 * Each rule in .oxlintrc.json runs on a small bad example, so a rule that's misnamed or stops
 * working fails here instead of passing silently.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const WEB_DIR = fileURLToPath(new URL("..", import.meta.url));
const CONFIG = join(WEB_DIR, ".oxlintrc.json");
const OXLINT = join(WEB_DIR, "node_modules", "oxlint", "bin", "oxlint");

const BAD_EXAMPLES: Record<string, [code: string, source: string]> = {
  "fetch.ts": ["eslint(no-restricted-globals)", 'export const load = () => fetch("/api/health");\n'],
  "window-fetch.ts": [
    "eslint(no-restricted-properties)",
    'export const load = () => window.fetch("/api/health");\n',
  ],
  "global-fetch.ts": [
    "eslint(no-restricted-properties)",
    'export const load = () => globalThis.fetch("/api/health");\n',
  ],
  "self-fetch.ts": [
    "eslint(no-restricted-properties)",
    'export const load = () => self.fetch("/api/health");\n',
  ],
  "xhr.ts": ["eslint(no-restricted-globals)", "export const request = new XMLHttpRequest();\n"],
  "console-log.ts": ["eslint(no-console)", 'console.log("hello");\n'],
  "any.ts": ["typescript(no-explicit-any)", "export const value: any = 1;\n"],
  "ts-ignore.ts": [
    "typescript(ban-ts-comment)",
    '// @ts-ignore\nexport const count: number = "one";\n',
  ],
  "danger.tsx": [
    "react(no-danger)",
    'export const Html = () => <div dangerouslySetInnerHTML={{ __html: "<b>x</b>" }} />;\n',
  ],
  "then.ts": [
    "promise(catch-or-return)",
    "export function go(answer: Promise<number>) {\n  answer.then((n) => n + 1);\n}\n",
  ],
  "deps.tsx": [
    "react-hooks(exhaustive-deps)",
    'import { useEffect, useState } from "react";\n' +
      "export function Echo({ text }: { text: string }) {\n" +
      '  const [shown, setShown] = useState("");\n' +
      "  useEffect(() => {\n    setShown(text);\n  }, []);\n" +
      "  return <p>{shown}</p>;\n}\n",
  ],
  "conditional-hook.tsx": [
    "react-hooks(rules-of-hooks)",
    'import { useState } from "react";\n' +
      "export function Maybe({ on }: { on: boolean }) {\n" +
      "  if (on) {\n    useState(0);\n  }\n  return null;\n}\n",
  ],
};

const ALLOWED_EXAMPLES: Record<string, string> = {
  "console-warn.ts": 'console.warn("odd but handled");\nconsole.error("a real problem");\n',
  "ts-expect-error.ts":
    '// @ts-expect-error -- the test needs a wrong type here\nexport const count: number = "one";\n',
  "caught.ts":
    "export function go(answer: Promise<number>) {\n" +
    "  return answer.then((n) => n + 1).catch(() => 0);\n}\n",
};

type Diagnostic = { code: string; filename: string };

// Configured as react/…, reported as react-hooks(…).
const REPORTED_AS: Record<string, string> = {
  "react/exhaustive-deps": "react-hooks(exhaustive-deps)",
  "react/rules-of-hooks": "react-hooks(rules-of-hooks)",
};

function lint(paths: string[], config = CONFIG): Diagnostic[] {
  const run = spawnSync(process.execPath, [OXLINT, "-c", config, "--format", "json", ...paths], {
    cwd: WEB_DIR,
    encoding: "utf8",
  });
  if (run.error) throw new Error(`oxlint didn't start: ${run.error.message}`);
  try {
    return (JSON.parse(run.stdout) as { diagnostics: Diagnostic[] }).diagnostics;
  } catch {
    // A broken config or a crash prints a message, not a JSON report: show oxlint's own words.
    throw new Error(`oxlint gave no report (exit ${run.status}): ${run.stderr || run.stdout}`);
  }
}

function codesFor(diagnostics: Diagnostic[], file: string): string[] {
  return diagnostics.filter((d) => d.filename.replaceAll("\\", "/").endsWith(`/${file}`)).map((d) => d.code);
}

let probeDir = "";
let diagnostics: Diagnostic[] = [];

beforeAll(() => {
  probeDir = mkdtempSync(join(tmpdir(), "countertalk-lint-"));
  const files = { ...ALLOWED_EXAMPLES, ...Object.fromEntries(Object.entries(BAD_EXAMPLES).map(([f, [, s]]) => [f, s])) };
  for (const [file, source] of Object.entries(files)) writeFileSync(join(probeDir, file), source);
  diagnostics = lint([probeDir]);
});

afterAll(() => {
  rmSync(probeDir, { recursive: true, force: true });
});

describe("oxlint", () => {
  it.each(Object.entries(BAD_EXAMPLES))("catches %s", (file, [code]) => {
    expect(codesFor(diagnostics, file)).toContain(code);
  });

  it.each(Object.keys(ALLOWED_EXAMPLES))("allows %s", (file) => {
    expect(codesFor(diagnostics, file)).toEqual([]);
  });

  it("shows oxlint's own message when it can't lint", () => {
    expect(() => lint([probeDir], join(probeDir, "missing.json"))).toThrow(/oxlint gave no report.+\S/s);
  });

  it("lets src/lib/api.ts, and only it, call fetch", () => {
    expect(lint([join(WEB_DIR, "src", "lib", "api.ts")])).toEqual([]);
  });

  it("has a bad example for every rule it turns on", () => {
    const config = JSON.parse(readFileSync(CONFIG, "utf8")) as { rules: Record<string, unknown> };
    const configured = Object.keys(config.rules).map(
      (rule) => REPORTED_AS[rule] ?? rule.replace(/^(.+?)\/(.+)$/, "$1($2)"),
    );
    const covered = new Set(Object.values(BAD_EXAMPLES).map(([code]) => code));

    expect(configured.filter((code) => !covered.has(code))).toEqual([]);
  });
});
