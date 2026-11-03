import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { REP, fakeApi, jsonResponse as json } from "../testUtils";
import type { Shop } from "../types/api";

const SHOPS: Shop[] = [
  { id: "s-yes", name: "Agreed Kirana", area: "Old Market", do_not_record: false, consent: "yes" },
  { id: "s-no", name: "Refused Kirana", area: "Old Market", do_not_record: false, consent: "no" },
  { id: "s-new", name: "New Kirana", area: "Old Market", do_not_record: false, consent: null },
];

function open() {
  fakeApi({ "GET /api/auth/me": () => json(200, REP), "GET /api/shops": () => json(200, { items: SHOPS }) });
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
