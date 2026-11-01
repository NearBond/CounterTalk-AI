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
  it.each([
    ["/", "Visits"],
    ["/record", "Record a visit"],
    ["/visits/v-1", "Visit"],
    ["/health", "Server status"],
  ])("shows its page at %s", (path, heading) => {
    renderAt(path);

    expect(screen.getByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
  });

  it.each(["/no-such-page", "/.env"])("shows a page, not a blank screen, at %s", (path) => {
    renderAt(path);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/not found/i);
    expect(screen.getByRole("link", { name: /home/i })).toHaveAttribute("href", "/");
  });
});
