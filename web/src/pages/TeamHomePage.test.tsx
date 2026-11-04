import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { FINANCE_MEMBER, type Handler, fakeApi, jsonResponse as json } from "../testUtils";
import type { Issue, Overview, Summary, SummaryPage, TeamNumbers } from "../types/api";

const OVERVIEW: Overview = {
  open: 5,
  in_progress: 2,
  overdue: 3,
  resolved_this_week: 4,
  triage: null,
  visits_this_week: null,
  names_to_review: null,
};

function issue(n: number, more: Partial<Issue> = {}): Issue {
  return {
    id: `i-${n}`,
    team_key: "finance",
    category: "payments",
    title_en: `Issue ${n}`,
    severity: "low",
    status: "open",
    owner_id: null,
    owner_name: null,
    due_at: "2026-11-09T18:29:00Z",
    overdue: false,
    first_seen_at: "2026-11-01T06:00:00Z",
    last_seen_at: "2026-11-03T06:00:00Z",
    resolved_at: null,
    resolution_note: null,
    reopened_count: 0,
    shops: 1,
    insights: 1,
    reps: 1,
    raised_again: false,
    version: 1,
    ...more,
  };
}

const ACTIVE = [
  issue(1, {
    title_en: "Credit notes come late",
    severity: "high",
    status: "in_progress",
    overdue: true,
    owner_id: "u-priya",
    owner_name: "Priya Sharma",
    shops: 4,
    insights: 6,
    raised_again: true,
  }),
  issue(2, { title_en: "Scheme money not paid", severity: "medium" }),
];

function resolvedIssue(n: number, resolvedAt: string, note: string): Issue {
  return issue(n, { status: "resolved", resolved_at: resolvedAt, resolution_note: note });
}

// Not in the order they were resolved: the page sorts them, the latest first.
const RESOLVED = [
  resolvedIssue(3, "2026-10-30T06:00:00Z", "The invoice is corrected."),
  resolvedIssue(4, "2026-11-03T06:00:00Z", "The credit is on your next bill."),
  resolvedIssue(5, "2026-11-01T06:00:00Z", "Paid in full."),
];

const WEEK = { start: "2026-10-25T18:30:00Z", end: "2026-11-01T18:30:00Z" };

const NUMBERS: TeamNumbers = {
  team_id: "t-finance",
  period: { kind: "week", ...WEEK },
  issues: [],
  totals: { insights: 3, shops: 2, reps: 2, visits: 3, triage: 0 },
};

const READY: Summary = {
  id: "s-1",
  scope: "team",
  team_id: "t-finance",
  period_start: WEEK.start,
  period_end: WEEK.end,
  status: "ready",
  numbers: NUMBERS,
  counted_at: "2026-11-02T04:30:00Z",
  narrative: null,
  model: null,
  failure_reason: null,
};

function summaryPage(summary: Summary | null): SummaryPage {
  return {
    period: "week",
    period_start: WEEK.start,
    period_end: WEEK.end,
    numbers: NUMBERS,
    numbers_as_of: "2026-11-02T04:30:00Z",
    summary,
  };
}

const SUMMARY = "GET /api/summaries?scope=team&period=week&team_id=t-finance";

function open(routesByCall: Record<string, Handler | Handler[]> = {}) {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, FINANCE_MEMBER),
    "GET /api/me/overview": () => json(200, OVERVIEW),
    "GET /api/issues": () => json(200, ACTIVE),
    "GET /api/issues?status=resolved": () => json(200, RESOLVED),
    [SUMMARY]: () => json(200, summaryPage(READY)),
    ...routesByCall,
  });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/"] })} />);
  return api;
}

/** The stat cards, once the page is there. */
async function numbers() {
  return within(await screen.findByRole("list", { name: "Your numbers" }));
}

/** A section's rows, once they're there. */
async function rowsOf(name: string): Promise<HTMLElement[]> {
  const section = await screen.findByRole("region", { name });
  return within(section).findAllByRole("listitem");
}

/** Each row's title, as the fixtures name them: "Issue 5". */
function titles(rows: HTMLElement[]): Array<string | undefined> {
  return rows.map((row) => row.textContent?.split(" ").slice(0, 2).join(" "));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("TeamHomePage", () => {
  it("greets the team member, names their team, and offers one Open the inbox", async () => {
    const api = open();

    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Finance Member · Demo Org" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByText("Your Finance team's issues at a glance.")).toBeInTheDocument();
    const inbox = screen.getAllByRole("link", { name: "Open the inbox" });
    expect(inbox).toHaveLength(1);
    expect(inbox[0]).toHaveAttribute("href", "/inbox");
    expect(screen.getByText("Assign, resolve and answer what shops raised.")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent)).toEqual([
      "Needs action",
      "Recently resolved",
      "Your team's latest summary",
    ]);
    await vi.waitFor(() => expect(api.gets()).toBe(4));
  });

  it("says each of the team's numbers in plain words, each a link to where it goes", async () => {
    open();
    const cards = await numbers();

    expect(await cards.findByRole("link", { name: "5 open issues waiting for your team" })).toHaveAttribute(
      "href",
      "/inbox",
    );
    expect(cards.getByRole("link", { name: "2 issues your team is working on" })).toHaveAttribute("href", "/inbox");
    const overdue = cards.getByRole("link", { name: "3 issues past their due date" });
    expect(overdue).toHaveAttribute("href", "/inbox");
    expect(overdue).toHaveClass("bg-vermilion-soft");
    expect(cards.getByRole("link", { name: "4 issues resolved this week" })).toHaveAttribute(
      "href",
      "/inbox?status=resolved",
    );
  });

  it("words one of each", async () => {
    open({
      "GET /api/me/overview": () => json(200, { ...OVERVIEW, open: 1, in_progress: 1, overdue: 1, resolved_this_week: 1 }),
    });
    const cards = await numbers();

    expect(await cards.findByRole("link", { name: "1 open issue waiting for your team" })).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "1 issue your team is working on" })).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "1 issue past its due date" })).toHaveClass("bg-vermilion-soft");
    expect(cards.getByRole("link", { name: "1 issue resolved this week" })).toBeInTheDocument();
  });

  it("says when nothing is open, worked on, overdue or resolved", async () => {
    open({
      "GET /api/me/overview": () => json(200, { ...OVERVIEW, open: 0, in_progress: 0, overdue: 0, resolved_this_week: 0 }),
    });
    const cards = await numbers();

    expect(await cards.findByRole("link", { name: "No open issues" })).toHaveAttribute("href", "/inbox");
    expect(cards.getByRole("link", { name: "No issues being worked on" })).toHaveAttribute("href", "/inbox");
    const overdue = cards.getByRole("link", { name: "Nothing overdue" });
    expect(overdue).toHaveAttribute("href", "/inbox");
    expect(overdue).not.toHaveClass("bg-vermilion-soft");
    expect(cards.getByRole("link", { name: "Nothing resolved yet this week" })).toHaveAttribute(
      "href",
      "/inbox?status=resolved",
    );
  });

  it("shows a dash, never a number, while the numbers load", async () => {
    open({ "GET /api/me/overview": () => new Promise<Response>(() => {}) });
    const cards = await numbers();

    const shown = cards.getAllByRole("link").map((link) => link.textContent ?? "");
    expect(shown.map((text) => text.startsWith("–") && !/\d/.test(text))).toEqual([true, true, true, true]);
    expect(cards.getByRole("link", { name: "Open issues waiting for your team" })).toBeInTheDocument();
    expect(cards.getByRole("link", { name: "Issues past their due date" })).not.toHaveClass("bg-vermilion-soft");
  });

  it("lists what needs action with its severity, status, counts and owner, each a link to the inbox", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Needs action" });
    const links = await within(section).findAllByRole("link");
    expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      [
        "Credit notes come late High severity In progress Overdue Raised again 4 shops · 6 findings · Owner: Priya Sharma",
        "/inbox",
      ],
      ["Scheme money not paid Medium severity Open 1 shop · 1 finding · No owner yet", "/inbox"],
    ]);
    expect(
      within(section).getByRole("link", {
        name: "Scheme money not paid Medium severity Open 1 shop · 1 finding · No owner yet",
      }),
    ).toBeInTheDocument();
    expect(within(section).queryByRole("status")).not.toBeInTheDocument();
  });

  it("shows 3 that need action, in the inbox's order, then View all (N) opens the rest", async () => {
    const many = [5, 2, 4, 1, 3].map((n) => issue(n));
    open({ "GET /api/issues": () => json(200, many) });

    expect(titles(await rowsOf("Needs action"))).toEqual(["Issue 5", "Issue 2", "Issue 4"]);
    const section = screen.getByRole("region", { name: "Needs action" });
    const more = within(section).getByRole("button", { name: "View all (5)" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(more);

    expect(titles(within(section).getAllByRole("listitem"))).toEqual(["Issue 5", "Issue 2", "Issue 4", "Issue 1", "Issue 3"]);
    expect(within(section).getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
  });

  it("lists the resolved issues, the latest first, each with the day and what the shop was told", async () => {
    open();

    const rows = await rowsOf("Recently resolved");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Issue 4 resolved 3 Nov 2026 Told the shop: The credit is on your next bill.",
      "Issue 5 resolved 1 Nov 2026 Told the shop: Paid in full.",
      "Issue 3 resolved 30 Oct 2026 Told the shop: The invoice is corrected.",
    ]);
    const section = screen.getByRole("region", { name: "Recently resolved" });
    expect(within(section).getAllByRole("link").map((link) => link.getAttribute("href"))).toEqual([
      "/inbox?status=resolved",
      "/inbox?status=resolved",
      "/inbox?status=resolved",
    ]);
    expect(screen.queryByRole("button", { name: /^View all/ })).not.toBeInTheDocument();
  });

  it("shows the 3 latest resolved, then View all (N) opens the older ones", async () => {
    const older = resolvedIssue(6, "2026-10-20T06:00:00Z", "Sorted.");
    open({ "GET /api/issues?status=resolved": () => json(200, [older, ...RESOLVED]) });

    expect(titles(await rowsOf("Recently resolved"))).toEqual(["Issue 4", "Issue 5", "Issue 3"]);
    const section = screen.getByRole("region", { name: "Recently resolved" });
    await userEvent.click(within(section).getByRole("button", { name: "View all (4)" }));

    const all = within(section).getAllByRole("listitem");
    expect(all).toHaveLength(4);
    expect(all[3]).toHaveTextContent("Issue 6 resolved 20 Oct 2026 Told the shop: Sorted.");
  });

  it("names last week's summary, ready to read, and links to it", async () => {
    open();

    const section = await screen.findByRole("region", { name: "Your team's latest summary" });
    expect(await within(section).findByRole("heading", { level: 3, name: "Last week's summary" })).toBeInTheDocument();
    expect(within(section).getByText("26 Oct – 1 Nov 2026")).toBeInTheDocument();
    expect(within(section).getByText("Ready to read")).toBeInTheDocument();
    expect(within(section).getByText("3 findings from 2 shops last week.")).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Read it →" })).toHaveAttribute("href", "/summaries");
  });

  it.each([
    ["writing", "Being written…", "Read it →"],
    ["failed", "Couldn't be written", "Try again on Summaries →"],
  ] as const)("says a %s summary is %s", async (status, label, link) => {
    open({ [SUMMARY]: () => json(200, summaryPage({ ...READY, status, numbers: null, counted_at: null })) });

    const section = await screen.findByRole("region", { name: "Your team's latest summary" });
    expect(await within(section).findByText(label)).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: link })).toHaveAttribute("href", "/summaries");
  });

  it("explains the empty lists, and a summary nobody has asked for yet", async () => {
    open({
      "GET /api/issues": () => json(200, []),
      "GET /api/issues?status=resolved": () => json(200, []),
      [SUMMARY]: () => json(200, summaryPage(null)),
    });

    const action = await screen.findByRole("region", { name: "Needs action" });
    expect(await within(action).findByText("Nothing needs action: your team is all caught up.")).toBeInTheDocument();
    expect(within(action).queryByRole("link")).not.toBeInTheDocument();
    const resolved = screen.getByRole("region", { name: "Recently resolved" });
    await vi.waitFor(() => expect(resolved).toHaveTextContent("Nothing resolved yet."));
    expect(within(resolved).queryByRole("link")).not.toBeInTheDocument();
    const summary = screen.getByRole("region", { name: "Your team's latest summary" });
    expect(
      await within(summary).findByText(
        "No summary of last week (26 Oct – 1 Nov 2026) yet. On Summaries, Write the summary turns the week's findings into a short read for your team.",
      ),
    ).toBeInTheDocument();
    expect(within(summary).getByRole("link", { name: "Go to Summaries →" })).toHaveAttribute("href", "/summaries");
    expect(within(summary).queryByRole("heading", { level: 3 })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^View all/ })).not.toBeInTheDocument();
  });

  it("says Loading… until the issues come, then offers Try again when they couldn't be loaded", async () => {
    let fail: (response: Response) => void = () => {};
    open({
      "GET /api/issues": [
        () => new Promise<Response>((resolve) => (fail = resolve)),
        () => json(200, ACTIVE),
      ],
    });

    const section = await screen.findByRole("region", { name: "Needs action" });
    expect(within(section).getByRole("status")).toHaveTextContent("Loading…");
    fail(json(404, { error: { code: "not_found", message: "Not found." } }));

    const alert = await within(section).findByRole("alert");
    expect(alert).toHaveTextContent("The issues couldn't be loaded: Not found.");
    expect(within(section).queryByRole("status")).not.toBeInTheDocument();
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    expect(await within(section).findByRole("link", { name: /^Credit notes come late / })).toHaveAttribute(
      "href",
      "/inbox",
    );
    expect(within(section).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("offers Try again for each other load that failed", async () => {
    const failed = () => json(404, { error: { code: "not_found", message: "Not found." } });
    open({ "GET /api/me/overview": failed, "GET /api/issues?status=resolved": failed, [SUMMARY]: failed });

    await vi.waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(3));
    expect(screen.getAllByRole("alert").map((alert) => alert.textContent)).toEqual([
      "Your team's numbers couldn't be loaded: Not found. Try again",
      "The resolved issues couldn't be loaded: Not found. Try again",
      "The summary couldn't be loaded: Not found. Try again",
    ]);
  });
});
