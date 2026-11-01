import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { APP_SOURCES, sourcesWith } from "../testUtils";
import { RetryAlert } from "./RetryAlert";

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
    expect(Object.keys(APP_SOURCES).length).toBeGreaterThan(10);
    expect(sourcesWith('role="alert"', "RetryAlert.tsx")).toEqual([]);
  });
});
