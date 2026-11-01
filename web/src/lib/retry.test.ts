import { describe, expect, it, vi } from "vitest";
import { ABORTED, ApiError, NETWORK_ERROR } from "./api";
import { RETRY_BASE_MS, RETRY_JITTER, RETRY_MAX_MS } from "./limits";
import { backoffMs, isRetryable, sleep, withRetry } from "./retry";

const offline = () => new ApiError(0, NETWORK_ERROR, "offline");

describe("isRetryable", () => {
  it.each([
    [offline(), true],
    [new ApiError(503, "database_unavailable", "busy"), true],
    [new ApiError(500, "internal_error", "oops"), true],
    [new ApiError(429, "too_many", "slow down"), true],
    [new ApiError(408, "timeout", "slow"), true],
    [new ApiError(0, ABORTED, "cancelled"), false],
    [new ApiError(409, "visit_not_recording", "no"), false],
    [new ApiError(400, "invalid_seq", "no"), false],
    [new ApiError(404, "visit_not_found", "no"), false],
    [new Error("a bug"), false],
  ])("%s -> %s", (error, expected) => {
    expect(isRetryable(error)).toBe(expected);
  });
});

describe("backoffMs", () => {
  it.each([
    [1, 0.5, RETRY_BASE_MS],
    [2, 0.5, RETRY_BASE_MS * 2],
    [3, 0, RETRY_BASE_MS * 4 * (1 - RETRY_JITTER)],
    [3, 1, RETRY_BASE_MS * 4 * (1 + RETRY_JITTER)],
    [30, 0.5, RETRY_MAX_MS],
  ])("attempt %i with random %f waits %i ms", (attempt, random, expected) => {
    expect(backoffMs(attempt, () => random)).toBe(Math.round(expected));
  });
});

describe("withRetry", () => {
  const noWait = () => Promise.resolve();

  it("retries a passing failure until the call goes through", async () => {
    const call = vi.fn().mockRejectedValueOnce(offline()).mockRejectedValueOnce(offline()).mockResolvedValue("done");
    const waits: number[] = [];

    const result = await withRetry(call, {
      attempts: 5,
      random: () => 0.5,
      sleep: async (ms) => void waits.push(ms),
    });

    expect(result).toBe("done");
    expect(call).toHaveBeenCalledTimes(3);
    expect(waits).toEqual([RETRY_BASE_MS, RETRY_BASE_MS * 2]);
  });

  it("gives up after its attempts with the last error", async () => {
    const last = new ApiError(503, "busy", "still busy");
    const call = vi.fn().mockRejectedValueOnce(offline()).mockRejectedValue(last);

    await expect(withRetry(call, { attempts: 3, sleep: noWait })).rejects.toBe(last);
    expect(call).toHaveBeenCalledTimes(3);
  });

  it("doesn't retry what retrying can't fix", async () => {
    const refused = new ApiError(409, "segment_conflict", "other bytes");
    const call = vi.fn().mockRejectedValue(refused);

    await expect(withRetry(call, { attempts: 5, sleep: noWait })).rejects.toBe(refused);
    expect(call).toHaveBeenCalledTimes(1);
  });

  it("stops waiting and calls no more once aborted", async () => {
    const controller = new AbortController();
    const call = vi.fn().mockRejectedValue(offline());

    const retrying = withRetry(call, { attempts: 5, signal: controller.signal });
    await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(1));
    controller.abort();

    await expect(retrying).rejects.toMatchObject({ code: ABORTED });
    expect(call).toHaveBeenCalledTimes(1);
  });
});

describe("sleep", () => {
  it("rejects as aborted when its signal is already aborted", async () => {
    await expect(sleep(10_000, AbortSignal.abort())).rejects.toMatchObject({ code: ABORTED });
  });
});
