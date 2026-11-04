import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { FINANCE_MEMBER, fakeApi, jsonResponse as json, MANAGER } from "../testUtils";

function signInAt(path: string, answer: () => Response, more: Record<string, () => Response> = {}) {
  const api = fakeApi({
    "POST /api/auth/signin": answer,
    "GET /api/auth/me": () => json(200, FINANCE_MEMBER),
    ...more,
  });
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
  it("lists the demo accounts with their password, reps first, and a tap fills in the email (batch F9)", async () => {
    signInAt("/signin", () => json(200, FINANCE_MEMBER), {
      "GET /api/auth/demo-accounts": () =>
        json(200, {
          accounts: [
            { email: "rep@example.com", label: "Field sales rep", note: "Hindi, Hinglish and English" },
            { email: "admin@example.com", label: "Admin", note: null },
          ],
          password: "demo-pass",
        }),
    });

    const list = await screen.findByRole("list", { name: "Demo accounts" });
    expect(screen.getByText(/Every demo account's password is/)).toHaveTextContent(
      "Every demo account's password is demo-pass",
    );
    const items = within(list).getAllByRole("listitem").map((item) => item.textContent);
    expect(items).toEqual(["rep@example.comField sales rep · Hindi, Hinglish and English", "admin@example.comAdmin"]);
    await userEvent.click(within(list).getByRole("button", { name: /admin@example.com/ }));
    expect(screen.getByLabelText("Email")).toHaveValue("admin@example.com");
  });

  it("shows no demo accounts when the host lists none", async () => {
    const asked = vi.fn(() => json(200, { accounts: [], password: null }));
    signInAt("/signin", () => json(200, FINANCE_MEMBER), { "GET /api/auth/demo-accounts": asked });

    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    await vi.waitFor(() => expect(asked).toHaveBeenCalled());
    expect(screen.queryByRole("heading", { name: "Demo accounts" })).not.toBeInTheDocument();
  });

  it("signs in and goes to the role's home", async () => {
    const { api, router } = signInAt("/signin", () => json(200, FINANCE_MEMBER));

    await fillIn("finance@example.com", "correct horse battery");

    // Every role's home is / since batch F4: a team member's own.
    expect(await screen.findByRole("heading", { level: 1, name: "Hi, Finance Member · PepsiCo India" })).toBeInTheDocument();
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
