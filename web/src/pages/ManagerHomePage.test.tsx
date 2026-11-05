import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { BRIEF_ROWS, ADMIN, type Handler, MANAGER, fakeApi, jsonResponse as json } from "../testUtils";
import type {
  CauseNumbers,
  Finding,
  IssueStatus,
  Me,
  Measures,
  OrgNumbers,
  Overview,
  Summary,
  SummaryPage,
  SummaryStatus,
} from "../types/api";

const OVERVIEW: Overview = {
  open: 5,
  in_progress: 2,
  overdue: 1,
  resolved_this_week: 3,
  triage: 4,
  visits_this_week: 12,
  names_to_review: null,
};

function finding(n: number, shop: string, visitedAt: string): Finding {
  return {
    insight: {
      id: `i-${n}`,
      seq: 1,
      kind: "complaint",
      category: "other",
      severity: "medium",
      summary_en: `Finding ${n} said at ${shop}.`,
      verified: true,
      visible: true,
      added_by_rep: false,
      sku: null,
      competitor: null,
      scheme_key: null,
      ai_team: null,
      ai_team_reason: null,
      quotes: [],
      routes: [],
      suggested_team: null,
      locked_team_id: null,
      chosen_by_rep: false,
      left_by_rep: false,
      routing_version: 1,
    },
    shop_name: shop,
    area: "Old Market",
    rep_name: "Demo Rep",
    visit_id: `v-${n}`,
    visited_at: visitedAt,
  };
}

// Newest first, as the server sends them.
const TRIAGE = [
  finding(1, "Sunrise Kirana", "2026-11-03T06:00:00Z"),
  finding(2, "Lotus General Store", "2026-11-02T06:00:00Z"),
];

const PERIOD = { kind: "week" as const, start: "2026-10-25T18:30:00Z", end: "2026-11-01T18:30:00Z" };

function measures(openShops: number): Measures {
  return {
    severity: "high",
    shops: openShops,
    insights: openShops,
    reps: 1,
    visits: openShops,
    raised_again: 0,
    open_shops: openShops,
    age_weeks: 1,
    overdue: false,
    baseline: 0,
    seen_periods: 1,
    label: "new",
    score: 6 * openShops,
    last_seen_at: "2026-11-01T06:00:00Z",
  };
}

function cause(key: string, title: string, openShops: number, teams: Array<[string, IssueStatus]>): CauseNumbers {
  return {
    issue_key: key,
    title_en: title,
    category: "other",
    teams: teams.map(([name, status]) => ({
      team_key: name.toLowerCase(),
      team_name: name,
      issue_id: `${key}-${name}`,
      status,
    })),
    measures: measures(openShops),
  };
}

// No team still has it.
const RESOLVED = cause("credit", "Credit note not sent", 0, [["Finance", "resolved"]]);

// Ranked as the summary's score ranks them; the resolved one isn't open any more.
const CAUSES = [
  cause("payout", "Kurkure Diwali payout late", 5, [
    ["Finance", "open"],
    ["Sales operations", "in_progress"],
  ]),
  cause("deliveries", "Late deliveries in Old Market", 3, [["Distribution", "in_progress"]]),
  RESOLVED,
  cause("price", "Lay's price above MRP", 1, [["Sales operations", "open"]]),
  cause("stock", "Out of stock on Kurkure", 2, [["Distribution", "open"]]),
];

function org(causes: CauseNumbers[]): OrgNumbers {
  return {
    period: PERIOD,
    causes,
    totals: { insights: 0, shops: 0, reps: 0, visits: 0, triage: 0 },
    by_category_area: [],
    competitors: [],
    issues_at_end: { open: 0, in_progress: 0, overdue: 0 },
    resolved_in_period: 0,
  };
}

function summary(status: SummaryStatus): Summary {
  return {
    id: "sum-1",
    scope: "org",
    team_id: null,
    period_start: PERIOD.start,
    period_end: PERIOD.end,
    status,
    numbers: null,
    counted_at: null,
    narrative: null,
    model: null,
    failure_reason: null,
  };
}

function summaryPage(causes: CauseNumbers[], given: Summary | null): SummaryPage {
  return {
    period: "week",
    period_start: PERIOD.start,
    period_end: PERIOD.end,
    numbers: org(causes),
    numbers_as_of: "2026-11-04T06:00:00Z",
    summary: given,
  };
}

const SUMMARIES = "GET /api/summaries?scope=org&period=week";

function open(me: Me = MANAGER, routesByCall: Record<string, Handler | Handler[]> = {}) {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, me),
    "GET /api/me/overview": () => json(200, { ...OVERVIEW, names_to_review: me === ADMIN ? 6 : null }),
    "GET /api/issues/triage": () => json(200, TRIAGE),
    [SUMMARIES]: () => json(200, summaryPage(CAUSES, summary("ready"))),
    "GET /api/briefs": () => json(200, { items: BRIEF_ROWS, next_cursor: null }),
    "GET /api/me/reps": () => json(200, REPS),
    ...routesByCall,
  });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/"] })} />);
  return api;
}

const REPS = [
  { rep_id: "r-1", rep_name: "Demo Rep", confirmed: 4, in_review: 1, pending: 2 },
  { rep_id: "r-2", rep_name: "Demo Rep 2", confirmed: 0, in_review: 0, pending: 0 },
];

/** The stat cards, once the page is there. */
async function numbers() {
  return within(await screen.findByRole("list", { name: "Your numbers" }));
}

/** Each card link's name and where it goes, once the numbers are in. */
async function cardsOf() {
  const cards = await numbers();
  await cards.findByRole("link", { name: /^\d+ issues open/ });
  // Its text, but for the arrow a phone shows.
  return cards.getAllByRole("link").map((link) => [link.textContent?.replace(/→$/, ""), link.getAttribute("href")]);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ManagerHomePage", () => {
  it("greets the manager, and offers Ask a question, never Record a visit", async () => {
    open();

    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Demo Manager · PepsiCo India" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    const ask = screen.getAllByRole("link", { name: "Ask a question" });
    expect(ask).toHaveLength(1);
    expect(ask[0]).toHaveAttribute("href", "/inbox");
    expect(screen.queryByRole("link", { name: "Record a visit" })).not.toBeInTheDocument();
  });

  it("says the org's 4 numbers in plain words, each a link to where it goes; no catalog for a manager", async () => {
    open();
    const cards = await numbers();

    expect(await cards.findByRole("link", { name: "4 findings waiting in triage for a team" })).toHaveAttribute(
      "href",
      "/inbox/triage",
    );
    expect(cards.getByRole("link", { name: "7 issues open across your teams" })).toHaveAttribute("href", "/inbox");
    expect(cards.getByRole("link", { name: "3 issues resolved this week" })).toHaveAttribute(
      "href",
      "/inbox?status=resolved",
    );
    expect(cards.getByRole("link", { name: "12 visits by your reps this week" })).toHaveAttribute("href", "/visits");
    expect(cards.getAllByRole("link")).toHaveLength(4);
    expect(screen.queryByRole("link", { name: /catalog/i })).not.toBeInTheDocument();
  });

  it("adds an admin's fifth card: the catalog's unknown names to review", async () => {
    open(ADMIN);

    expect(await cardsOf()).toEqual([
      ["4 findings waiting in triage for a team", "/inbox/triage"],
      ["7 issues open across your teams", "/inbox"],
      ["3 issues resolved this week", "/inbox?status=resolved"],
      ["12 visits by your reps this week", "/visits"],
      ["6 unknown names to review in the catalog", "/catalog"],
    ]);
    const cards = await numbers();
    expect(cards.getByRole("link", { name: "6 unknown names to review in the catalog" })).toHaveAttribute(
      "href",
      "/catalog",
    );
  });

  it("says when there's nothing, never 0 read out", async () => {
    const none = { open: 0, in_progress: 0, overdue: 0, resolved_this_week: 0, triage: 0, visits_this_week: 0, names_to_review: 0 };
    open(ADMIN, { "GET /api/me/overview": () => json(200, none) });
    const cards = await numbers();

    expect(await cards.findByRole("link", { name: "Nothing waiting in triage" })).toHaveAttribute("href", "/inbox/triage");
    expect(cards.getByRole("link", { name: "No open issues" })).toHaveAttribute("href", "/inbox");
    expect(cards.getByRole("link", { name: "No issues resolved yet this week" })).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "No visits yet this week" })).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "No unknown names to review" })).toHaveAttribute("href", "/catalog");
  });

  it("shows a dash, never a number, while the numbers load", async () => {
    open(MANAGER, { "GET /api/me/overview": () => new Promise<Response>(() => {}) });
    const cards = await numbers();

    const shown = cards.getAllByRole("link").map((link) => link.textContent ?? "");
    expect(shown.map((text) => text.startsWith("–") && !/\d/.test(text))).toEqual([true, true, true, true]);
    expect(cards.getByRole("link", { name: "Findings waiting in triage" })).toHaveAttribute("href", "/inbox/triage");
    expect(cards.getByRole("link", { name: "Issues open across your teams" })).toHaveAttribute("href", "/inbox");
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
  });

  it("lists triage's findings newest first, each with its shop, what was said and its day", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Waiting in triage" });
    await within(section).findByText("Sunrise Kirana");
    const links = within(section).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(["/inbox/triage", "/inbox/triage"]);
    expect(links[0]).toHaveAccessibleName("Sunrise Kirana, Old Market Finding 1 said at Sunrise Kirana. 3 Nov 2026");
    expect(links[1]).toHaveAccessibleName(
      "Lotus General Store, Old Market Finding 2 said at Lotus General Store. 2 Nov 2026",
    );
    expect(within(section).queryByRole("button", { name: /^View all/ })).not.toBeInTheDocument();
  });

  it("shows 3 in triage, then View all (N) opens the rest in place", async () => {
    const many = [1, 2, 3, 4, 5].map((n) => finding(n, `Shop ${n}`, `2026-11-0${6 - n}T06:00:00Z`));
    open(MANAGER, { "GET /api/issues/triage": () => json(200, many) });

    const section = await screen.findByRole("region", { name: "Waiting in triage" });
    await within(section).findByText("Shop 1");
    expect(within(section).getAllByRole("listitem")).toHaveLength(3);
    const more = within(section).getByRole("button", { name: "View all (5)" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(more);

    const rows = within(section).getAllByRole("listitem").map((row) => row.textContent);
    expect(rows.map((text) => text?.slice(0, 6))).toEqual(["Shop 1", "Shop 2", "Shop 3", "Shop 4", "Shop 5"]);
    expect(within(section).getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
  });

  it("ranks the top open issues as last week's summary does, each team with its status", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Top open issues" });
    await within(section).findByText("Kurkure Diwali payout late");
    const links = within(section).getAllByRole("link");
    // Batch F7: each opens its issue, the first team still working it, in that team's inbox.
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/inbox?team=finance&issue=payout-Finance",
      "/inbox?team=distribution&issue=deliveries-Distribution",
      "/inbox?team=sales+operations&issue=price-Sales+operations",
    ]);
    expect(links[0]).toHaveAccessibleName(
      "Kurkure Diwali payout late Finance: Open Sales operations: In progress 5 shops waiting on a fix",
    );
    expect(links[1]).toHaveAccessibleName("Late deliveries in Old Market Distribution: In progress 3 shops waiting on a fix");
    expect(links[2]).toHaveAccessibleName("Lay's price above MRP Sales operations: Open 1 shop waiting on a fix");
    // Each team a pill.
    expect(within(links[0] as HTMLElement).getByText("Finance: Open")).toHaveClass("rounded-full");
    expect(within(links[0] as HTMLElement).getByText("Sales operations: In progress")).toHaveClass("rounded-full");
    // No team still has it: it isn't open any more.
    expect(within(section).queryByText("Credit note not sent")).not.toBeInTheDocument();

    await userEvent.click(within(section).getByRole("button", { name: "View all (4)" }));
    expect(within(section).getAllByRole("link")).toHaveLength(4);
    expect(within(section).getAllByRole("link")[3]).toHaveAccessibleName(
      "Out of stock on Kurkure Distribution: Open 2 shops waiting on a fix",
    );
  });

  it("links last week's summary, ready to read", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Latest summary" });
    expect(await within(section).findByText("Last week's summary")).toBeInTheDocument();
    expect(section).toHaveTextContent("26 Oct – 1 Nov 2026");
    expect(section).toHaveTextContent("Ready to read");
    expect(within(section).getByRole("link", { name: "Read it →" })).toHaveAttribute("href", "/summaries?scope=org");
  });

  it.each([
    ["writing", "Being written…"],
    ["failed", "Couldn't be written"],
  ] as const)("says a %s summary's status", async (status, said) => {
    open(MANAGER, { [SUMMARIES]: () => json(200, summaryPage(CAUSES, summary(status))) });

    const section = await screen.findByRole("region", { name: "Latest summary" });
    expect(await within(section).findByText(said)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Read it →" })).toHaveAttribute("href", "/summaries?scope=org");
  });

  it("gives a manager the latest 3 pre-visit briefs and View all", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Pre-visit briefs" });
    const rows = await within(section).findAllByRole("listitem");
    expect(rows).toHaveLength(3);
    expect(rows[1]).toHaveTextContent("2 visits since");
    expect(within(section).getByRole("link", { name: "View all" })).toHaveAttribute("href", "/briefs");
  });

  it.each([
    ["a manager", MANAGER],
    ["an admin", ADMIN],
  ])("shows %s each rep's card under triage, by name, with their three numbers, in words at none", async (_who, me) => {
    open(me, { "GET /api/issues/triage": () => json(200, []) });

    const section = await screen.findByRole("region", { name: "Reps" });
    const busy = await within(section).findByRole("list", { name: "Demo Rep's numbers" });
    const quiet = within(section).getByRole("list", { name: "Demo Rep 2's numbers" });

    const triage = screen.getByRole("region", { name: "Waiting in triage" });
    expect(section.parentElement).toBe(triage.parentElement);
    expect(triage.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(section).getAllByRole("heading", { level: 3 }).map((name) => name.textContent)).toEqual([
      "Demo Rep",
      "Demo Rep 2",
    ]);
    const said = (list: HTMLElement) => within(list).getAllByRole("listitem").map((item) => item.textContent);
    expect(said(busy)).toEqual(["4 visits confirmed", "1 visit in review", "2 issues pending"]);
    expect(said(quiet)).toEqual(["0 No visits confirmed", "0 No visits in review", "0 No issues pending"]);
    // Each 0 is only shown: the words say it.
    for (const zero of within(quiet).getAllByText("0")) expect(zero).toHaveAttribute("aria-hidden", "true");
    expect(within(section).queryByRole("link")).not.toBeInTheDocument();
  });

  it("explains each empty section", async () => {
    const api = open(MANAGER, {
      "GET /api/issues/triage": () => json(200, []),
      [SUMMARIES]: () => json(200, summaryPage([RESOLVED], null)),
    });

    const triage = await screen.findByRole("region", { name: "Waiting in triage" });
    expect(await within(triage).findByText("Nothing waiting: every finding has a team.")).toBeInTheDocument();
    await vi.waitFor(() => expect(api.gets()).toBe(5));
    const issues = screen.getByRole("region", { name: "Top open issues" });
    expect(await within(issues).findByText("No open issues at the end of last week.")).toBeInTheDocument();
    const latest = screen.getByRole("region", { name: "Latest summary" });
    expect(latest).toHaveTextContent("No summary yet for last week. It's written once the week ends.");
    for (const section of [triage, issues, latest]) expect(within(section).queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^View all/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("says a load failed, and Try again loads it", async () => {
    const refused = () => json(403, { error: { code: "forbidden", message: "Not now." } });
    open(MANAGER, { "GET /api/me/overview": [refused, () => json(200, OVERVIEW)] });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Your numbers couldn't be loaded: Not now.");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    const cards = await numbers();
    expect(await cards.findByRole("link", { name: "7 issues open across your teams" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
