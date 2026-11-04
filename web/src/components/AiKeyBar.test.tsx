import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { REP, fakeApi, jsonResponse as json } from "../testUtils";

// The user's exact words (5 Nov); the ⚠ is decoration, hidden from screen readers.
const WORDS =
  "The Meta API key isn't set, so new recordings can't be processed, and Ask questions, briefs and summaries won't work. Everything else does.";

function open(path: string, aiKeySet: boolean) {
  fakeApi({ "GET /api/auth/me": () => json(200, { ...REP, ai_key_set: aiKeySet }) });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const view = render(<RouterProvider router={router} />);
  return { router, view };
}

afterEach(() => {
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("AiKeyBar (batch F8)", () => {
  it("shows over a signed-in page while the server has no model key", async () => {
    open("/profile", false);

    const bar = (await screen.findByText(WORDS, { exact: false })).closest('[role="status"]');
    expect(bar).toHaveTextContent(`⚠ ${WORDS}`);
    expect(screen.getByRole("button", { name: "Dismiss this warning" })).toBeInTheDocument();
    expect(bar).toHaveClass("motion-reduce:animate-none");
  });

  it("isn't there when the key is set", async () => {
    open("/profile", true);

    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByText(WORDS, { exact: false })).not.toBeInTheDocument();
  });

  it("✕ hides it for the rest of the session, a reload included", async () => {
    const first = open("/profile", false);
    await userEvent.click(await screen.findByRole("button", { name: "Dismiss this warning" }));

    expect(screen.queryByText(WORDS, { exact: false })).not.toBeInTheDocument();
    first.view.unmount();
    open("/profile", false);
    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByText(WORDS, { exact: false })).not.toBeInTheDocument();
  });

  it("still shows, and ✕ still hides it, when session storage is blocked", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    open("/profile", false);

    await userEvent.click(await screen.findByRole("button", { name: "Dismiss this warning" }));

    expect(screen.queryByText(WORDS, { exact: false })).not.toBeInTheDocument();
    vi.restoreAllMocks();
  });

  it("isn't on the sign-in page", async () => {
    open("/signin", false);

    expect(await screen.findByRole("heading", { level: 1 })).toBeInTheDocument();
    expect(screen.queryByText(WORDS, { exact: false })).not.toBeInTheDocument();
  });
});
