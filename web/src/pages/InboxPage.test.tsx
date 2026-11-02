import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { FINANCE_MEMBER, type Handler, fakeApi, jsonResponse as json } from "../testUtils";
import type { InboxOptions, Insight, Issue, IssueDetail, Route, Untriaged } from "../types/api";

const OPTIONS: InboxOptions = {
  teams: [
    { id: "t-finance", key: "finance", name: "Finance" },
    // Before the AI's team, so preselecting the AI's isn't just taking the first.
    { id: "t-area", key: "area_manager", name: "Area manager" },
    { id: "t-sales", key: "sales_ops", name: "Sales operations" },
  ],
  categories: [
    { key: "payments", label: "Credit and payments" },
    { key: "other", label: "Other" },
  ],
  areas: ["Old Market", "Station Road"],
  distributors: [{ id: "d-1", name: "Northgate Agencies (example)" }],
};

const ISSUE: Issue = {
  id: "issue-1",
  team_key: "finance",
  category: "payments",
  title_en: "Credit and payments · Station Road Distributors (example)",
  severity: "high",
  status: "open",
  owner_id: null,
  owner_name: null,
  due_at: "2026-11-17T06:00:00Z",
  overdue: true,
  first_seen_at: "2026-11-15T06:00:00Z",
  last_seen_at: "2026-11-15T06:00:00Z",
  resolved_at: null,
  resolution_note: null,
  reopened_count: 0,
  shops: 2,
  insights: 3,
  reps: 1,
  raised_again: true,
  version: 4,
};

function route(changes: Partial<Route> = {}): Route {
  return {
    slot: 1,
    team_id: "t-finance",
    team_key: "finance",
    team_name: "Finance",
    routed_by: "ai",
    version: 2,
    issue_id: "issue-1",
    issue_status: "open",
    resolution_note: null,
    ...changes,
  };
}

const INSIGHT: Insight = {
  id: "i-1",
  seq: 0,
  kind: "problem",
  category: "payments",
  severity: "high",
  summary_en: "The ₹4,500 credit note hasn't come.",
  verified: true,
  visible: true,
  added_by_rep: false,
  sku: null,
  competitor: null,
  scheme_key: null,
  ai_team: "sales_ops",
  ai_team_reason: "A credit dispute.",
  quotes: [{ seq: 0, turn_id: 4, speaker_role: "shopkeeper", text: "Credit note abhi tak nahi aaya", match: "exact", start_ms: 67_520 }],
  routes: [route()],
};

const DETAIL: IssueDetail = {
  issue: ISSUE,
  suggestions: [],
  members: [
    {
      insight: INSIGHT,
      shop_name: "Corner Daily Needs",
      area: "Station Road",
      rep_name: "Demo Rep",
      visit_id: "v-1",
      visited_at: "2026-11-15T06:00:00Z",
      route: route(),
    },
  ],
};

function open(path: string, routesByCall: Record<string, Handler | Handler[]>) {
  const api = fakeApi({ "GET /api/issues/options": () => json(200, OPTIONS), ...routesByCall });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return { api, router };
}

async function expand() {
  await userEvent.click(await screen.findByRole("button", { name: /Credit and payments · Station Road/ }));
  return screen.findByRole("list", { name: "Its insights" });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("InboxPage", () => {
  it("lists the team's issues as cards, counting shops, with their flags", async () => {
    open("/inbox", { "GET /api/issues?team=finance": () => json(200, [ISSUE]) });

    const card = await screen.findByRole("button", { name: /Credit and payments · Station Road/ });

    expect(screen.getByRole("heading", { level: 1, name: "Inbox" })).toBeInTheDocument();
    expect(card).toHaveTextContent("high · Open · 2 shops · 3 insights · 1 rep");
    expect(within(card).getByText("Raised again")).toBeInTheDocument();
    expect(within(card).getByText("Overdue")).toBeInTheDocument();
    expect(card).toHaveAttribute("aria-expanded", "false");
  });

  it("asks for the team and filters picked, and keeps them in the address", async () => {
    const { router } = open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues?team=sales_ops": () => json(200, []),
      "GET /api/issues?team=sales_ops&area=Old+Market": () => json(200, []),
    });
    await screen.findByRole("button", { name: /Credit and payments/ });

    await userEvent.selectOptions(screen.getByLabelText("Team"), "sales_ops");
    await userEvent.selectOptions(screen.getByLabelText("Area"), "Old Market");

    expect(await screen.findByText("No issues here.")).toBeInTheDocument();
    expect(router.state.location.search).toBe("?team=sales_ops&area=Old+Market");
  });

  it("expands an issue to its insights, with the quote's ▶ and the AI's suggestion", async () => {
    open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues/issue-1": () => json(200, DETAIL),
    });

    const members = await expand();

    const [member] = within(members).getAllByRole("listitem");
    expect(member).toHaveTextContent("Corner Daily Needs (Station Road) · Demo Rep");
    expect(within(member as HTMLElement).getByRole("button", { name: "Play from 1:07" })).toBeInTheDocument();
    expect(within(member as HTMLElement).getByText(/Suggested by AI: A credit dispute\./)).toBeInTheDocument();
    // Move and Also send share one picker, preselected to the AI's team.
    expect(within(member as HTMLElement).getByLabelText("Team")).toHaveValue("t-sales");
    expect(within(member as HTMLElement).getByRole("link", { name: "Open the visit" })).toHaveAttribute(
      "href",
      "/visits/v-1",
    );
  });

  it("plays a quote from its moment on the first ▶, once that visit's recording has loaded", async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    const seek = vi.spyOn(HTMLMediaElement.prototype, "currentTime", "set").mockImplementation(() => {});
    open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues/issue-1": () => json(200, DETAIL),
    });
    await expand();

    await userEvent.click(screen.getByRole("button", { name: "Play from 1:07" }));
    const player = document.querySelector("audio") as HTMLAudioElement;
    expect(player).toHaveAttribute("src", "/api/visits/v-1/audio");
    expect(play).not.toHaveBeenCalled();
    fireEvent.loadedMetadata(player);

    expect(seek).toHaveBeenCalledWith(67.52);
    expect(play).toHaveBeenCalledTimes(1);
  });

  it("assigns, then resolves with a note, each naming the version it read", async () => {
    const mine: IssueDetail = { ...DETAIL, issue: { ...ISSUE, owner_name: "Demo Rep", version: 5 } };
    const done: IssueDetail = { ...mine, issue: { ...mine.issue, status: "resolved", version: 6 } };
    const { api } = open("/inbox", {
      "GET /api/issues?team=finance": [() => json(200, [ISSUE]), () => json(200, [ISSUE]), () => json(200, [])],
      "GET /api/issues?team=finance&area=Old+Market": () => json(200, []),
      "GET /api/issues/issue-1": [() => json(200, DETAIL), () => json(200, mine)],
      "PATCH /api/issues/issue-1": [() => json(200, mine), () => json(200, done)],
    });
    await expand();

    await userEvent.click(screen.getByRole("button", { name: "Assign to me" }));
    expect(await screen.findByText(/Owner: Demo Rep/)).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Its status"), "resolved");
    expect(screen.getByRole("button", { name: "Save status" })).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/How it was resolved/), "Credit note sent.");
    await userEvent.click(screen.getByRole("button", { name: "Save status" }));

    await vi.waitFor(() => expect(api.sent()).toHaveLength(2));
    expect(api.sent().map((request) => request.body)).toEqual([
      { version: 4, assign_to_me: true },
      { version: 5, status: "resolved", note: "Credit note sent." },
    ]);
    // It leaves the open list at once, and the page says where it went, with focus there.
    const notice = await screen.findByText(
      "Resolved: Credit and payments · Station Road Distributors (example). It's under Status: Resolved now.",
    );
    expect(notice).toHaveFocus();
    expect(screen.getByText("No issues here.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Credit and payments/ })).not.toBeInTheDocument();
    // Another view: the notice goes.
    await userEvent.selectOptions(screen.getByLabelText("Area"), "Old Market");
    expect(screen.queryByText(/^Resolved:/)).not.toBeInTheDocument();
  });

  it("shows a merge suggestion; Merge names both versions, opens the kept issue and says so", async () => {
    const other = { id: "issue-2", title_en: "Credit and payments · Northgate Agencies (example)", version: 7 };
    const suggestion = {
      id: "s-1",
      into_issue_id: "issue-1",
      merge_issue_id: "issue-2",
      reason: "Both are the same unpaid credit note.",
      other,
    };
    const kept: IssueDetail = { ...DETAIL, issue: { ...ISSUE, shops: 3, version: 5 } };
    const northgate: Issue = { ...ISSUE, id: "issue-2", title_en: other.title_en, version: 7 };
    const { api } = open("/inbox", {
      // The list read again still has the merged one: it's hidden at once all the same.
      "GET /api/issues?team=finance": [() => json(200, [ISSUE, northgate]), () => json(200, [kept.issue, northgate])],
      "GET /api/issues/issue-1": [() => json(200, { ...DETAIL, suggestions: [suggestion] }), () => json(200, kept)],
      "POST /api/issues/suggestions/s-1/merge": () => json(200, kept),
    });
    await userEvent.click(await screen.findByRole("button", { name: /Credit and payments · Station Road/ }));

    const offered = within(await screen.findByRole("list", { name: "Merge suggestions" })).getByRole("listitem");
    expect(offered).toHaveTextContent("These look like the same issue: Both are the same unpaid credit note.");
    expect(offered).toHaveTextContent(
      "Merge moves the findings of “Credit and payments · Northgate Agencies (example)” into “Credit and payments · Station Road Distributors (example)”.",
    );
    await userEvent.click(within(offered).getByRole("button", { name: "Merge" }));

    const notice = await screen.findByText(
      "Merged: their findings are together in Credit and payments · Station Road Distributors (example).",
    );
    expect(notice).toHaveFocus();
    expect(api.sent()).toEqual([
      { route: "POST /api/issues/suggestions/s-1/merge", body: { into_version: 4, merge_version: 7 } },
    ]);
    expect(await screen.findByRole("list", { name: "Its insights" })).toBeInTheDocument();
    await vi.waitFor(() =>
      expect(screen.queryByRole("list", { name: "Merge suggestions" })).not.toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /Northgate Agencies/ })).not.toBeInTheDocument();
  });

  it("Dismiss keeps them apart, and a merge on an old read says so", async () => {
    const suggestion = {
      id: "s-1",
      into_issue_id: "issue-2",
      merge_issue_id: "issue-1",
      reason: "Same.",
      other: { id: "issue-2", title_en: "Credit and payments · Northgate Agencies (example)", version: 7 },
    };
    const changed = () => json(409, { error: { code: "issue_changed", message: "Someone changed one of these issues meanwhile: reload" } });
    const { api } = open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues/issue-1": [
        () => json(200, { ...DETAIL, suggestions: [suggestion] }),
        () => json(200, DETAIL),
      ],
      "POST /api/issues/suggestions/s-1/merge": changed,
      "POST /api/issues/suggestions/s-1/dismiss": () => new Response(null, { status: 204 }),
    });
    await expand();
    const offered = within(await screen.findByRole("list", { name: "Merge suggestions" })).getByRole("listitem");

    await userEvent.click(within(offered).getByRole("button", { name: "Merge" }));
    expect(await within(offered).findByRole("alert")).toHaveTextContent(
      "One of them changed meanwhile: nothing was merged.",
    );
    // This issue is the one merged away here: the other's version is the kept one's.
    expect(api.sent()[0]).toEqual({
      route: "POST /api/issues/suggestions/s-1/merge",
      body: { into_version: 7, merge_version: 4 },
    });
    await userEvent.click(within(offered).getByRole("button", { name: "Dismiss" }));

    await vi.waitFor(() => expect(screen.queryByRole("list", { name: "Merge suggestions" })).not.toBeInTheDocument());
    expect(api.sent()[1]).toEqual({ route: "POST /api/issues/suggestions/s-1/dismiss", body: undefined });
  });

  it("a suggestion gone another way says why, and Reload reads the issue again", async () => {
    const suggestion = {
      id: "s-1",
      into_issue_id: "issue-1",
      merge_issue_id: "issue-2",
      reason: "Same.",
      other: { id: "issue-2", title_en: "Credit and payments · Northgate Agencies (example)", version: 7 },
    };
    open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues/issue-1": [() => json(200, { ...DETAIL, suggestions: [suggestion] }), () => json(200, DETAIL)],
      "POST /api/issues/suggestions/s-1/merge": () =>
        json(409, { error: { code: "suggestion_dismissed", message: "It was dismissed: they stay apart" } }),
    });
    await expand();
    const offered = within(await screen.findByRole("list", { name: "Merge suggestions" })).getByRole("listitem");

    await userEvent.click(within(offered).getByRole("button", { name: "Merge" }));
    const alert = await within(offered).findByRole("alert");
    expect(alert).toHaveTextContent("It was dismissed: they stay apart Try again");
    await userEvent.click(within(alert).getByRole("button", { name: "Try again" }));

    await vi.waitFor(() => expect(screen.queryByRole("list", { name: "Merge suggestions" })).not.toBeInTheDocument());
  });

  it("an open card that leaves the list on a refresh says so", async () => {
    open("/inbox", {
      "GET /api/issues?team=finance": [() => json(200, [ISSUE]), () => json(200, [])],
      "GET /api/issues/issue-1": [() => json(200, DETAIL), () => json(200, { ...DETAIL, members: [] })],
      "POST /api/insights/i-1/move": () => json(200, INSIGHT),
    });
    const members = await expand();

    await userEvent.click(within(members).getByRole("button", { name: "Move" }));

    expect(
      await screen.findByText(
        "That issue has left this list: someone resolved it, or its findings went to another team.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("No issues here.")).toBeInTheDocument();
  });

  it("a change made on an old read says so and keeps the old status on screen", async () => {
    const moved: IssueDetail = { ...DETAIL, issue: { ...ISSUE, status: "in_progress", version: 5 } };
    open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues/issue-1": [() => json(200, DETAIL), () => json(200, moved)],
      "PATCH /api/issues/issue-1": () =>
        json(409, { error: { code: "issue_changed", message: "Someone changed this issue meanwhile" } }),
    });
    await expand();

    await userEvent.selectOptions(screen.getByLabelText("Its status"), "in_progress");
    await userEvent.click(screen.getByRole("button", { name: "Save status" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Someone changed it meanwhile: your change wasn't saved.",
    );
    expect(screen.getByRole("button", { name: /Credit and payments/ })).toHaveTextContent("Open");

    // Read again: the alert goes, and the form starts from what's saved now.
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await vi.waitFor(() => expect(screen.getByLabelText("Its status")).toHaveValue("in_progress"));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("accepts the AI's team naming the route it read; at two teams Also send is greyed out", async () => {
    const two: IssueDetail = {
      ...DETAIL,
      members: [{ ...DETAIL.members[0]!, insight: { ...INSIGHT, routes: [route(), route({ slot: 2, team_id: "t-sales" })] } }],
    };
    const { api } = open("/inbox", {
      "GET /api/issues?team=finance": () => json(200, [ISSUE]),
      "GET /api/issues/issue-1": [() => json(200, two), () => json(200, two)],
      "POST /api/insights/i-1/accept": () => json(200, INSIGHT),
    });
    const members = await expand();
    const member = within(members).getAllByRole("listitem")[0] as HTMLElement;

    await userEvent.click(within(member).getByRole("button", { name: "Accept" }));

    await vi.waitFor(() => expect(api.sent()).toEqual([{ route: "POST /api/insights/i-1/accept", body: { slot: 1, version: 2 } }]));
    // At two teams, Also send is greyed out.
    expect(within(member).getByRole("button", { name: "Also send" })).toBeDisabled();
    expect(within(member).getByRole("button", { name: "Move" })).toBeEnabled();
  });
});

describe("TriagePage", () => {
  const WAITING: Untriaged = {
    insight: { ...INSIGHT, id: "i-2", ai_team: null, routes: [], summary_en: "Road work outside the shop." },
    shop_name: "Corner Daily Needs",
    area: "Station Road",
    rep_name: "Demo Rep",
    visit_id: "v-2",
    visited_at: "2026-11-15T06:00:00Z",
  };

  it("gives a finding a team, and says so if someone already had", async () => {
    const { api } = open("/inbox/triage", {
      "GET /api/issues/triage": [() => json(200, [WAITING]), () => json(200, [])],
      "POST /api/insights/i-2/team": () =>
        json(409, { error: { code: "already_routed", message: "Someone gave it a team meanwhile" } }),
    });
    const item = (await screen.findByText("Road work outside the shop.")).closest("li") as HTMLElement;

    await userEvent.selectOptions(within(item).getByLabelText("Team"), "t-sales");
    await userEvent.click(within(item).getByRole("button", { name: "Give it this team" }));

    expect(await within(item).findByRole("alert")).toHaveTextContent("Someone gave it a team meanwhile.");
    expect(api.sent()).toEqual([{ route: "POST /api/insights/i-2/team", body: { team_id: "t-sales" } }]);
    expect(screen.getByRole("heading", { level: 1, name: "Triage" })).toBeInTheDocument();
  });

  it("asks a team member's first list with the address's filters", async () => {
    const { api } = open("/inbox?category=payments", {
      "GET /api/auth/me": () => json(200, FINANCE_MEMBER),
      "GET /api/issues?category=payments": () => json(200, [ISSUE]),
    });

    expect(await screen.findByRole("button", { name: /Credit and payments/ })).toBeInTheDocument();
    expect(api.gets()).toBe(2); // the options, then the filtered list: never an unfiltered one
  });

  it("is a team member's own team's, with no team to pick", async () => {
    const { api } = open("/inbox?team=sales_ops", {
      "GET /api/auth/me": () => json(200, FINANCE_MEMBER),
      "GET /api/issues": () => json(200, [ISSUE]),
    });

    expect(await screen.findByRole("button", { name: /Credit and payments/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Team")).not.toBeInTheDocument();
    expect(api.gets()).toBe(2); // the options, and their own team's issues: no other team asked
  });
});
