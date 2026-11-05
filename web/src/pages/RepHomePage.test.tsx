import { render, screen, within } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { BRIEF_ROWS, type Handler, REP, fakeApi, jsonResponse as json } from "../testUtils";
import userEvent from "@testing-library/user-event";
import type { FixedItem, PendingItem, ShopFixed, Visit } from "../types/api";

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
  return { team, category: "supply", problem: "Late deliveries.", note, resolved_at: resolvedAt, since_last_visit: true };
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

function pending(n: number, status: PendingItem["status"] = "open"): PendingItem {
  return {
    issue_id: `i-${n}`,
    shop: `Shop ${n}`,
    area: "Old Market",
    team: "Finance",
    problem: `Credit note ${n} hasn't come.`,
    status,
    raised_at: `2026-11-0${n}T06:00:00Z`,
  };
}

const PENDING = [pending(2, "in_progress"), pending(1)];

/** The visits list's first page, and its visits in review as the home asks for them. */
function visitsOf(items: Visit[]): Record<string, Handler> {
  const review = items.filter((item) => item.status === "review");
  return {
    "GET /api/visits": () => json(200, { items, next_cursor: null }),
    "GET /api/visits?status=review": () => json(200, { items: review, next_cursor: null }),
  };
}

function open(routesByCall: Record<string, Handler | Handler[]> = {}) {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, REP),
    ...visitsOf([visit("v-r", "s1", "review"), visit("v-c", "s2", "confirmed")]),
    "GET /api/shops": () => json(200, { items: SHOPS, next_cursor: null }),
    "GET /api/me/home": () => json(200, { to_review: 1, to_pass_on: 2 }),
    "GET /api/me/counts": () => json(200, { raised: 3, fixed: 1 }),
    "GET /api/me/fixed": () => json(200, { items: FIXED, next_cursor: null }),
    "GET /api/me/pending": () => json(200, { items: PENDING, next_cursor: null }),
    "GET /api/briefs": () => json(200, { items: BRIEF_ROWS, next_cursor: null }),
    ...routesByCall,
  });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/"] })} />);
  return api;
}

/** The stat cards, once the page is there. */
async function numbers() {
  return within(await screen.findByRole("list", { name: "Your numbers" }));
}

/** What each of a shop's fixes says, from its card under Fixed since the last visit. */
function fixesOf(fixes: HTMLElement, shop: string): Array<string | null> {
  const card = within(fixes).getByRole("heading", { level: 3, name: shop }).closest("li") as HTMLElement;
  return within(card)
    .getAllByRole("listitem")
    .map((item) => item.textContent);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RepHomePage", () => {
  it("greets the rep, and offers one Record a visit", async () => {
    open();

    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Demo Rep · PepsiCo India" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const record = screen.getAllByRole("link", { name: "Record a visit" });
    expect(record).toHaveLength(1);
    expect(record[0]).toHaveAttribute("href", "/record");
  });

  it("says each of the rep's numbers in plain words, each a link to where it goes", async () => {
    open();
    const cards = await numbers();

    expect(
      await cards.findByRole("link", { name: "1 visit waiting for you to check before teams see it" }),
    ).toHaveAttribute("href", "#ready-to-review");
    expect(await cards.findByRole("link", { name: "3 problems raised this month · 1 fixed" })).toHaveAttribute(
      "href",
      "/updates",
    );
    expect(cards.getByRole("link", { name: "2 shops have news to pass on" })).toHaveAttribute("href", "/updates");
  });

  it("words many visits to check and one shop to pass on", async () => {
    open({ "GET /api/me/home": () => json(200, { to_review: 12, to_pass_on: 1 }) });
    const cards = await numbers();

    expect(
      await cards.findByRole("link", { name: "12 visits waiting for you to check before teams see them" }),
    ).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "1 shop has news to pass on" })).toBeInTheDocument();
  });

  it("shows a dash, never a number, while the numbers load", async () => {
    const waiting = () => new Promise<Response>(() => {});
    open({ "GET /api/me/home": waiting, "GET /api/me/counts": waiting });
    const cards = await numbers();

    const shown = cards.getAllByRole("link").map((link) => link.textContent ?? "");
    expect(shown.map((text) => text.startsWith("–") && !/\d/.test(text))).toEqual([true, true, true]);
    expect(cards.getByRole("link", { name: "Visits waiting for you to check" })).toHaveAttribute("href", "#ready-to-review");
    expect(cards.getByRole("link", { name: "Problems raised this month" })).toHaveAttribute("href", "/updates");
    expect(cards.getByRole("link", { name: "Shops with news to pass on" })).toHaveAttribute("href", "/updates");
  });

  it("lists each visit waiting for review with its shop, day and length, and one Review link", async () => {
    open();

    const waiting = await screen.findByRole("region", { name: "Ready to review" });
    await within(waiting).findByText("Sunrise Kirana");
    const rows = within(waiting).getAllByRole("listitem");
    expect(rows).toHaveLength(1);
    const row = within(rows[0] as HTMLElement);
    expect(rows[0]).toHaveTextContent("Sunrise Kirana, Old Market");
    expect(rows[0]).toHaveTextContent("3 Nov 2026, 11:30 am · 1:35");
    const links = row.getAllByRole("link");
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([["Review", "/visits/v-r"]]);
    // No status badge: the heading says it already.
    expect(row.queryByText("Ready to review")).not.toBeInTheDocument();
  });

  it("lists the rest of the visits under Recent visits, each with its status", async () => {
    open();

    const recent = await screen.findByRole("region", { name: "Recent visits" });
    await within(recent).findByText("Lotus General Store");
    const links = within(recent).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/visits/v-c"]);
    expect(links[0]).toHaveTextContent("Lotus General Store, Old Market");
    expect(links[0]).toHaveTextContent("Confirmed");
  });

  it("explains the empty lists when there are no visits yet", async () => {
    open(visitsOf([]));

    const waiting = await screen.findByRole("region", { name: "Ready to review" });
    expect(
      await within(waiting).findByText("Nothing to check. New recordings show up here once they're processed."),
    ).toBeInTheDocument();
    const recent = screen.getByRole("region", { name: "Recent visits" });
    expect(within(recent).getByText("No visits yet. Tap Record a visit to start.")).toBeInTheDocument();
    expect(within(recent).queryAllByRole("link")).toEqual([]);
  });

  it("groups the fixes by shop, each with its team and the line to tell them, then See all updates", async () => {
    open();

    const fixes = await screen.findByRole("region", { name: "Fixed since the last visit" });
    expect(within(fixes).getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Sunrise Kirana, Old Market",
      "Lotus General Store, Old Market",
    ]);
    expect(fixesOf(fixes, "Sunrise Kirana, Old Market")).toEqual([
      expect.stringMatching(/^Distribution.*Tell them: Deliveries are on Mondays now\.$/),
      expect.stringMatching(/^Finance.*Tell them: Paid\.$/),
    ]);
    expect(fixesOf(fixes, "Lotus General Store, Old Market")).toEqual([
      expect.stringMatching(/^Sales operations.*Tell them: The scheme is on\.$/),
      expect.stringMatching(/^Finance.*Tell them: Credit note sent\.$/),
    ]);
    expect(within(fixes).getByRole("link", { name: "See all updates →" })).toHaveAttribute("href", "/updates");
  });

  it("lists only what's fixed since each shop's last visit, not the rep's older fixes (batch F7)", async () => {
    const older = { ...fix("Finance", "Paid last month.", "2026-10-20T06:00:00Z"), since_last_visit: false };
    const onlyOlder: ShopFixed = { shop_id: "s3", name: "Corner Daily Needs", area: "Station Road", items: [older] };
    const mixed: ShopFixed = { ...(FIXED[0] as ShopFixed), items: [...(FIXED[0] as ShopFixed).items, older] };
    open({ "GET /api/me/fixed": () => json(200, { items: [mixed, onlyOlder], next_cursor: null }) });

    const fixes = await screen.findByRole("region", { name: "Fixed since the last visit" });
    expect(within(fixes).getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Sunrise Kirana, Old Market",
    ]);
    expect(fixesOf(fixes, "Sunrise Kirana, Old Market")).toHaveLength(2);
    expect(within(fixes).queryByText(/Paid last month/)).not.toBeInTheDocument();
  });

  it("shows 3 visits to review, then View all (N) opens the rest in place", async () => {
    const waiting = [1, 2, 3, 4, 5].map((n) => visit(`v-${n}`, "s1", "review"));
    open(visitsOf(waiting));

    const section = await screen.findByRole("region", { name: "Ready to review" });
    await within(section).findAllByRole("link", { name: "Review" });
    expect(within(section).getAllByRole("link", { name: "Review" })).toHaveLength(3);
    const more = within(section).getByRole("button", { name: "View all (5)" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(more);

    expect(within(section).getAllByRole("link", { name: "Review" })).toHaveLength(5);
    expect(within(section).getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
  });

  it("lists every visit in review, not only those on the visits list's first page", async () => {
    open({
      "GET /api/visits": () => json(200, { items: [visit("v-c", "s2", "confirmed")], next_cursor: "more" }),
      "GET /api/visits?status=review": () => json(200, { items: [visit("v-old", "s1", "review")], next_cursor: null }),
    });

    const section = await screen.findByRole("region", { name: "Ready to review" });
    expect(await within(section).findByRole("link", { name: "Review" })).toHaveAttribute("href", "/visits/v-old");
  });

  it("says when its numbers couldn't be loaded, and Try again asks for them again", async () => {
    const failing = () => json(403, { error: { code: "forbidden", message: "Not now." } });
    open({ "GET /api/me/counts": [failing, () => json(200, { raised: 3, fixed: 1 })] });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Some of your numbers and lists couldn't be loaded.");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    const cards = await numbers();
    expect(await cards.findByRole("link", { name: "3 problems raised this month · 1 fixed" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists the latest 3 pre-visit briefs, marks those out of date, and View all opens /briefs", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Pre-visit briefs" });
    const rows = await within(section).findAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Shop 4, Old MarketGenerated 4 Nov 2026 · Up to date",
      "Shop 3, Old MarketGenerated 3 Nov 2026 · 2 visits since",
      "Shop 2, Old MarketGenerated 2 Nov 2026 · Up to date",
    ]);
    expect(within(rows[1] as HTMLElement).getByRole("link")).toHaveAttribute("href", "/briefs/s3");
    expect(within(section).getByRole("link", { name: "View all" })).toHaveAttribute("href", "/briefs");
  });

  it("explains the briefs when there are none yet", async () => {
    open({ "GET /api/briefs": () => json(200, { items: BRIEF_ROWS.slice(4), next_cursor: null }) });

    const section = await screen.findByRole("region", { name: "Pre-visit briefs" });
    expect(await within(section).findByText(/^No briefs yet\. Once a shop has 2 confirmed visits/)).toBeInTheDocument();
    expect(within(section).queryAllByRole("listitem")).toEqual([]);
  });

  it("has no View all for 3 or fewer", async () => {
    open();

    await screen.findByRole("region", { name: "Fixed since the last visit" });
    await screen.findByRole("region", { name: "Pending resolutions" });
    expect(screen.queryByRole("button", { name: /^View all/ })).not.toBeInTheDocument();
  });

  it("lists the rep's problems still being worked on: shop, team, status and day", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Pending resolutions" });
    const rows = within(section).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Shop 2, Old MarketFinanceIn progressraised 2 Nov 2026Problem: Credit note 2 hasn't come.",
      "Shop 1, Old MarketFinanceOpenraised 1 Nov 2026Problem: Credit note 1 hasn't come.",
    ]);
  });

  it("shows 3 pending, then View all (N)", async () => {
    const many = [4, 3, 2, 1].map((n) => pending(n));
    open({ "GET /api/me/pending": () => json(200, { items: many, next_cursor: null }) });

    const section = await screen.findByRole("region", { name: "Pending resolutions" });
    expect(within(section).getAllByRole("listitem")).toHaveLength(3);
    await userEvent.click(within(section).getByRole("button", { name: "View all (4)" }));

    expect(within(section).getAllByRole("listitem")).toHaveLength(4);
  });

  it("shows 3 shops' fixes, then View all (N) opens the rest", async () => {
    const more: ShopFixed[] = [3, 4].map((n) => ({
      shop_id: `s${n}`,
      name: `Shop ${n}`,
      area: "Old Market",
      items: [fix("Finance", "Paid.", "2026-10-29T06:00:00Z")],
    }));
    open({ "GET /api/me/fixed": () => json(200, { items: [...FIXED, ...more], next_cursor: null }) });

    const fixes = await screen.findByRole("region", { name: "Fixed since the last visit" });
    expect(within(fixes).getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Sunrise Kirana, Old Market",
      "Lotus General Store, Old Market",
      "Shop 3, Old Market",
    ]);
    expect(within(fixes).getByRole("link", { name: "See all updates →" })).toBeInTheDocument();
    await userEvent.click(within(fixes).getByRole("button", { name: "View all (4)" }));
    expect(within(fixes).getAllByRole("heading", { level: 3 })).toHaveLength(4);
  });

  it("says when nothing was raised, nothing waits and nothing's fixed yet", async () => {
    const api = open({
      "GET /api/me/home": () => json(200, { to_review: 0, to_pass_on: 0 }),
      "GET /api/me/counts": () => json(200, { raised: 0, fixed: 0 }),
      "GET /api/me/fixed": () => json(200, { items: [], next_cursor: null }),
      "GET /api/me/pending": () => json(200, { items: [], next_cursor: null }),
    });
    const cards = await numbers();

    expect(await cards.findByRole("link", { name: "Nothing raised yet this month" })).toHaveAttribute("href", "/updates");
    expect(cards.getByRole("link", { name: "No visits waiting for you to check" })).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "No news to pass on yet" })).toBeInTheDocument();
    // Shops, the visits list and its visits in review, the home, counts, fixed, pending and briefs.
    await vi.waitFor(() => expect(api.gets()).toBe(8));
    const fixes = await screen.findByRole("region", { name: "Fixed since the last visit" });
    expect(fixes).toHaveTextContent(
      "Nothing yet. When a team fixes a problem a shop raised, what to tell the shopkeeper shows up here.",
    );
    expect(within(fixes).queryByRole("link")).not.toBeInTheDocument();
    const pendingNone = screen.getByRole("region", { name: "Pending resolutions" });
    expect(pendingNone).toHaveTextContent("Nothing pending: no team is working on a problem you raised right now.");
  });
});
