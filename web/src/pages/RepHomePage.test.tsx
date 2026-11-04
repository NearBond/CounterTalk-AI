import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { type Handler, REP, fakeApi, jsonResponse as json } from "../testUtils";
import type { FixedItem, ShopFixed, Visit } from "../types/api";

function visit(id: string, shopId: string, status: Visit["status"]): Visit {
  return {
    id,
    shop_id: shopId,
    rep_id: "u-rep",
    kind: "conversation",
    client: "web",
    status,
    failure_reason: null,
    duration_ms: 95_000,
    processing: false,
    completed_at: null,
    audio_deleted_at: null,
    created_at: "2026-11-03T06:00:00Z",
    updated_at: "2026-11-03T06:00:00Z",
  };
}

const SHOPS = [
  { id: "s1", name: "Sunrise Kirana", area: "Old Market", do_not_record: false, consent: "yes" },
  { id: "s2", name: "Lotus General Store", area: "Old Market", do_not_record: false, consent: "yes" },
];

function fix(team: string, note: string, resolvedAt: string): FixedItem {
  return { team, category: "supply", problem: "Late deliveries.", note, resolved_at: resolvedAt };
}

const FIXED: ShopFixed[] = [
  {
    shop_id: "s1",
    name: "Sunrise Kirana",
    area: "Old Market",
    items: [fix("Distribution", "Deliveries are on Mondays now.", "2026-11-03T06:00:00Z"), fix("Finance", "Paid.", "2026-11-01T06:00:00Z")],
  },
  {
    shop_id: "s2",
    name: "Lotus General Store",
    area: "Old Market",
    items: [fix("Sales operations", "The scheme is on.", "2026-11-02T06:00:00Z"), fix("Finance", "Credit note sent.", "2026-10-30T06:00:00Z")],
  },
];

function open(routesByCall: Record<string, Handler | Handler[]> = {}) {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, REP),
    "GET /api/visits": () =>
      json(200, { items: [visit("v-r", "s1", "review"), visit("v-c", "s2", "confirmed")], next_cursor: null }),
    "GET /api/shops": () => json(200, { items: SHOPS, next_cursor: null }),
    "GET /api/me/home": () => json(200, { to_review: 1, to_pass_on: 2 }),
    "GET /api/me/counts": () => json(200, { raised: 3, fixed: 1 }),
    "GET /api/me/fixed": () => json(200, { items: FIXED, next_cursor: null }),
    ...routesByCall,
  });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/"] })} />);
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RepHomePage", () => {
  it("greets the rep and shows their cards: record, this month, to review and to pass on", async () => {
    open();

    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Demo Rep · Demo Org" })).toBeInTheDocument();
    const cards = screen.getByRole("list", { name: "Shortcuts" });
    expect(await within(cards).findByRole("link", { name: "This month 3 raised · 1 fixed" })).toHaveAttribute("href", "/updates");
    expect(within(cards).getByRole("link", { name: "Record a visit" })).toHaveAttribute("href", "/record");
    expect(await within(cards).findByRole("link", { name: "To review 1" })).toHaveAttribute("href", "#ready-to-review");
    expect(within(cards).getByRole("link", { name: "To pass on 2 shops" })).toHaveAttribute("href", "/updates");
  });

  it("lists the visits waiting for review apart from the rest, each with Review", async () => {
    open();

    const waiting = await screen.findByRole("region", { name: "Ready to review" });
    const [ready] = await within(waiting).findAllByRole("link");
    expect(ready).toHaveAttribute("href", "/visits/v-r");
    expect(ready).toHaveTextContent("Review");
    const recent = within(screen.getByRole("region", { name: "Recent visits" })).getAllByRole("link");
    expect(recent.map((link) => link.getAttribute("href"))).toEqual(["/visits/v-c"]);
  });

  it("shows the 3 newest fixes with their shop, team and line to say, then See all updates", async () => {
    open();

    const fixes = await screen.findByRole("region", { name: "Fixed since the last visit" });
    expect(within(fixes).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Sunrise Kirana · DistributionTell them: Deliveries are on Mondays now.",
      "Lotus General Store · Sales operationsTell them: The scheme is on.",
      "Sunrise Kirana · FinanceTell them: Paid.",
    ]);
    expect(within(fixes).getByRole("link", { name: "See all updates →" })).toHaveAttribute("href", "/updates");
  });

  it("says when nothing was raised this month, and hides the fixes when there are none", async () => {
    const api = open({
      "GET /api/me/counts": () => json(200, { raised: 0, fixed: 0 }),
      "GET /api/me/fixed": () => json(200, { items: [], next_cursor: null }),
    });

    expect(await screen.findByRole("link", { name: "This month Nothing raised yet this month" })).toBeInTheDocument();
    await vi.waitFor(() => expect(api.gets()).toBe(5));
    expect(screen.queryByRole("region", { name: "Fixed since the last visit" })).not.toBeInTheDocument();
  });
});
