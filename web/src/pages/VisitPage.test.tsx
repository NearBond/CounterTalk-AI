import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { jsonResponse as json } from "../testUtils";
import type { Review, Visit } from "../types/api";

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
};

const ANSWERS: Record<string, unknown> = {
  "/api/visits/v1": VISIT,
  "/api/visits/v1/review": REVIEW,
  "/api/visits": { items: [] },
  "/api/shops": { items: [] },
};

/** The visit page with the list behind it in history, as when the rep opened it from there. */
async function openWithUnsavedNote() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => (url in ANSWERS ? json(200, ANSWERS[url]) : new Promise<Response>(() => {}))),
  );
  const router = createMemoryRouter(routes, { initialEntries: ["/", "/visits/v1"], initialIndex: 1 });
  render(<RouterProvider router={router} />);
  await userEvent.type(await screen.findByLabelText("Add a note"), "Half a thought");
  return router;
}

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

    expect(await screen.findByRole("heading", { level: 1, name: "Visits" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
  });
});
