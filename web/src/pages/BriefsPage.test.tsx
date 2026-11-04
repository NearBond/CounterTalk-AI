import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import {
  BRIEF,
  BRIEF_ROWS,
  FINANCE_MEMBER,
  type Handler,
  REP,
  fakeApi,
  jsonResponse as json,
  shopBrief,
} from "../testUtils";

const ONE = "GET /api/shops/s1/brief";
const GENERATE = "POST /api/shops/s1/brief";

function open(path: string, routesByCall: Record<string, Handler | Handler[]> = {}) {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, REP),
    "GET /api/briefs": () => json(200, { items: BRIEF_ROWS, next_cursor: null }),
    [ONE]: () => json(200, shopBrief()),
    ...routesByCall,
  });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />);
  return api;
}

/** The brief's card, once it's there. */
async function card() {
  return within(await screen.findByRole("region", { name: "Pre-visit brief" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("BriefsPage", () => {
  it("lists every shop with its brief's state, each opening the shop's brief", async () => {
    open("/briefs");

    const list = await screen.findByRole("list", { name: "Shops" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      "Shop 4, Old MarketGenerated 4 Nov 2026 · Up to date",
      "Shop 3, Old MarketGenerated 3 Nov 2026 · 2 visits since",
      "Shop 2, Old MarketGenerated 2 Nov 2026 · Up to date",
      "Shop 1, Old MarketGenerated 1 Nov 2026 · 1 visit since",
      "Shop 5, Old MarketNo brief yet",
      "Shop 6, Old MarketNot enough visits yet",
    ]);
    expect(within(rows[4] as HTMLElement).getByRole("link")).toHaveAttribute("href", "/briefs/s5");
  });

  it("isn't a team member's page", async () => {
    open("/briefs", { "GET /api/auth/me": () => json(200, FINANCE_MEMBER) });

    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
  });
});

describe("ShopBriefPage", () => {
  it("shows the brief's five parts in order, with when it was written", async () => {
    open("/briefs/s1");

    expect(await screen.findByRole("heading", { level: 1, name: "Sunrise Kirana, Old Market" })).toBeInTheDocument();
    const brief = await card();
    expect(brief.getByText("Generated on 2 Nov 2026, after visit 4.")).toBeInTheDocument();
    expect(brief.getAllByRole("heading", { level: 3 }).map((heading) => heading.textContent)).toEqual([
      "Shop overview",
      "Last visit · 1 Nov 2026",
      "Still open",
      "Fixed since",
      "Worth raising this time",
    ]);
    expect(brief.getByRole("list", { name: "What was raised" })).toHaveTextContent(
      "Payments: The credit note is late.पैसा अभी तक नहीं आया",
    );
    expect(brief.getByRole("list", { name: "What was ordered" }).textContent).toBe("Lay's Classic · 2 boxesKurkure");
    expect(brief.getByRole("list", { name: "Still open" })).toHaveTextContent("FinanceIn progressraised 1 Nov 2026");
    expect(brief.getByText("Tell them: Deliveries are on Mondays now.")).toBeInTheDocument();
    expect(brief.queryByRole("button")).not.toBeInTheDocument(); // up to date: nothing to generate
    expect(screen.getByRole("link", { name: "← All briefs" })).toHaveAttribute("href", "/briefs");
  });

  it("says how many visits came since, and offers Regenerate", async () => {
    open("/briefs/s1", { [ONE]: () => json(200, shopBrief({ state: "out_of_date", since: 2, can_generate: true })) });

    const brief = await card();
    const line = await brief.findByText(/^Generated on 2 Nov 2026, after visit 4\.$/);
    expect(line.textContent).toBe("Generated on 2 Nov 2026, after visit 4. 2 visits since.");
    expect(brief.getByRole("button", { name: "Regenerate" })).toBeEnabled();
  });

  it("offers Generate when there's none yet, and shows the brief it writes", async () => {
    let answer: (response: Response) => void = () => {};
    const api = open("/briefs/s1", {
      [ONE]: () => json(200, shopBrief({ state: "none", can_generate: true, brief: null })),
      [GENERATE]: () => new Promise<Response>((resolve) => (answer = resolve)),
    });

    const brief = await card();
    expect(await brief.findByText(/^No brief yet\. Generate writes one/)).toBeInTheDocument();
    await userEvent.click(brief.getByRole("button", { name: "Generate" }));

    expect(brief.getByRole("button", { name: "Writing the brief…" })).toBeDisabled();
    answer(json(200, shopBrief({ brief: { ...BRIEF, after_visit: 5 } })));
    expect(await brief.findByText("Generated on 2 Nov 2026, after visit 5.")).toBeInTheDocument();
    expect(brief.queryByRole("button")).not.toBeInTheDocument();
    expect(api.sent()).toEqual([{ route: GENERATE, body: undefined }]);
  });

  it("explains a shop with too few visits, with no Generate", async () => {
    open("/briefs/s1", {
      [ONE]: () => json(200, shopBrief({ state: "not_enough_visits", visits: 1, brief: null })),
    });

    const brief = await card();
    expect(
      await brief.findByText("A brief needs at least 2 confirmed visits to this shop. So far: 1 confirmed visit."),
    ).toBeInTheDocument();
    expect(brief.queryByRole("button")).not.toBeInTheDocument();
  });

  it("says why Regenerate was refused, with no Try again, and shows what's there now", async () => {
    const api = open("/briefs/s1", {
      [ONE]: [
        () => json(200, shopBrief({ state: "out_of_date", since: 1, can_generate: true })),
        () => json(200, shopBrief()),
      ],
      [GENERATE]: () =>
        json(409, { error: { code: "brief_up_to_date", message: "The brief already covers every confirmed visit." } }),
    });

    const brief = await card();
    await userEvent.click(await brief.findByRole("button", { name: "Regenerate" }));

    const alert = await brief.findByRole("alert");
    expect(alert).toHaveTextContent("The brief couldn't be written: The brief already covers every confirmed visit.");
    expect(within(alert).queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
    await vi.waitFor(() => expect(brief.queryByRole("button", { name: "Regenerate" })).not.toBeInTheDocument());
    expect(brief.getByText("Generated on 2 Nov 2026, after visit 4.")).toBeInTheDocument();
    expect(api.gets()).toBe(2);
  });
});
