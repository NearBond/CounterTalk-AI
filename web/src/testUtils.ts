import { vi } from "vitest";
import type { Me } from "./types/api";

/** Helpers for tests that fake the API. */

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** fetch rejects like the real one when the request's signal is aborted. */
export function rejectOnAbort(init: RequestInit | undefined, reject: (reason: unknown) => void) {
  init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
}

export type Handler = (body: unknown) => Response | Promise<Response>;

/** The signed-in user the fake API answers GET /api/auth/me with, unless a test says: a manager,
 * who sees every page (batch 16). */
export const MANAGER: Me = {
  id: "u-manager",
  name: "Demo Manager",
  email: "manager@example.com",
  role: "manager",
  team: null,
  org_name: "Demo Org",
};

export const FINANCE_MEMBER: Me = {
  id: "u-finance",
  name: "Finance Member",
  email: "finance@example.com",
  role: "team_member",
  team: { id: "t-finance", key: "finance", name: "Finance" },
  org_name: "Demo Org",
};

export const ADMIN: Me = {
  id: "u-admin",
  name: "Demo Admin",
  email: "admin@example.com",
  role: "admin",
  team: null,
  org_name: "Demo Org",
};

export const REP: Me = {
  id: "u-rep",
  name: "Demo Rep",
  email: "rep@example.com",
  role: "rep",
  team: null,
  org_name: "Demo Org",
};

const ME = "GET /api/auth/me";

/** A fake API: answers by "METHOD /path", recording each request's body; one may wait. Who's
 * signed in is MANAGER unless the routes answer GET /api/auth/me themselves. */
export function fakeApi(given: Record<string, Handler | Handler[]>) {
  const routes: Record<string, Handler | Handler[]> = { [ME]: () => jsonResponse(200, MANAGER), ...given };
  const sent: Array<{ route: string; body: unknown }> = [];
  const fake = vi.fn(
    (url: string, init?: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        rejectOnAbort(init, reject);
        const route = `${init?.method ?? "GET"} ${url}`;
        const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
        sent.push({ route, body });
        const found = routes[route];
        const handler = Array.isArray(found) ? found.shift() : found;
        if (handler === undefined) {
          resolve(jsonResponse(500, { error: { code: "no_fake", message: `nothing for ${route}` } }));
          return;
        }
        void Promise.resolve(handler(body)).then(resolve, reject);
      }),
  );
  vi.stubGlobal("fetch", fake);
  return {
    sent: () => sent.filter((request) => !request.route.startsWith("GET")),
    // Who's signed in is asked once per page load: not one of the page's own reads.
    gets: () => sent.filter((request) => request.route.startsWith("GET") && request.route !== ME).length,
  };
}

/** Every source file of the app, tests aside, as text, by its path from src/: the scan tests
 * read them (R1's guards). */
export const APP_SOURCES = import.meta.glob<string>(["./**/*.tsx", "./**/*.ts", "!./**/*.test.tsx", "!./**/*.test.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
});

/** The app's source files that contain `text`, but for `owner`, the one allowed to. */
export function sourcesWith(text: string, owner: string): string[] {
  return Object.entries(APP_SOURCES)
    .filter(([path, source]) => !path.endsWith(`/${owner}`) && source.includes(text))
    .map(([path]) => path);
}
