import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { FINANCE_MEMBER, MANAGER, REP, fakeApi, jsonResponse as json } from "../testUtils";
import type { Me } from "../types/api";

function open(me: Me, path = "/profile") {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, me),
    "POST /api/auth/signout": () => new Response(null, { status: 204 }),
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return { api, router };
}

function details(): string[] {
  return [...document.querySelectorAll("dt")].map((term) => `${term.textContent}: ${term.nextElementSibling?.textContent}`);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ProfilePage", () => {
  it.each([
    [REP, ["Name: Demo Rep", "Email: rep@example.com", "Company: PepsiCo India", "Position: Sales rep"]],
    [
      FINANCE_MEMBER,
      ["Name: Finance Member", "Email: finance@example.com", "Company: PepsiCo India", "Position: Team member", "Team: Finance"],
    ],
    [MANAGER, ["Name: Demo Manager", "Email: manager@example.com", "Company: PepsiCo India", "Position: Manager"]],
  ])("shows who's signed in, read-only: %s.role", async (me, shown) => {
    open(me);

    expect(await screen.findByRole("heading", { level: 1, name: "Profile" })).toBeInTheDocument();
    expect(details()).toEqual(shown);
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("is reached from the name in the header", async () => {
    const { router } = open(REP, "/");

    await userEvent.click(await screen.findByRole("link", { name: "Demo Rep" }));

    expect(router.state.location.pathname).toBe("/profile");
  });

  it("signs out from the profile too", async () => {
    const { router } = open(MANAGER);
    const [, onThePage] = await screen.findAllByRole("button", { name: "Sign out" }); // the header's first

    await userEvent.click(onThePage as HTMLElement);

    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/signin");
  });
});
