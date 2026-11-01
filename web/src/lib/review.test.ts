import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse as json } from "../testUtils";
import type { Insight } from "../types/api";
import { changesOf, confirmReview, draftOf, labelOf } from "./review";

const INSIGHT: Insight = {
  id: "i1",
  seq: 0,
  kind: "problem",
  category: "payments",
  severity: "low",
  summary_en: "A payout is late.",
  verified: true,
  visible: false,
  added_by_rep: false,
  sku: null,
  competitor: null,
  scheme_key: null,
  ai_team: null,
  ai_team_reason: null,
  quotes: [],
  routes: [],
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("changesOf", () => {
  it("sends only what changed, the summary trimmed as the server trims it", () => {
    const draft = { ...draftOf(INSIGHT), summary_en: "  A payout is late.  ", severity: "high" };

    expect(changesOf(INSIGHT, draft)).toEqual({ severity: "high" });
  });

  it("is empty when nothing changed", () => {
    expect(changesOf(INSIGHT, draftOf(INSIGHT))).toEqual({});
  });
});

describe("labelOf", () => {
  it("is the playbook's label, or the key itself", () => {
    const choices = [{ key: "payments", label: "Credit and payments" }];

    expect([labelOf(choices, "payments"), labelOf(choices, "other")]).toEqual(["Credit and payments", "other"]);
  });
});

describe("confirmReview", () => {
  it("is retried through a server that's down for a moment: confirming again is safe", async () => {
    vi.useFakeTimers();
    const answers = [json(503, { error: { code: "x", message: "Down" } }), json(200, { id: "v1", status: "confirmed" })];
    const fake = vi.fn(async () => answers.shift() ?? json(500, {}));
    vi.stubGlobal("fetch", fake);

    const confirmed = confirmReview("v1");
    await vi.advanceTimersByTimeAsync(5_000);

    await expect(confirmed).resolves.toEqual({ id: "v1", status: "confirmed" });
    expect(fake).toHaveBeenCalledTimes(2);
  });

  it("isn't retried when the visit can't be confirmed", async () => {
    const fake = vi.fn(async () => json(409, { error: { code: "visit_not_in_review", message: "Not ready" } }));
    vi.stubGlobal("fetch", fake);

    await expect(confirmReview("v1")).rejects.toMatchObject({ code: "visit_not_in_review" });
    expect(fake).toHaveBeenCalledTimes(1);
  });
});
