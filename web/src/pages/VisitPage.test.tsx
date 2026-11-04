import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { REP, jsonResponse as json } from "../testUtils";
import type { Review, Transcript, Visit } from "../types/api";

const VISIT: Visit = {
  id: "v1",
  shop_id: "s1",
  rep_id: "r1",
  kind: "conversation",
  client: "upload",
  status: "review",
  failure_reason: null,
  duration_ms: null,
  processing: false,
  completed_at: null,
  audio_deleted_at: null,
  created_at: "2026-11-01T06:00:00Z",
  updated_at: "2026-11-01T06:00:00Z",
};

const REVIEW: Review = {
  visit: VISIT,
  report: { summary_en: "A routine order.", next_visit_date: null },
  tasks: [],
  order_items: [],
  insights: [],
  notes: [],
  speaker_roles: {},
  kinds: [{ key: "problem", label: "Problem" }],
  categories: [{ key: "payments", label: "Credit and payments" }],
  severities: [{ key: "low", label: "Low" }],
  teams: [],
};

const ANSWERS: Record<string, unknown> = {
  "/api/visits/v1": VISIT,
  "/api/visits/v1/review": REVIEW,
  "/api/visits": { items: [] },
  "/api/shops": { items: [] },
  "/api/auth/me": { ...REP, id: "r1" }, // the visit's rep: they review it
};

/** The visit page with the list behind it in history, as when the rep opened it from there. */
function open(answers: Record<string, unknown> = ANSWERS) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => (url in answers ? json(200, answers[url]) : new Promise<Response>(() => {}))),
  );
  const router = createMemoryRouter(routes, { initialEntries: ["/", "/visits/v1"], initialIndex: 1 });
  render(<RouterProvider router={router} />);
  return router;
}

async function openWithUnsavedNote() {
  const router = open();
  await userEvent.type(await screen.findByLabelText("Add a note"), "Half a thought");
  return router;
}

const TRANSCRIPT: Transcript = {
  id: "t1",
  visit_id: "v1",
  provider: "meta",
  model: "test",
  language_code: "hi,en",
  created_at: "2026-11-01T06:00:00Z",
  turns: [{ turn_id: 0, speaker_label: "c0:B", start_ms: 4_000, end_ms: 6_000, text: "Payment abhi tak nahi aaya" }],
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("VisitPage", () => {
  it("asks before the back button leaves unsaved changes, and stays if the rep says no", async () => {
    const asked = vi.spyOn(window, "confirm").mockReturnValue(false);
    const router = await openWithUnsavedNote();

    await act(() => router.navigate(-1));

    expect(asked).toHaveBeenCalledWith("Leave without saving your changes?");
    expect(router.state.location.pathname).toBe("/visits/v1");
    expect(screen.getByLabelText("Add a note")).toHaveValue("Half a thought");
  });

  it("leaves by the Visits link once the rep agrees", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const router = await openWithUnsavedNote();

    await userEvent.click(screen.getByRole("link", { name: "Visits" }));

    // A rep's own home (batch F2).
    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Demo Rep · Demo Org" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });

  it("says so when the recording can't be played, and the review still works", async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    const load = vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    open({ ...ANSWERS, "/api/visits/v1": { ...VISIT, duration_ms: 60_000 }, "/api/visits/v1/transcript": TRANSCRIPT });
    const line = await screen.findByRole("button", { name: /Payment abhi tak nahi aaya/ });
    const player = document.querySelector("audio") as HTMLAudioElement;
    // As a browser does: the element holds its error, and loading again clears it.
    const failing = (code: number | null) =>
      Object.defineProperty(player, "error", { configurable: true, value: code === null ? null : { code } });

    // Its file is gone: the player's request fails.
    failing(4);
    fireEvent.error(player);

    const message = "The recording can't be played right now. Everything else on this page still works.";
    expect(screen.getByRole("alert")).toHaveTextContent(message);
    await userEvent.click(line);
    expect(play).not.toHaveBeenCalled();
    // ▶ moves to the message's Try again, so it's read out again.
    expect(within(screen.getByRole("alert")).getByRole("button", { name: "Try again" })).toHaveFocus();
    await userEvent.type(await screen.findByLabelText("Add a note"), "Still works");
    expect(screen.getByLabelText("Add a note")).toHaveValue("Still works");

    failing(null);
    await userEvent.click(within(screen.getByRole("alert")).getByRole("button", { name: "Try again" }));

    expect(load).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    // Still gone: the message comes back.
    failing(4);
    fireEvent.error(player);
    expect(screen.getByRole("alert")).toHaveTextContent(message);
  });

  it("shows the review read-only to anyone but the visit's rep", async () => {
    open({ ...ANSWERS, "/api/auth/me": { ...REP, id: "someone-else", role: "manager" } });

    expect(await screen.findByRole("heading", { level: 2, name: "Findings" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Add a note")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirm/ })).not.toBeInTheDocument();
  });
});
