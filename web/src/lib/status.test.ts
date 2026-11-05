import { describe, expect, it } from "vitest";
import type { Visit } from "../types/api";
import { anyProcessing } from "./status";

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
