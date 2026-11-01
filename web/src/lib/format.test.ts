import { describe, expect, it } from "vitest";
import { formatDateTime, formatDuration } from "./format";

describe("formatDuration", () => {
  it.each([
    [null, "–"],
    [0, "0:00"],
    [999, "0:00"],
    [5_000, "0:05"],
    [196_862, "3:16"],
    [775_600, "12:55"],
    [3_725_000, "1:02:05"],
  ])("%s ms -> %s", (ms, expected) => {
    expect(formatDuration(ms)).toBe(expected);
  });
});

describe("formatDateTime", () => {
  it.each([
    // In IST, whatever the browser's own time zone: 06:00 UTC is 11:30 in India.
    ["2026-11-01T06:00:00Z", "1 Nov 2026, 11:30 am"],
    ["2026-11-01T20:15:00Z", "2 Nov 2026, 1:45 am"],
    ["not a time", "–"],
  ])("%s -> %s", (iso, expected) => {
    expect(formatDateTime(iso)).toBe(expected);
  });
});
