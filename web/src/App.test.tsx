import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "./App";
import { ADMIN, APP_SOURCES, FINANCE_MEMBER, jsonResponse, MANAGER, REP } from "./testUtils";
import type { Me } from "./types/api";

/** The app at `path`, signed in as `me` (or signed out): every other request never answers. */
function renderAt(path: string, me: Me | null = MANAGER, extra: Record<string, Response> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      const route = `${init?.method ?? "GET"} ${url}`;
      if (route === "GET /api/auth/me") {
        return Promise.resolve(
          me === null ? jsonResponse(401, { error: { code: "signed_out", message: "Sign in to carry on" } }) : jsonResponse(200, me),
        );
      }
      if (route in extra) return Promise.resolve(extra[route]);
      return new Promise<Response>(() => {});
    }),
  );
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return router;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("App", () => {
  it.each([
    ["/", MANAGER, "Visits"],
    ["/record", REP, "Record a visit"],
    ["/visits/v-1", MANAGER, "Visit"],
    ["/inbox", MANAGER, "Inbox"],
    ["/inbox/triage", MANAGER, "Triage"],
    ["/health", null, "Server status"],
    ["/signin", null, "Sign in"],
  ])("shows its page at %s", async (path, me, heading) => {
    renderAt(path, me);

    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
  });

  it.each(["/no-such-page", "/.env"])("shows a page, not a blank screen, at %s", (path) => {
    renderAt(path);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/not found/i);
    expect(screen.getByRole("link", { name: /home/i })).toHaveAttribute("href", "/");
  });

  it("sends someone signed out to sign in, and back after", async () => {
    const router = renderAt("/inbox?team=finance", null);

    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/signin");
    expect(new URLSearchParams(router.state.location.search).get("next")).toBe("/inbox?team=finance");
  });

  it.each([
    [REP, true],
    [MANAGER, false],
  ])("offers %s.role a record button only if they record", async (me, offered) => {
    renderAt("/", me);

    await screen.findByRole("heading", { level: 1, name: "Visits" });
    expect(screen.queryByRole("link", { name: "Record a visit" }) !== null).toBe(offered);
  });

  it("starts a team member at their inbox", async () => {
    const router = renderAt("/", FINANCE_MEMBER);

    expect(await screen.findByRole("heading", { level: 1, name: "Inbox" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/inbox");
  });

  it.each([
    ["/inbox", REP],
    ["/summaries", REP],
    ["/inbox/triage", FINANCE_MEMBER],
    ["/record", MANAGER],
    ["/catalog", MANAGER],
  ])("shows not-found at %s to a role that doesn't use it", async (path, me) => {
    renderAt(path, me);

    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(/not found/i);
  });

  it.each([
    [REP, ["Visits", "Updates"]],
    [FINANCE_MEMBER, ["Inbox", "Summaries"]],
    [MANAGER, ["Visits", "Inbox", "Triage", "Summaries"]],
    [ADMIN, ["Visits", "Inbox", "Triage", "Summaries", "Catalog"]],
  ])("shows %s.role's pages in the header", async (me, links) => {
    renderAt("/visits/v-1", me);

    const nav = await screen.findByRole("navigation", { name: "Pages" });
    expect([...nav.querySelectorAll("a")].map((a) => a.textContent)).toEqual(links);
  });

  it("says who uses which page in lib/session.tsx alone (R1)", () => {
    const owners = ["./lib/session.tsx", "./types/api.ts", "./testUtils.ts"];
    const writes = /"(team_member|manager|admin)"|\.role [!=]==/;

    const found = Object.entries(APP_SOURCES).filter(([path, source]) => !owners.includes(path) && writes.test(source));

    expect(Object.keys(APP_SOURCES).length).toBeGreaterThan(10);
    expect(found.map(([path]) => path)).toEqual([]);
  });

  it("signs out, and goes to sign in", async () => {
    const router = renderAt("/visits/v-1", MANAGER, { "POST /api/auth/signout": new Response(null, { status: 204 }) });

    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/signin");
  });

  it("goes to sign in when the session ends while the page is open", async () => {
    const ended = jsonResponse(401, { error: { code: "signed_out", message: "Sign in to carry on" } });
    const router = renderAt("/visits/v-1", MANAGER, { "GET /api/visits/v-1": ended });

    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(new URLSearchParams(router.state.location.search).get("next")).toBe("/visits/v-1");
  });
});
