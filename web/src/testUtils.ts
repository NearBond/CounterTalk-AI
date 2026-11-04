import { vi } from "vitest";
import type { Brief, BriefRow, Me, ShopBrief } from "./types/api";

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
  org_name: "PepsiCo India",
  ai_key_set: true,
};

export const FINANCE_MEMBER: Me = {
  id: "u-finance",
  name: "Finance Member",
  email: "finance@example.com",
  role: "team_member",
  team: { id: "t-finance", key: "finance", name: "Finance" },
  org_name: "PepsiCo India",
  ai_key_set: true,
};

export const ADMIN: Me = {
  id: "u-admin",
  name: "Demo Admin",
  email: "admin@example.com",
  role: "admin",
  team: null,
  org_name: "PepsiCo India",
  ai_key_set: true,
};

export const REP: Me = {
  id: "u-rep",
  name: "Demo Rep",
  email: "rep@example.com",
  role: "rep",
  team: null,
  org_name: "PepsiCo India",
  ai_key_set: true,
};

const ME = "GET /api/auth/me";
const DEMO_ACCOUNTS = "GET /api/auth/demo-accounts";

/** A fake API: answers by "METHOD /path", recording each request's body; one may wait. Who's
 * signed in is MANAGER unless the routes answer GET /api/auth/me themselves. */
/** A shop on /briefs (batch F5). */
export function briefRow(n: number, more: Partial<BriefRow> = {}): BriefRow {
  return {
    shop_id: `s${n}`,
    name: `Shop ${n}`,
    area: "Old Market",
    visits: 4,
    state: "up_to_date",
    since: 0,
    generated_at: `2026-11-0${n}T06:00:00Z`,
    ...more,
  };
}

/** /api/briefs as the server orders it: four briefs newest first, then one with none, then one
 * with too few visits. */
export const BRIEF_ROWS: BriefRow[] = [
  briefRow(4),
  briefRow(3, { state: "out_of_date", since: 2 }),
  briefRow(2),
  briefRow(1, { state: "out_of_date", since: 1 }),
  briefRow(5, { state: "none", generated_at: null }),
  briefRow(6, { state: "not_enough_visits", visits: 1, generated_at: null }),
];

/** A brief, every part filled in. */
export const BRIEF: Brief = {
  id: "b-1",
  generated_at: "2026-11-02T06:00:00Z",
  after_visit: 4,
  overview: ["The shop raises late credit notes most.", "It orders Lay's every visit."],
  last_visit: {
    visited_at: "2026-11-01T06:00:00Z",
    raised: [{ category: "Payments", problem: "The credit note is late.", quote: "पैसा अभी तक नहीं आया" }],
    ordered: [
      { name: "Lay's Classic", quantity: 2, unit: "boxes" },
      { name: "Kurkure", quantity: null, unit: null },
    ],
  },
  still_open: [
    { team: "Finance", status: "in_progress", problem: "The credit note is late.", raised_at: "2026-11-01T06:00:00Z" },
  ],
  fixed_since: [
    { team: "Distribution", problem: "Deliveries came late.", tell_them: "Deliveries are on Mondays now.", resolved_at: "2026-11-01T09:00:00Z" },
  ],
  worth_raising: ["Ask whether the credit note has come."],
};

/** A shop's brief as GET /api/shops/{id}/brief answers: up to date, nothing to generate. */
export function shopBrief(more: Partial<ShopBrief> = {}): ShopBrief {
  return {
    shop_id: "s1",
    name: "Sunrise Kirana",
    area: "Old Market",
    visits: 4,
    state: "up_to_date",
    since: 0,
    can_generate: false,
    brief: BRIEF,
    ...more,
  };
}

export function fakeApi(given: Record<string, Handler | Handler[]>) {
  const routes: Record<string, Handler | Handler[]> = {
    [ME]: () => jsonResponse(200, MANAGER),
    // The sign-in page asks for the demo accounts (batch F9): none, unless a test lists some.
    [DEMO_ACCOUNTS]: () => jsonResponse(200, { accounts: [], password: null }),
    ...given,
  };
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
    gets: () =>
      sent.filter((request) => request.route.startsWith("GET") && request.route !== ME && request.route !== DEMO_ACCOUNTS)
        .length,
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
