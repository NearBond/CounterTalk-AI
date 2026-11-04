import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { REP, fakeApi, jsonResponse as json, shopBrief } from "../testUtils";
import type { Shop } from "../types/api";

const SHOPS: Shop[] = [
  { id: "s-yes", name: "Agreed Kirana", area: "Old Market", do_not_record: false, consent: "yes" },
  { id: "s-no", name: "Refused Kirana", area: "Old Market", do_not_record: false, consent: "no" },
  { id: "s-new", name: "New Kirana", area: "Old Market", do_not_record: false, consent: null },
];

function open() {
  // Each shop's pre-visit brief (batch F5): the picked one's is shown.
  const briefs = Object.fromEntries(
    SHOPS.map((shop) => [
      `GET /api/shops/${shop.id}/brief`,
      () => json(200, shopBrief({ shop_id: shop.id, name: shop.name })),
    ]),
  );
  fakeApi({ "GET /api/auth/me": () => json(200, REP), "GET /api/shops": () => json(200, { items: SHOPS }), ...briefs });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/record"] })} />);
}

async function pick(name: string) {
  const option = await screen.findByRole("option", { name }); // once the shops are in
  await userEvent.selectOptions(screen.getByLabelText("Shop"), option);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RecordPage's consent (batch 17)", () => {
  it("lists each shop's consent: yes, no or not yet", async () => {
    open();

    await screen.findByRole("option", { name: "Agreed Kirana, Old Market · consent: yes" });
    expect(screen.getByRole("option", { name: "Refused Kirana, Old Market · consent: no" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "New Kirana, Old Market · consent: not yet" })).toBeInTheDocument();
  });

  it("records a shop that agreed", async () => {
    open();

    await pick("Agreed Kirana, Old Market · consent: yes");

    expect(screen.getByRole("button", { name: "Start recording" })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "Consent needed" })).not.toBeInTheDocument();
  });

  it.each([
    ["New Kirana, Old Market · consent: not yet", true],
    ["Refused Kirana, Old Market · consent: no", false],
  ])("doesn't start for %s, and offers its consent's ways", async (name, paper) => {
    open();

    await pick(name);

    expect(screen.getByRole("button", { name: "Start recording" })).toBeDisabled();
    expect(screen.getByRole("heading", { name: "Consent needed" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Share consent link" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Signed on paper" }) !== null).toBe(paper);
  });
});

describe("RecordPage's pre-visit brief (batch F5)", () => {
  it("shows the picked shop's brief: the overview and what to raise, the rest under a disclosure", async () => {
    open();
    expect(screen.queryByRole("region", { name: "Pre-visit brief" })).not.toBeInTheDocument();

    await pick("Agreed Kirana, Old Market · consent: yes");

    const card = await screen.findByRole("region", { name: "Pre-visit brief" });
    expect(await within(card).findByText("The shop raises late credit notes most. It orders Lay's every visit.")).toBeVisible();
    expect(within(card).getByText("Ask whether the credit note has come.")).toBeVisible();
    const more = within(card).getByText("Last visit, still open and fixed since");
    expect(within(card).getByText("Tell them: Deliveries are on Mondays now.")).not.toBeVisible();
    await userEvent.click(more);
    expect(within(card).getByText("Tell them: Deliveries are on Mondays now.")).toBeVisible();
    expect(screen.getByRole("button", { name: "Start recording" })).toBeEnabled();
  });
});
