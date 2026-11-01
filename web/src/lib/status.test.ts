import { describe, expect, it } from "vitest";
import type { Visit, VisitStatus } from "../types/api";
import { STATUS, anyProcessing } from "./status";

describe("STATUS", () => {
  it.each<[VisitStatus, string, string]>([
    ["recording", "Recording", "busy"],
    ["uploaded", "Waiting to transcribe", "busy"],
    ["transcribing", "Transcribing", "busy"],
    ["transcribed", "Transcribed", "good"],
    ["extracting", "Finding insights", "busy"],
    ["review", "Ready to review", "good"],
    ["confirmed", "Confirmed", "good"],
    ["failed", "Failed", "bad"],
  ])("%s reads %s, shown as %s", (status, label, tone) => {
    expect(STATUS[status]).toEqual({ label, tone });
  });
});

describe("anyProcessing", () => {
  const visit = (processing: boolean) => ({ processing }) as Visit;

  it.each([
    [[], false],
    [[visit(false), visit(false)], false],
    [[visit(false), visit(true)], true],
  ])("%j -> %s", (visits, expected) => {
    expect(anyProcessing(visits)).toBe(expected);
  });
});
