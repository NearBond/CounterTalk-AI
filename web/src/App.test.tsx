import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";
import { App } from "./App";

function renderAt(path: string) {
  vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(() => {})));
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

describe("App", () => {
  it("shows the home page at /", () => {
    renderAt("/");

    expect(screen.getByRole("heading", { level: 1, name: "CounterTalk" })).toBeInTheDocument();
  });

  it.each(["/no-such-page", "/.env"])("shows a page, not a blank screen, at %s", (path) => {
    renderAt(path);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/not found/i);
    expect(screen.getByRole("link", { name: /home/i })).toHaveAttribute("href", "/");
  });
});
