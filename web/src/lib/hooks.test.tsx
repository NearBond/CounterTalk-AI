import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import { usePolling } from "./hooks";
import { POLL_MS, RETRY_MAX_MS } from "./limits";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

const tick = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms));

describe("usePolling", () => {
  it("loads, polls while shouldPoll says so, and stops when it doesn't", async () => {
    let count = 0;
    const load = vi.fn(async () => ++count);
    const { result } = renderHook(() =>
      usePolling(load, { key: "visit", shouldPoll: (value) => value < 3 }),
    );

    await tick(0);
    expect(result.current.data).toBe(1);
    await tick(POLL_MS);
    await tick(POLL_MS);
    expect(result.current.data).toBe(3);
    await tick(POLL_MS * 5);

    expect(load).toHaveBeenCalledTimes(3);
  });

  it("stops when the page goes: its request is aborted and a late answer is ignored", async () => {
    let answer: (value: number) => void = () => {};
    let signal: AbortSignal | undefined;
    const load = vi.fn(
      (given: AbortSignal) =>
        new Promise<number>((resolve) => {
          signal = given;
          answer = resolve;
        }),
    );
    const { result, unmount } = renderHook(() =>
      usePolling(load, { key: "visit", shouldPoll: () => true }),
    );
    await tick(0);

    unmount();
    answer(1);
    await tick(POLL_MS * 3);

    expect(signal?.aborted).toBe(true);
    expect(result.current.data).toBeUndefined();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("retries a failed load before giving up", async () => {
    const busy = () => new ApiError(503, "busy", "busy");
    const load = vi.fn().mockRejectedValueOnce(busy()).mockRejectedValueOnce(busy()).mockResolvedValue("loaded");
    const { result } = renderHook(() =>
      usePolling<string>(load, { key: "visit", shouldPoll: () => false }),
    );

    await tick(RETRY_MAX_MS * 2);

    expect(result.current).toMatchObject({ data: "loaded", error: null });
    expect(load).toHaveBeenCalledTimes(3);
  });

  it("an error that outlasts its retries stops polling and is reported, and reload() starts again", async () => {
    const failure = new ApiError(404, "visit_not_found", "There's no such visit");
    const load = vi.fn().mockRejectedValueOnce(failure).mockResolvedValue("back");
    const { result } = renderHook(() =>
      usePolling<string>(load, { key: "visit", shouldPoll: () => false }),
    );

    await tick(0);
    expect(result.current.error).toBe(failure);
    await tick(POLL_MS * 3);
    expect(load).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await tick(0);

    expect(result.current).toMatchObject({ data: "back", error: null });
  });

  it("starts over for a new key, never showing the old key's data under it", async () => {
    const load = vi.fn(async (_signal: AbortSignal) => `loaded ${load.mock.calls.length}`);
    const { result, rerender } = renderHook(
      ({ key }) => usePolling(load, { key, shouldPoll: () => false }),
      { initialProps: { key: "first" } },
    );
    await tick(0);
    expect(result.current.data).toBe("loaded 1");

    rerender({ key: "second" });
    expect(result.current.data).toBeUndefined();
    await tick(0);

    expect(result.current.data).toBe("loaded 2");
  });
});
