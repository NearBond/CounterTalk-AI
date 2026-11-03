import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeApi, jsonResponse as json } from "../testUtils";
import { FixedSinceLastVisit } from "./FixedSinceLastVisit";
import { MyCountsLine } from "./MyCountsLine";

const FIXED = {
  team: "Distribution",
  category: "supply",
  problem: "The last two Kurkure deliveries came a week late.",
  note: "Your distributor now delivers every Monday.",
  resolved_at: "2026-11-02T06:30:00Z",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("FixedSinceLastVisit", () => {
  it("shows each fix with its team, its day and the line to say", async () => {
    fakeApi({ "GET /api/shops/s1/fixed": () => json(200, { items: [FIXED], next_cursor: null }) });
    render(<FixedSinceLastVisit shopId="s1" />);

    expect(await screen.findByRole("heading", { name: "Fixed since your last visit" })).toBeInTheDocument();
    const [item] = screen.getAllByRole("listitem");
    expect(item).toHaveTextContent("Distribution · 2 Nov 2026");
    expect(item).toHaveTextContent(FIXED.problem);
    expect(item).toHaveTextContent(`Tell them: ${FIXED.note}`);
  });

  it("shows nothing when nothing was fixed, or the list can't be read", async () => {
    const api = fakeApi({
      "GET /api/shops/s1/fixed": () => json(200, { items: [], next_cursor: null }),
      "GET /api/shops/s2/fixed": () => json(500, { error: { code: "unexpected_error", message: "No." } }),
    });
    const { rerender } = render(<FixedSinceLastVisit shopId="s1" />);
    await vi.waitFor(() => expect(api.gets()).toBe(1));
    rerender(<FixedSinceLastVisit shopId="s2" />);
    await vi.waitFor(() => expect(api.gets()).toBe(2));

    expect(screen.queryByRole("heading")).not.toBeInTheDocument();
  });
});

describe("MyCountsLine", () => {
  it.each([
    [{ raised: 12, fixed: 7 }, "This month you raised 12 problems. 7 are fixed."],
    [{ raised: 1, fixed: 1 }, "This month you raised 1 problem. 1 is fixed."],
  ])("says the rep's own counts: %j", async (counts, line) => {
    fakeApi({ "GET /api/me/counts": () => json(200, counts) });
    render(<MyCountsLine />);

    expect(await screen.findByText(line)).toBeInTheDocument();
  });

  it("is hidden when the rep raised none this month", async () => {
    const api = fakeApi({ "GET /api/me/counts": () => json(200, { raised: 0, fixed: 0 }) });
    const { container } = render(<MyCountsLine />);
    await vi.waitFor(() => expect(api.gets()).toBe(1));

    expect(container).toBeEmptyDOMElement();
  });
});
