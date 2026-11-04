import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeApi, jsonResponse as json } from "../testUtils";
import type { Insight, Review, Route, Turn } from "../types/api";
import { ReviewPanel } from "./ReviewPanel";

const TURNS: Turn[] = [
  { turn_id: 0, speaker_label: "c0:A", start_ms: 1_000, end_ms: 3_000, text: "Namaste" },
  { turn_id: 1, speaker_label: "c0:B", start_ms: 4_000, end_ms: 6_000, text: "Payment abhi tak nahi aaya" },
];

function route(key: string, name: string): Route {
  return {
    slot: 1,
    team_id: `team-${key}`,
    team_key: key,
    team_name: name,
    routed_by: "table",
    version: 1,
    issue_id: null,
    issue_status: null,
    resolution_note: null,
  };
}

const PAYMENT: Insight = {
  id: "i1",
  seq: 0,
  kind: "problem",
  category: "payments",
  severity: "low",
  summary_en: "A payout is late.",
  verified: true,
  visible: false,
  added_by_rep: false,
  sku: "Kurkure",
  competitor: null,
  scheme_key: null,
  ai_team: "money",
  ai_team_reason: "Money owed.",
  quotes: [{ seq: 0, turn_id: 1, speaker_role: "shopkeeper", text: "Payment abhi tak nahi aaya", match: "exact", start_ms: 2_500 }],
  routes: [route("money", "Money desk")],
  suggested_team: null, chosen_by_rep: false, left_by_rep: false, routing_version: 1, locked_team_id: null,
};

const UNFOUND: Insight = {
  ...PAYMENT,
  id: "i2",
  seq: 1,
  summary_en: "Wants a discount.",
  verified: false,
  sku: null,
  quotes: [{ seq: 0, turn_id: 0, speaker_role: "rep", text: "Discount do", match: "none", start_ms: null }],
  routes: [],
};

function reviewOf(status: "review" | "confirmed", insights: Insight[] = [PAYMENT, UNFOUND]): Review {
  return {
    visit: {
      id: "v1",
      shop_id: "s1",
      rep_id: "r1",
      kind: "conversation",
      client: "upload",
      status,
      failure_reason: null,
      duration_ms: 60_000,
      processing: false,
      completed_at: null,
      audio_deleted_at: null,
      created_at: "2026-11-01T06:00:00Z",
      updated_at: "2026-11-01T06:00:00Z",
    },
    report: { summary_en: "A routine order.", next_visit_date: "2026-11-22" },
    tasks: [{ seq: 0, type: "callback", description_en: "Call back.", due: "2026-11-17", owner: "rep", status: "open" }],
    order_items: [
      { seq: 0, name: "कुरकुरे मंच", quantity: 2, unit: "dabbe", sku: "Kurkure Masala Munch" },
      { seq: 1, name: "Zingo", quantity: null, unit: "peti", sku: null },
      { seq: 2, name: "Bingo", quantity: null, unit: null, sku: "Bingo" },
    ],
    insights,
    notes: [],
    speaker_roles: { "c0:A": "rep", "c0:B": "shopkeeper" },
    kinds: [
      { key: "problem", label: "Problem" },
      { key: "praise", label: "Praise" },
    ],
    categories: [
      { key: "payments", label: "Credit and payments" },
      { key: "supply", label: "Supply" },
    ],
    severities: [
      { key: "high", label: "High" },
      { key: "low", label: "Low" },
    ],
    teams: [
      { id: "team-money", key: "money", name: "Money desk" },
      { id: "team-field", key: "field", name: "Field team" },
      { id: "team-area", key: "area", name: "Area manager" },
    ],
  };
}

function show(props: Partial<React.ComponentProps<typeof ReviewPanel>> = {}) {
  const callbacks = { onPlay: vi.fn(), onDirty: vi.fn(), onConfirmed: vi.fn() };
  render(<ReviewPanel visitId="v1" turns={TURNS} lang="hi" {...callbacks} {...props} />);
  return callbacks;
}

const cardOf = async (summary: string) => (await screen.findByText(summary)).closest("li") as HTMLElement;

const BUSY = () => json(503, { error: { code: "unavailable", message: "The server is busy." } });
const UUID = expect.stringMatching(/^[0-9a-f-]{36}$/);

/** Runs a save's backoff waits on a fake clock, then hands the clock back to userEvent. */
async function throughRetries(start: () => unknown) {
  vi.useFakeTimers();
  await act(async () => {
    await start();
    await vi.advanceTimersByTimeAsync(60_000);
  });
  vi.useRealTimers();
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ReviewPanel", () => {
  it("shows the report, the order, the tasks and each finding with the shopkeeper's words", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, reviewOf("review")) });

    show();

    expect(await screen.findByText("A routine order.")).toBeInTheDocument();
    expect(screen.getByText("Next visit: 2026-11-22")).toBeInTheDocument();
    const order = within(screen.getByRole("list", { name: "Order" })).getAllByRole("listitem");
    expect(order.map((item) => item.textContent)).toEqual([
      "कुरकुरे मंच · 2 dabbe · Product: Kurkure Masala Munch",
      "Zingo · peti",
      "Bingo · Product: Bingo",
    ]);
    expect(within(order[0] as HTMLElement).getByText("कुरकुरे मंच")).toHaveAttribute("lang", "hi");
    expect(screen.getByText("Call back.")).toBeInTheDocument();
    const found = await cardOf("A payout is late.");
    expect(within(found).getByText("Problem · Credit and payments · Low")).toBeInTheDocument();
    expect(within(found).getByText("Payment abhi tak nahi aaya")).toHaveAttribute("lang", "hi");
    expect(within(found).getByText("Product: Kurkure")).toBeInTheDocument();
    expect(within(found).getByText("Sent to Money desk")).toBeInTheDocument();
  });

  it("focuses the finding a summary's citation points at, once it's shown", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, reviewOf("confirmed")) });

    show({ citedId: "i1" });

    const cited = await cardOf("A payout is late.");
    expect(cited).toHaveAttribute("id", "insight-i1");
    expect(cited).toHaveFocus();
  });

  it("shows no order section without order items: an older extraction never looked for one", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, { ...reviewOf("review"), order_items: [] }) });

    show();

    expect(await screen.findByText("A routine order.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Order" })).not.toBeInTheDocument();
  });

  it("greys out a finding it couldn't find in the recording: delete only, never sent", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, reviewOf("review")) });

    show();

    const unfound = await cardOf("Wants a discount.");
    expect(within(unfound).getByText("Couldn't find this in the recording")).toBeInTheDocument();
    expect(within(unfound).getByText("If it was said, add it yourself from the transcript.")).toBeInTheDocument();
    expect(within(unfound).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(within(unfound).queryByRole("button", { name: /Play/ })).not.toBeInTheDocument();
    expect(within(unfound).getByRole("button", { name: "Delete" })).toBeInTheDocument();
  });

  it("plays the page's player from the quote's moment", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, reviewOf("review")) });
    const { onPlay } = show();

    await userEvent.click(await screen.findByRole("button", { name: "Play from 0:02" }));

    expect(onPlay).toHaveBeenCalledWith(2_500);
  });

  it("saves a new category and shows the team it moved to", async () => {
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "PATCH /api/insights/i1": () =>
        json(200, {
          ...PAYMENT,
          category: "supply",
          routes: [route("field", "Field team")],
        }),
    });
    show();
    const found = await cardOf("A payout is late.");

    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(within(found).getByLabelText("Category"), "supply");
    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    expect(await within(found).findByText("Sent to Field team")).toBeInTheDocument();
    expect(api.sent()).toEqual([{ route: "PATCH /api/insights/i1", body: { category: "supply" } }]);
  });

  it("saves a new category first, then the teams picked, naming the version that answer gave", async () => {
    const reset = { ...PAYMENT, category: "supply", routes: [route("field", "Field team")], routing_version: 2 };
    const area = { ...route("area", "Area manager"), routed_by: "rep" as const };
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "PATCH /api/insights/i1": () => json(200, reset),
      "PUT /api/insights/i1/teams": () => json(200, { ...reset, routes: [area], chosen_by_rep: true, routing_version: 3 }),
    });
    show();
    const found = await cardOf("A payout is late.");

    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(within(found).getByLabelText("Category"), "supply");
    await userEvent.selectOptions(within(found).getByLabelText("Main team"), "team-area");
    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    expect(await within(found).findByText("Sent to Area manager")).toBeInTheDocument();
    expect(api.sent()).toEqual([
      { route: "PATCH /api/insights/i1", body: { category: "supply" } },
      { route: "PUT /api/insights/i1/teams", body: { team_ids: ["team-area"], routing_version: 2 } },
    ]);
  });

  it("changes a finding's main team and adds a second, naming the routing version it read", async () => {
    const field = { ...route("field", "Field team"), routed_by: "rep" as const };
    const area = { ...route("area", "Area manager"), slot: 2, routed_by: "rep" as const };
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "PUT /api/insights/i1/teams": () =>
        json(200, { ...PAYMENT, routes: [field, area], chosen_by_rep: true, suggested_team: "Money desk", routing_version: 3 }),
    });
    show();
    const found = await cardOf("A payout is late.");

    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    expect(within(found).getByLabelText("Main team")).toHaveValue("team-money"); // the suggestion
    await userEvent.selectOptions(within(found).getByLabelText("Main team"), "team-field");
    await userEvent.selectOptions(within(found).getByLabelText("Second team"), "team-area");
    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    expect(await within(found).findByText("Sent to Field team, Area manager")).toBeInTheDocument();
    expect(api.sent()).toEqual([
      { route: "PUT /api/insights/i1/teams", body: { team_ids: ["team-field", "team-area"], routing_version: 1 } },
    ]);
  });

  it("leaves a finding for the manager: no team, and it says so", async () => {
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "PUT /api/insights/i1/teams": () => json(200, { ...PAYMENT, routes: [], chosen_by_rep: true, left_by_rep: true }),
    });
    show();
    const found = await cardOf("A payout is late.");

    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(within(found).getByLabelText("Main team"), "leave");
    expect(within(found).getByLabelText("Second team")).toBeDisabled();
    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    expect(await within(found).findByText("Left for the manager: it goes to triage")).toBeInTheDocument();
    expect(api.sent()).toEqual([{ route: "PUT /api/insights/i1/teams", body: { team_ids: [], routing_version: 1 } }]);
  });

  it("keeps the category's override team: shown, and never offered to take off", async () => {
    const override = { ...route("area", "Area manager"), slot: 2, routed_by: "override" as const };
    const high = { ...PAYMENT, severity: "high", routes: [route("money", "Money desk"), override], locked_team_id: "team-area" };
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review", [high])),
      "PUT /api/insights/i1/teams": () => json(200, high),
    });
    show();
    const found = await cardOf("A payout is late.");

    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    expect(within(found).getByText(/always gets it, for this category and severity/)).toHaveTextContent("Area manager");
    const also = within(found).getByLabelText("Also send to");
    expect([...also.querySelectorAll("option")].map((option) => option.textContent)).toEqual(["None", "Money desk", "Field team"]);
    await userEvent.selectOptions(also, "team-field");
    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    await vi.waitFor(() =>
      expect(api.sent()).toEqual([
        { route: "PUT /api/insights/i1/teams", body: { team_ids: ["team-field", "team-area"], routing_version: 1 } },
      ]),
    );
  });

  it("says a team choice made on an old read is out of date, and reads the review again", async () => {
    const api = fakeApi({
      "GET /api/visits/v1/review": [() => json(200, reviewOf("review")), () => json(200, reviewOf("review"))],
      "PUT /api/insights/i1/teams": () =>
        json(409, { error: { code: "route_changed", message: "Someone routed it meanwhile: reload it" } }),
    });
    show();
    const found = await cardOf("A payout is late.");

    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(within(found).getByLabelText("Main team"), "team-field");
    await userEvent.click(within(found).getByRole("button", { name: "Save" }));
    expect(await within(found).findByRole("alert")).toHaveTextContent("Its teams changed since this page was read");
    await userEvent.click(within(found).getByRole("button", { name: "Try again" }));

    await vi.waitFor(() => expect(api.gets()).toBe(2)); // the review, read again
    expect(api.sent()).toHaveLength(1); // the choice was sent once, never retried by itself
  });

  it("says a confirmed finding's team was the rep's own choice, with the suggestion", async () => {
    const chosen = { ...PAYMENT, visible: true, routes: [{ ...route("field", "Field team"), routed_by: "rep" as const }] };
    fakeApi({
      "GET /api/visits/v1/review": () =>
        json(200, reviewOf("confirmed", [{ ...chosen, chosen_by_rep: true, suggested_team: "Money desk" }])),
    });
    show();
    const found = await cardOf("A payout is late.");

    expect(within(found).getByText("Suggested: Money desk · chosen by the rep")).toBeInTheDocument();
    expect(within(found).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
  });

  it("locks the form while its save is in flight, retries it, then offers Try again", async () => {
    let fail: (response: Response) => void = () => {};
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "PATCH /api/insights/i1": [
        () => new Promise<Response>((resolve) => (fail = resolve)),
        BUSY,
        BUSY,
        BUSY,
        () => json(200, { ...PAYMENT, summary_en: "Two months late." }),
      ],
    });
    show();
    const found = await cardOf("A payout is late.");
    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    await userEvent.clear(within(found).getByLabelText("Summary"));
    await userEvent.type(within(found).getByLabelText("Summary"), "Two months late.");

    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    expect(within(found).getByRole("button", { name: "Saving…" })).toBeDisabled();
    expect(within(found).getByLabelText("Category")).toBeDisabled();
    await throughRetries(() => fail(BUSY()));
    expect(within(found).getByRole("alert")).toHaveTextContent("The server is busy.");
    expect(api.sent()).toHaveLength(4);

    await userEvent.click(within(found).getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Two months late.")).toBeInTheDocument();
    expect(new Set(api.sent().map((request) => JSON.stringify(request.body)))).toEqual(
      new Set([JSON.stringify({ summary_en: "Two months late." })]),
    );
  });

  it("offers no Try again for an answer that won't change", async () => {
    fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "PATCH /api/insights/i1": () => json(409, { error: { code: "visit_confirmed", message: "It's confirmed." } }),
    });
    show();
    const found = await cardOf("A payout is late.");
    await userEvent.click(within(found).getByRole("button", { name: "Edit" }));
    await userEvent.selectOptions(within(found).getByLabelText("Severity"), "high");

    await userEvent.click(within(found).getByRole("button", { name: "Save" }));

    expect(await within(found).findByRole("alert")).toHaveTextContent("It's confirmed.");
    expect(within(found).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("deletes a finding once the rep says so", async () => {
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "DELETE /api/insights/i2": () => new Response(null, { status: 204 }),
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    show();
    const unfound = await cardOf("Wants a discount.");

    await userEvent.click(within(unfound).getByRole("button", { name: "Delete" }));

    await vi.waitFor(() => expect(screen.queryByText("Wants a discount.")).not.toBeInTheDocument());
    expect(api.sent()).toEqual([{ route: "DELETE /api/insights/i2", body: undefined }]);
  });

  it("adds a finding from the line the rep picks", async () => {
    const added: Insight = { ...PAYMENT, id: "i3", seq: 2, summary_en: "Asked for a stand.", added_by_rep: true };
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "POST /api/visits/v1/insights": () => json(201, added),
    });
    show();
    await screen.findByText("A routine order.");

    await userEvent.selectOptions(screen.getByLabelText("Where the shopkeeper said it"), "1");
    await userEvent.selectOptions(screen.getByLabelText("Kind"), "praise");
    await userEvent.type(screen.getByLabelText("Summary, in English"), "Asked for a stand.");
    await userEvent.click(screen.getByRole("button", { name: "Add finding" }));

    expect(await screen.findByText("Asked for a stand.")).toBeInTheDocument();
    expect(api.sent()).toEqual([
      {
        route: "POST /api/visits/v1/insights",
        body: { id: UUID, turn_id: 1, kind: "praise", category: "payments", severity: "high", summary_en: "Asked for a stand." },
      },
    ]);
    // The shopkeeper's lines only: the rep's "Namaste" isn't offered.
    const picker = screen.getByLabelText("Where the shopkeeper said it");
    expect(within(picker).getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Pick a line of the transcript",
      "0:04 · Payment abhi tak nahi aaya",
    ]);
  });

  it("says so when no line is the shopkeeper's, so there's none to quote", async () => {
    fakeApi({
      "GET /api/visits/v1/review": () =>
        json(200, { ...reviewOf("review"), speaker_roles: { "c0:A": "rep", "c0:B": "other" } }),
    });
    show();
    await screen.findByText("A routine order.");

    expect(screen.getByText(/No line here is marked as the shopkeeper's/)).toBeInTheDocument();
    expect(within(screen.getByLabelText("Where the shopkeeper said it")).getAllByRole("option")).toHaveLength(1);
  });

  it("adds a note, and says notes go to no team", async () => {
    const api = fakeApi({
      "GET /api/visits/v1/review": () => json(200, reviewOf("review")),
      "POST /api/visits/v1/notes": [
        BUSY,
        BUSY,
        BUSY,
        BUSY,
        (body) => json(201, { id: (body as { id: string }).id, text: "Owner away.", created_at: "2026-11-01T07:00:00Z" }),
      ],
    });
    show();
    await screen.findByText("A routine order.");

    expect(screen.getByText("Notes are for this visit's record only. They aren't sent to any team.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Add a note"), "Owner away.");
    await throughRetries(() => screen.getByRole("button", { name: "Add note" }).click());
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Owner away.")).toBeInTheDocument();
    expect(screen.getByLabelText("Add a note")).toHaveValue("");
    // Every try, Try again's too, carries the first one's ID: a note the server kept isn't added twice.
    const bodies = api.sent().map((request) => request.body);
    expect(bodies).toHaveLength(5);
    expect(bodies[0]).toEqual({ id: UUID, text: "Owner away." });
    expect(new Set(bodies.map((body) => JSON.stringify(body))).size).toBe(1);
  });

  it("holds Confirm while there are unsaved changes, and warns before leaving", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, reviewOf("review")) });
    const { onDirty } = show();
    await screen.findByText("A routine order.");

    await userEvent.type(screen.getByLabelText("Add a note"), "Half a thought");

    expect(screen.getByRole("button", { name: "Confirm visit" })).toBeDisabled();
    expect(screen.getByText("Save or cancel your changes first.")).toBeInTheDocument();
    expect(onDirty).toHaveBeenLastCalledWith(true);
    const leaving = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(leaving);
    expect(leaving.defaultPrevented).toBe(true);
  });

  it("confirms, then shows the visit read-only", async () => {
    // One GET only: a reload's failure could hide a confirm that worked.
    const api = fakeApi({
      "GET /api/visits/v1/review": [() => json(200, reviewOf("review"))],
      "POST /api/visits/v1/confirm": () => json(200, reviewOf("confirmed").visit),
    });
    const { onConfirmed } = show();
    await screen.findByText("A routine order.");

    await userEvent.click(screen.getByRole("button", { name: "Confirm visit" }));

    // As confirm did on the server: the one verified finding is shown, the other isn't.
    expect(
      await screen.findByText(
        "Confirmed: 1 of 2 findings, the ones found in the recording, went on to their teams, or to triage where" +
          " no team fits. It can't be changed now.",
      ),
    ).toBeInTheDocument();
    expect(onConfirmed).toHaveBeenCalledWith(expect.objectContaining({ status: "confirmed" }));
    expect(api.sent()).toEqual([{ route: "POST /api/visits/v1/confirm", body: undefined }]);
    expect(api.gets()).toBe(1);
    expect(screen.queryByRole("button", { name: /Edit|Delete|Add finding|Add note|Confirm visit/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add a note")).not.toBeInTheDocument();
  });

  it("shows a confirmed finding's teams with their issue's status and answer, for the shop", async () => {
    const resolved: Route = {
      ...route("money", "Money desk"),
      issue_id: "issue-1",
      issue_status: "resolved",
      resolution_note: "Credit note sent.",
    };
    // Confirmed before issues existed: its team, and no status.
    const before: Route = { ...route("field", "Field team"), slot: 2 };
    fakeApi({
      "GET /api/visits/v1/review": () =>
        json(200, reviewOf("confirmed", [{ ...PAYMENT, visible: true, routes: [resolved, before] }])),
    });

    show();

    const teams = within(await cardOf("A payout is late.")).getByRole("list", { name: "Teams" });
    expect(within(teams).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
      "Money desk · ResolvedAnswer: Credit note sent.",
      "Field team",
    ]);
    expect(screen.queryByText(/^Sent to/)).not.toBeInTheDocument();
  });

  it("offers Try again when the review can't be loaded", async () => {
    fakeApi({
      "GET /api/visits/v1/review": [
        () => json(404, { error: { code: "visit_not_found", message: "There's no such visit" } }),
        () => json(200, reviewOf("review")),
      ],
    });
    show();

    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByText("A routine order.")).toBeInTheDocument();
  });
});
