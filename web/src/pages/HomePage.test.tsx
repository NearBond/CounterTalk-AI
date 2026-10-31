import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { describe, expect, it, vi } from "vitest";
import { jsonResponse as json, rejectOnAbort } from "../testUtils";
import { HomePage } from "./HomePage";

const OK_TEXT = "API ok, database ok";

const healthy = () => json(200, { status: "ok", database: "ok" });
const databaseDown = () => json(503, { status: "error", database: "error" });

/** A fetch whose answers the test hands out, and which rejects on abort like the real one. */
function controlledFetch() {
  const pending: Array<{ resolve: (response: Response) => void; signal?: AbortSignal | null }> = [];
  const fake = vi.fn(
    (_url: string, init?: RequestInit) =>
      new Promise<Response>((resolve, reject) => {
        pending.push({ resolve, signal: init?.signal });
        rejectOnAbort(init, reject);
      }),
  );
  vi.stubGlobal("fetch", fake);
  const signal = (request: number) => fake.mock.calls[request]?.[1]?.signal;
  /** Answers the request-th request still waiting (the oldest by default). */
  const answer = async (response: Response, request = 0) => {
    const [next] = pending.splice(request, 1);
    if (next === undefined) throw new Error("no request is waiting for an answer");
    await act(async () => next.resolve(response));
  };
  return { fake, answer, signal };
}

describe("HomePage", () => {
  it("says the API and database are ok when the health check passes", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => healthy()));

    render(<HomePage />);

    expect(await screen.findByText(OK_TEXT)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("announces every result in one live region that stays on the page", async () => {
    const server = controlledFetch();
    render(<HomePage />);
    const region = screen.getByRole("status");
    expect(region).toHaveTextContent(/checking/i);

    await server.answer(healthy());

    expect(screen.getByRole("status")).toBe(region);
    expect(region).toHaveTextContent(OK_TEXT);
  });

  it("says the database is down when the health check answers 503", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => databaseDown()));

    render(<HomePage />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent(/database isn't answering/i);
    expect(screen.queryByText(OK_TEXT)).not.toBeInTheDocument();
  });

  it("shows the server's error for a 503 that isn't the health check's", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>Unavailable</html>", { status: 503 })));

    render(<HomePage />);

    await screen.findByRole("button", { name: "Try again" });
    expect(screen.getByRole("status")).toHaveTextContent("HTTP 503");
  });

  it("shows an error when the server can't be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    render(<HomePage />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(/can't reach/i);
  });

  it("shows an error when a 200 reports a problem", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json(200, { status: "ok", database: "error" })));

    render(<HomePage />);

    expect(await screen.findByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText(OK_TEXT)).not.toBeInTheDocument();
  });

  it("checks again on Try again, and shows the new answer", async () => {
    const server = controlledFetch();
    render(<HomePage />);
    await server.answer(databaseDown());

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(server.fake).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("status")).toHaveTextContent(/checking/i);
    await server.answer(healthy());
    expect(screen.getByRole("status")).toHaveTextContent(OK_TEXT);
  });

  it("keeps focus on Try again during the retry, and ignores clicks until it's done", async () => {
    const server = controlledFetch();
    render(<HomePage />);
    await server.answer(databaseDown());
    const button = screen.getByRole("button", { name: "Try again" });

    await userEvent.click(button);
    await userEvent.click(button);

    expect(button).toHaveFocus();
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(server.fake).toHaveBeenCalledTimes(2);
    await server.answer(databaseDown());
    expect(button).toHaveFocus();
    expect(button).not.toHaveAttribute("aria-disabled", "true");
  });

  it("has exactly one h1", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => healthy()));

    render(<HomePage />);

    await screen.findByText(OK_TEXT);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("cancels a check that was replaced, and ignores its answer", async () => {
    // StrictMode runs the effect, cleans it up, and runs it again: two checks, the first one
    // cancelled. Its answer must not replace the second one's.
    const server = controlledFetch();
    render(
      <StrictMode>
        <HomePage />
      </StrictMode>,
    );
    expect(server.fake).toHaveBeenCalledTimes(2);
    expect(server.signal(0)?.aborted).toBe(true);

    await server.answer(healthy(), 1);
    await server.answer(databaseDown(), 0);

    expect(screen.getByRole("status")).toHaveTextContent(OK_TEXT);
  });

  it("cancels the check when the page is left", async () => {
    const server = controlledFetch();
    const { unmount } = render(<HomePage />);

    unmount();

    expect(server.signal(0)?.aborted).toBe(true);
  });
});
