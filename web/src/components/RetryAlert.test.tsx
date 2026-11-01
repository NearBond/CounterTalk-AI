import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RetryAlert } from "./RetryAlert";

// Every source file of the app, as text: the scan below reads them.
const SOURCES = import.meta.glob<string>(["../**/*.tsx", "!../**/*.test.tsx"], {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("RetryAlert", () => {
  it("shows the failure, and Try again only when it's offered", async () => {
    const onRetry = vi.fn();
    const { rerender } = render(<RetryAlert onRetry={onRetry}>The visit couldn't be loaded.</RetryAlert>);

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    rerender(<RetryAlert>Recording isn't allowed here.</RetryAlert>);

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent(/^Recording isn't allowed here\.$/);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("is the app's one alert: no page writes its own (R1)", () => {
    const own = Object.entries(SOURCES)
      .filter(([path, text]) => !path.endsWith("/RetryAlert.tsx") && text.includes('role="alert"'))
      .map(([path]) => path);

    expect(Object.keys(SOURCES).length).toBeGreaterThan(10);
    expect(own).toEqual([]);
  });
});
