import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { FINANCE_MEMBER, type Handler, fakeApi, jsonResponse as json } from "../testUtils";
import type { InboxOptions, Measures, OrgNumbers, Summary, SummaryPage, TeamNumbers } from "../types/api";

const OPTIONS: InboxOptions = {
  teams: [
    { id: "t-finance", key: "finance", name: "Finance" },
    { id: "t-area", key: "area_manager", name: "Area manager" },
  ],
  categories: [],
  areas: [],
  distributors: [],
};

const WEEK = { start: "2026-10-25T18:30:00Z", end: "2026-11-01T18:30:00Z" };

const MEASURES: Measures = {
  severity: "high",
  shops: 3,
  insights: 3,
  reps: 2,
  visits: 3,
  raised_again: 1,
  open_shops: 4,
  age_weeks: 1,
  overdue: false,
  baseline: 0.5,
  seen_periods: 1,
  label: "growing",
  score: 24,
  last_seen_at: "2026-10-30T05:30:00Z",
};

const TITLE = "Credit and payments · Station Road Distributors (example)";

const TEAM: TeamNumbers = {
  team_id: "t-finance",
  period: { kind: "week", ...WEEK },
  issues: [{ issue_id: "issue-1", title_en: TITLE, category: "payments", status: "open", measures: MEASURES }],
  totals: { insights: 3, shops: 3, reps: 2, visits: 3, triage: 0 },
};

const PAGE: SummaryPage = {
  period: "week",
  period_start: WEEK.start,
  period_end: WEEK.end,
  numbers: TEAM,
  numbers_as_of: "2026-11-02T04:30:00Z",
  summary: null,
};

const WRITING: Summary = {
  id: "s-1",
  scope: "team",
  team_id: "t-finance",
  period_start: WEEK.start,
  period_end: WEEK.end,
  status: "writing",
  numbers: null,
  counted_at: null,
  narrative: null,
  model: null,
  failure_reason: null,
};

const READY: Summary = {
  ...WRITING,
  status: "ready",
  // Its own numbers: one more insight than when the page first counted.
  numbers: { ...TEAM, totals: { ...TEAM.totals, insights: 4 } },
  counted_at: "2026-11-02T05:00:00Z",
  narrative: {
    overview: [{ text: "Credit notes grew to 3 shops.", cites: [{ insight_id: "i-1", visit_id: "v-1" }] }],
    issues: [
      {
        id: "issue-1",
        sentences: [
          {
            text: "3 shops are waiting on a credit note.",
            cites: [
              { insight_id: "i-1", visit_id: "v-1" },
              { insight_id: "i-2", visit_id: "v-2" },
            ],
          },
        ],
      },
    ],
    kept: 2,
    dropped: { extra: 0, uncited: 0, citation: 0, number: 0 },
  },
  model: "muse-spark",
};

const FAILED: Summary = {
  ...WRITING,
  status: "failed",
  failure_reason: "The summary's words need Muse Spark, which isn't set up on the server (MODEL_API_KEY)",
};

const FINANCE_WEEK = "GET /api/summaries?scope=team&period=week&team_id=t-finance";

function open(path: string, routesByCall: Record<string, Handler | Handler[]>) {
  const api = fakeApi({ "GET /api/issues/options": () => json(200, OPTIONS), ...routesByCall });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return { api, router };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("SummariesPage", () => {
  it("shows the team's numbers at once, ranked, with why each is there", async () => {
    open("/summaries", { [FINANCE_WEEK]: () => json(200, PAGE) });

    const [issue] = within(await screen.findByRole("list", { name: "Issues, ranked" })).getAllByRole("listitem");

    expect(screen.getByRole("heading", { level: 2, name: "Last week: 26 Oct – 1 Nov 2026" })).toBeInTheDocument();
    expect(screen.getByText("Numbers as of 2 Nov 2026, 10:00 am")).toBeInTheDocument();
    expect(screen.getByText("3 insights · 3 shops · 2 reps · 3 visits")).toBeInTheDocument();
    expect(issue).toHaveTextContent(TITLE);
    expect(issue).toHaveTextContent("Growinghigh · Open");
    expect(issue).toHaveTextContent("3 shops · 3 insights · 2 reps in the period; usually 0.5 shops");
    expect(issue).toHaveTextContent("Why it's here: high severity, 4 open shops, 1 week old (score 24)");
    expect(screen.getByRole("button", { name: "Write the summary" })).toBeInTheDocument();
  });

  it("asks once, polls the summary by its ID, then shows its cited words and its own numbers", async () => {
    const { api } = open("/summaries", {
      [FINANCE_WEEK]: () => json(200, PAGE),
      "POST /api/summaries": () => json(200, WRITING),
      "GET /api/summaries/s-1": () => json(200, READY),
    });

    await userEvent.click(await screen.findByRole("button", { name: "Write the summary" }));

    const overview = await screen.findByRole("region", { name: "Overview" });
    expect(api.sent()).toEqual([
      { route: "POST /api/summaries", body: { scope: "team", team_id: "t-finance", period: "week" } },
    ]);
    expect(overview).toHaveTextContent("Credit notes grew to 3 shops.[1]");
    expect(within(overview).getByRole("link", { name: "Finding 1, on its visit" })).toHaveAttribute(
      "href",
      "/visits/v-1#insight-i-1",
    );
    const issue = within(screen.getByRole("list", { name: "Issues, ranked" })).getByRole("listitem");
    expect(issue).toHaveTextContent("3 shops are waiting on a credit note.[1][2]");
    expect(within(issue).getByRole("link", { name: "Finding 2, on its visit" })).toHaveAttribute(
      "href",
      "/visits/v-2#insight-i-2",
    );
    expect(screen.getByText("Numbers as of 2 Nov 2026, 10:30 am")).toBeInTheDocument();
    expect(screen.getByText("4 insights · 3 shops · 2 reps · 3 visits")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Write the summary" })).not.toBeInTheDocument();
    expect(api.gets()).toBe(3);
  });

  it("says while it's being written, with the numbers counted now", async () => {
    open("/summaries", {
      [FINANCE_WEEK]: () => json(200, { ...PAGE, summary: WRITING }),
      "GET /api/summaries/s-1": () => json(200, WRITING),
    });

    expect(await screen.findByText("Writing the summary. The numbers below are counted now.")).toHaveRole("status");
    expect(screen.getByText("Numbers as of 2 Nov 2026, 10:00 am")).toBeInTheDocument();
  });

  it("says why a summary failed, and Try again writes it again", async () => {
    const { api } = open("/summaries", {
      [FINANCE_WEEK]: () => json(200, { ...PAGE, summary: FAILED }),
      "POST /api/summaries/s-1/retry": () => json(200, WRITING),
      "GET /api/summaries/s-1": () => json(200, READY),
    });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`The summary couldn't be written: ${FAILED.failure_reason} Try again`);
    // The numbers still show.
    expect(screen.getByText("3 insights · 3 shops · 2 reps · 3 visits")).toBeInTheDocument();

    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    expect(await screen.findByRole("region", { name: "Overview" })).toHaveTextContent("Credit notes grew");
    expect(api.sent()).toEqual([{ route: "POST /api/summaries/s-1/retry", body: undefined }]);
    // The options, the page once, and the summary once: nothing counted again to learn it.
    expect(api.gets()).toBe(3);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says when the org has no teams to summarise", async () => {
    fakeApi({ "GET /api/issues/options": () => json(200, { ...OPTIONS, teams: [] }) });
    render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/summaries"] })} />);

    expect(await screen.findByText("This org has no teams yet: there's nothing to summarise.")).toBeInTheDocument();
  });

  it("shows the leadership summary: causes across teams, triage, areas and competitors", async () => {
    const org: OrgNumbers = {
      period: { kind: "month", start: "2026-09-30T18:30:00Z", end: "2026-10-31T18:30:00Z" },
      causes: [
        {
          issue_key: "payments|d-1",
          title_en: TITLE,
          category: "payments",
          teams: [
            { team_key: "area_manager", team_name: "Area manager", issue_id: "issue-2", status: "resolved" },
            { team_key: "finance", team_name: "Finance", issue_id: "issue-1", status: "open" },
          ],
          measures: MEASURES,
        },
      ],
      totals: { insights: 5, shops: 4, reps: 2, visits: 5, triage: 1 },
      by_category_area: [{ category: "payments", area: "Station Road", causes: 1, shops: 3 }],
      competitors: [{ competitor: "Haldiram's Bhujia", insights: 2, shops: 2 }],
      issues_at_end: { open: 3, in_progress: 1, overdue: 1 },
      resolved_in_period: 2,
    };
    const { router } = open("/summaries", {
      [FINANCE_WEEK]: () => json(200, PAGE),
      "GET /api/summaries?scope=org&period=month": () =>
        json(200, {
          ...PAGE,
          period: "month",
          period_start: org.period.start,
          period_end: org.period.end,
          numbers: org,
        }),
    });
    await screen.findByRole("list", { name: "Issues, ranked" });

    await userEvent.selectOptions(screen.getByLabelText("Summary"), "org");
    await userEvent.selectOptions(screen.getByLabelText("Period"), "month");

    const cause = within(await screen.findByRole("list", { name: "Causes, ranked" })).getByRole("listitem");
    expect(router.state.location.search).toBe("?scope=org&period=month");
    expect(screen.getByRole("heading", { level: 2, name: "Last month: October 2026" })).toBeInTheDocument();
    expect(cause).toHaveTextContent("high · Area manager: Resolved · Finance: Open");
    expect(screen.getByText("5 insights · 4 shops · 2 reps · 5 visits · 1 waiting in triage")).toBeInTheDocument();
    expect(
      screen.getByText("The teams' issues at the period's end: 3 open, 1 in progress, 1 overdue. Resolved in the period: 2."),
    ).toBeInTheDocument();
    expect(screen.getByText("payments, Station Road: 1 cause, 3 shops")).toBeInTheDocument();
    expect(screen.getByText("Haldiram's Bhujia: 2 insights at 2 shops")).toBeInTheDocument();
    expect(screen.queryByLabelText("Team")).not.toBeInTheDocument();
  });

  it("is a team member's own team's: no org's summary, no team to pick", async () => {
    open("/summaries?scope=org&team=sales_ops", {
      "GET /api/auth/me": () => json(200, FINANCE_MEMBER),
      [FINANCE_WEEK]: () => json(200, PAGE),
    });

    expect(await screen.findByRole("list", { name: "Issues, ranked" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Summary")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Team")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Period")).toBeInTheDocument();
  });
});
