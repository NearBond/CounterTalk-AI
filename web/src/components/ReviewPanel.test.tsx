import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse as json, rejectOnAbort } from "../testUtils";
import type { Insight, Review, Turn } from "../types/api";
import { ReviewPanel } from "./ReviewPanel";

const TURNS: Turn[] = [
  { turn_id: 0, speaker_label: "c0:A", start_ms: 1_000, end_ms: 3_000, text: "Namaste" },
  { turn_id: 1, speaker_label: "c0:B", start_ms: 4_000, end_ms: 6_000, text: "Payment abhi tak nahi aaya" },
];

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
  routes: [{ slot: 1, team_key: "money", team_name: "Money desk", routed_by: "table" }],
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
      created_at: "2026-11-01T06:00:00Z",
      updated_at: "2026-11-01T06:00:00Z",
    },
    report: { summary_en: "A routine order.", next_visit_date: "2026-11-22" },
    tasks: [{ seq: 0, type: "callback", description_en: "Call back.", due: "2026-11-17", owner: "rep", status: "open" }],
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
  };
}

type Handler = (body: unknown) => Response | Promise<Response>;

/** A fake API: answers by "METHOD /path", recording each request's body; one may wait. */
function fakeApi(routes: Record<string, Handler | Handler[]>) {
  const sent: Array<{ route: string; body: unknown }> = [];
  const fake = vi.fn(
    (url: string, init?: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        rejectOnAbort(init, reject);
        const route = `${init?.method ?? "GET"} ${url}`;
        const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
        sent.push({ route, body });
        const found = routes[route];
        const handler = Array.isArray(found) ? found.shift() : found;
        if (handler === undefined) {
          resolve(json(500, { error: { code: "no_fake", message: `nothing for ${route}` } }));
          return;
        }
        void Promise.resolve(handler(body)).then(resolve, reject);
      }),
  );
  vi.stubGlobal("fetch", fake);
  return { sent: () => sent.filter((request) => !request.route.startsWith("GET")) };
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
  it("shows the report, the tasks and each finding with the shopkeeper's words", async () => {
    fakeApi({ "GET /api/visits/v1/review": () => json(200, reviewOf("review")) });

    show();

    expect(await screen.findByText("A routine order.")).toBeInTheDocument();
    expect(screen.getByText("Next visit: 2026-11-22")).toBeInTheDocument();
    expect(screen.getByText("Call back.")).toBeInTheDocument();
    const found = await cardOf("A payout is late.");
    expect(within(found).getByText("Problem · Credit and payments · Low")).toBeInTheDocument();
    expect(within(found).getByText("Payment abhi tak nahi aaya")).toHaveAttribute("lang", "hi");
    expect(within(found).getByText("Product: Kurkure")).toBeInTheDocument();
    expect(within(found).getByText("Sent to Money desk")).toBeInTheDocument();
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
          routes: [{ slot: 1, team_key: "field", team_name: "Field team", routed_by: "table" }],
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
    // The picker names each line's speaker by its role.
    expect(screen.getByRole("option", { name: /0:04 · shopkeeper · Payment abhi tak nahi aaya/ })).toBeInTheDocument();
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
    const api = fakeApi({
      "GET /api/visits/v1/review": [
        () => json(200, reviewOf("review")),
        () => json(200, reviewOf("confirmed", [{ ...PAYMENT, visible: true }, UNFOUND])),
      ],
      "POST /api/visits/v1/confirm": () => json(200, reviewOf("confirmed").visit),
    });
    const { onConfirmed } = show();
    await screen.findByText("A routine order.");

    await userEvent.click(screen.getByRole("button", { name: "Confirm visit" }));

    expect(await screen.findByText(/Confirmed: the findings went to their teams \(1 of 2/)).toBeInTheDocument();
    expect(onConfirmed).toHaveBeenCalledWith(expect.objectContaining({ status: "confirmed" }));
    expect(api.sent()).toEqual([{ route: "POST /api/visits/v1/confirm", body: undefined }]);
    expect(screen.queryByRole("button", { name: /Edit|Delete|Add finding|Add note|Confirm visit/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Add a note")).not.toBeInTheDocument();
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
