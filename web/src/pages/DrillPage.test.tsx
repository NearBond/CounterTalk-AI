import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { fakeApi, jsonResponse as json } from "../testUtils";
import type { Drill, Insight } from "../types/api";

const QUOTED: Insight = {
  id: "i-1",
  seq: 0,
  kind: "problem",
  category: "schemes",
  severity: "high",
  summary_en: "The Diwali payout is still pending.",
  verified: true,
  visible: true,
  added_by_rep: false,
  sku: null,
  competitor: null,
  scheme_key: "kurkure_diwali_payout",
  ai_team: "finance",
  ai_team_reason: null,
  quotes: [{ seq: 0, turn_id: 0, speaker_role: "shopkeeper", text: "Diwali scheme payout pending", match: "exact", start_ms: 1_000 }],
  routes: [],
  suggested_team: null, chosen_by_rep: false, left_by_rep: false, routing_version: 1, locked_team_id: null,
};

const DRILL: Drill = {
  period: "week",
  period_start: "2026-10-25T18:30:00Z",
  period_end: "2026-11-01T18:30:00Z",
  numbers_as_of: "2026-11-02T04:30:00Z",
  issue_id: "issue-1",
  title_en: "Schemes and promotions · Kurkure Diwali payout · Riverbend Traders (example)",
  category: "schemes",
  status: "open",
  shops: 5,
  insights: 6,
  reps: 2,
  raised_again: 1,
  in_period: { insights: 4, shops: 4, reps: 2, visits: 4, triage: 0 },
  by_rep: [
    {
      rep_id: "r-1",
      rep_name: "Rep 1 (example)",
      insights: 3,
      shops: [
        {
          shop_id: "s-a",
          name: "Shop 13 (example)",
          area: "Old Market",
          distributor: "Riverbend Traders (example)",
          visits: [
            { visit_id: "v-1", visited_at: "2026-10-20T05:30:00Z", in_period: false, insights: [QUOTED] },
            { visit_id: "v-2", visited_at: "2026-10-27T05:30:00Z", in_period: true, insights: [{ ...QUOTED, id: "i-2" }] },
          ],
        },
      ],
    },
    { rep_id: "r-2", rep_name: "Rep 2 (example)", insights: 3, shops: [] },
  ],
};

const PATH = "/summaries/drill?team=finance&team_id=t-finance&period=week&start=2026-10-26&issue=issue-1";
const DRILL_GET = "GET /api/summaries/drill?team_id=t-finance&issue_id=issue-1&period=week&start=2026-10-26";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("DrillPage", () => {
  it("shows the issue's reps, their shops and visits, and each quote with ▶, counted now", async () => {
    fakeApi({ [DRILL_GET]: () => json(200, DRILL) });
    render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [PATH] })} />);

    expect(await screen.findByRole("heading", { level: 1, name: DRILL.title_en })).toBeInTheDocument();
    expect(screen.getByText("Last week: 26 Oct – 1 Nov 2026 · Open · counted 2 Nov 2026, 10:00 am")).toBeInTheDocument();
    expect(
      screen.getByText("Since it was last opened: 5 shops · 6 insights · 2 reps · 1 shop raising it again"),
    ).toBeInTheDocument();
    expect(screen.getByText("That week: 4 shops · 4 insights · 2 reps")).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual([
      "Rep 1 (example): 1 shop, 3 insights",
      "Rep 2 (example): 0 shops, 3 insights",
    ]);
    const [shop] = within(screen.getByRole("list", { name: "Rep 1 (example)'s shops" })).getAllByRole("listitem");
    expect(shop).toHaveTextContent("Shop 13 (example), Old Market · Riverbend Traders (example)");
    const visits = within(shop!).getAllByRole("link");
    expect(visits.map((link) => [link.textContent, link.getAttribute("href")])).toEqual([
      ["20 Oct 2026, 11:00 am", "/visits/v-1"],
      ["27 Oct 2026, 11:00 am (that week)", "/visits/v-2"],
    ]);
    expect(within(shop!).getAllByRole("button", { name: /Play/ })).toHaveLength(2);
  });

  it("says when the issue isn't one of the team's in that summary", async () => {
    fakeApi({
      [DRILL_GET]: () =>
        json(404, { error: { code: "issue_not_found", message: "There's no such issue" } }),
    });
    render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [PATH] })} />);

    // Merged or moved since: it won't change, so no Try again.
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The issue couldn't be loaded: There's no such issue");
    expect(within(alert).queryByRole("button")).not.toBeInTheDocument();
  });

  it("says where to start when the address names no issue", async () => {
    const api = fakeApi({});
    render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/summaries/drill"] })} />);

    expect(
      await screen.findByText("Open an issue from a team's summary to see its reps, shops and quotes."),
    ).toBeInTheDocument();
    expect(api.gets()).toBe(0);
  });

  it("is a click from a team summary's issue", async () => {
    const team = {
      team_id: "t-finance",
      period: { kind: "week", start: DRILL.period_start, end: DRILL.period_end },
      issues: [],
      totals: { insights: 0, shops: 0, reps: 0, visits: 0, triage: 0 },
    };
    fakeApi({
      "GET /api/issues/options": () =>
        json(200, { teams: [{ id: "t-finance", key: "finance", name: "Finance" }], categories: [], areas: [], distributors: [] }),
      "GET /api/summaries?scope=team&period=week&team_id=t-finance": () =>
        json(200, {
          period: "week",
          period_start: DRILL.period_start,
          period_end: DRILL.period_end,
          numbers: {
            ...team,
            issues: [
              {
                issue_id: "issue-1",
                title_en: DRILL.title_en,
                category: "schemes",
                status: "open",
                measures: {
                  severity: "high",
                  shops: 4,
                  insights: 4,
                  reps: 2,
                  visits: 4,
                  raised_again: 1,
                  open_shops: 5,
                  age_weeks: 1,
                  overdue: false,
                  baseline: 0.5,
                  seen_periods: 1,
                  label: "growing",
                  score: 30,
                  last_seen_at: "2026-10-30T05:30:00Z",
                },
              },
            ],
          },
          numbers_as_of: DRILL.numbers_as_of,
          summary: null,
        }),
      [DRILL_GET]: () => json(200, DRILL),
    });
    const router = createMemoryRouter(routes, { initialEntries: ["/summaries"] });
    render(<RouterProvider router={router} />);

    await userEvent.click(await screen.findByRole("link", { name: "Its reps, shops and quotes" }));

    expect(await screen.findByRole("heading", { level: 1, name: DRILL.title_en })).toBeInTheDocument();
    // The very period shown, and the way back to this team's summary.
    expect(router.state.location.search).toBe("?team=finance&team_id=t-finance&period=week&start=2026-10-26&issue=issue-1");
    // The page's own way back, not the header's Summaries.
    expect(within(screen.getByRole("main")).getByRole("link", { name: "Summaries" })).toHaveAttribute(
      "href",
      "/summaries?team=finance&period=week",
    );
  });
});
