import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { type Handler, REP, fakeApi, jsonResponse as json } from "../testUtils";
import type { FixedItem, ShopFixed } from "../types/api";

const FIXED: FixedItem = {
  team: "Distribution",
  category: "supply",
  problem: "The last two Kurkure deliveries came a week late.",
  note: "Your distributor now delivers every Monday.",
  resolved_at: "2026-11-02T06:30:00Z",
};
const SUNRISE: ShopFixed = { shop_id: "s1", name: "Sunrise Kirana", area: "Old Market", items: [FIXED] };
const LOTUS: ShopFixed = {
  shop_id: "s2",
  name: "Lotus General Store",
  area: "Old Market",
  items: [{ ...FIXED, team: "Finance", problem: "The credit note is late.", note: "It's paid." }],
};

function open(routesByCall: Record<string, Handler | Handler[]>) {
  const api = fakeApi({ "GET /api/auth/me": () => json(200, REP), ...routesByCall });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/updates"] })} />);
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("UpdatesPage", () => {
  it("lists each shop's fixes in the server's order, with the team, day and line to say", async () => {
    open({
      "GET /api/me/counts": () => json(200, { raised: 3, fixed: 1 }),
      "GET /api/me/fixed": () => json(200, { items: [SUNRISE, LOTUS], next_cursor: null }),
    });

    expect(await screen.findByText("This month you raised 3 problems. 1 is fixed.")).toBeInTheDocument();
    const shops = await screen.findAllByRole("region");
    expect(shops.map((shop) => within(shop).getByRole("heading", { level: 2 }).textContent)).toEqual([
      "Sunrise Kirana, Old Market",
      "Lotus General Store, Old Market",
    ]);
    const [item] = within(shops[0] as HTMLElement).getAllByRole("listitem");
    expect(item).toHaveTextContent("Distribution · 2 Nov 2026");
    expect(item).toHaveTextContent(FIXED.problem);
    expect(item).toHaveTextContent(`Tell them: ${FIXED.note}`);
  });

  it("says when there's nothing new, and hides the counts when none were raised", async () => {
    const api = open({
      "GET /api/me/counts": () => json(200, { raised: 0, fixed: 0 }),
      "GET /api/me/fixed": () => json(200, { items: [], next_cursor: null }),
    });

    expect(await screen.findByText("Nothing new to pass on.")).toBeInTheDocument();
    await vi.waitFor(() => expect(api.gets()).toBe(2));
    expect(screen.queryByText(/^This month you raised/)).not.toBeInTheDocument();
  });

  it.each([
    [{ raised: 12, fixed: 7 }, "This month you raised 12 problems. 7 are fixed."],
    [{ raised: 1, fixed: 1 }, "This month you raised 1 problem. 1 is fixed."],
  ])("says the rep's own counts: %j", async (counts, line) => {
    open({
      "GET /api/me/counts": () => json(200, counts),
      "GET /api/me/fixed": () => json(200, { items: [], next_cursor: null }),
    });

    expect(await screen.findByText(line)).toBeInTheDocument();
  });

  it("says when the updates can't be loaded, with Try again", async () => {
    open({
      "GET /api/me/counts": () => json(200, { raised: 0, fixed: 0 }),
      // A 4xx isn't retried: the message shows at once.
      "GET /api/me/fixed": () => json(403, { error: { code: "forbidden", message: "No." } }),
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("The updates couldn't be loaded: No.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
