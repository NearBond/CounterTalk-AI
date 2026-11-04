import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { FINANCE_MEMBER, fakeApi, jsonResponse as json, MANAGER } from "../testUtils";

function signInAt(path: string, answer: () => Response) {
  const api = fakeApi({ "POST /api/auth/signin": answer, "GET /api/auth/me": () => json(200, FINANCE_MEMBER) });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  render(<RouterProvider router={router} />);
  return { api, router };
}

async function fillIn(email: string, password: string) {
  await userEvent.type(screen.getByLabelText("Email"), email);
  await userEvent.type(screen.getByLabelText("Password"), password);
  await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SignInPage", () => {
  it("signs in and goes to the role's home", async () => {
    const { api, router } = signInAt("/signin", () => json(200, FINANCE_MEMBER));

    await fillIn("finance@example.com", "correct horse battery");

    // Every role's home is / since batch F4: a team member's own.
    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Finance Member · Demo Org" })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/");
    expect(api.sent()).toEqual([
      { route: "POST /api/auth/signin", body: { email: "finance@example.com", password: "correct horse battery" } },
    ]);
  });

  it("goes back where it was sent from", async () => {
    const { router } = signInAt("/signin?next=%2Fvisits%2Fv-1", () => json(200, MANAGER));

    await fillIn("manager@example.com", "correct horse battery");

    await vi.waitFor(() => expect(router.state.location.pathname).toBe("/visits/v-1"));
  });

  it.each(["//evil.example", "/\\evil.example", "https://evil.example", "/signin"])(
    "goes home, not to %s",
    async (next) => {
      const { router } = signInAt(`/signin?next=${encodeURIComponent(next)}`, () => json(200, MANAGER));

      await fillIn("manager@example.com", "correct horse battery");

      await vi.waitFor(() => expect(router.state.location.pathname).toBe("/"));
    },
  );

  it.each([
    [401, "wrong_email_or_password", "Wrong email or password"],
    [429, "too_many_tries", "Too many tries for this email. Try again in 3 seconds."],
  ])("says the server's one line for a %i", async (status, code, message) => {
    const { router } = signInAt("/signin", () => json(status, { error: { code, message } }));

    await fillIn("finance@example.com", "not it");

    expect(await screen.findByRole("alert")).toHaveTextContent(message);
    expect(router.state.location.pathname).toBe("/signin");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });
});
