import { describe, expect, it } from "vitest";
import { sourcesWith } from "../testUtils";
import { countOf, formatDateTime, formatDuration, formatPeriod } from "./format";

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

describe("formatPeriod", () => {
  it("gives a week from Monday to Sunday, and a month by its name, in IST", () => {
    expect(formatPeriod("week", "2026-10-25T18:30:00Z", "2026-11-01T18:30:00Z")).toBe("26 Oct – 1 Nov 2026");
    expect(formatPeriod("month", "2026-09-30T18:30:00Z", "2026-10-31T18:30:00Z")).toBe("October 2026");
  });

  it("names both years for a week across a new year", () => {
    expect(formatPeriod("week", "2026-12-27T18:30:00Z", "2027-01-03T18:30:00Z")).toBe("28 Dec 2026 – 3 Jan 2027");
  });
});

describe("countOf", () => {
  it("words one, many and a fraction", () => {
    expect([countOf(1, "shop", "shops"), countOf(3, "shop", "shops"), countOf(0.5, "shop", "shops")]).toEqual([
      "1 shop",
      "3 shops",
      "0.5 shops",
    ]);
  });

  it("is the one place a count is worded (R1)", () => {
    expect(sourcesWith("=== 1 ?", "format.ts")).toEqual([]);
  });
});
