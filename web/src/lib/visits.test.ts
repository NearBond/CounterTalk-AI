import { describe, expect, it } from "vitest";
import { MAX_SEGMENT_BYTES } from "../types/api";
import { byteRanges, extOf } from "./visits";

describe("byteRanges", () => {
  it.each([
    [1, [[0, 1]]],
    [MAX_SEGMENT_BYTES, [[0, MAX_SEGMENT_BYTES]]],
    [
      MAX_SEGMENT_BYTES + 1,
      [
        [0, MAX_SEGMENT_BYTES],
        [MAX_SEGMENT_BYTES, MAX_SEGMENT_BYTES + 1],
      ],
    ],
    [0, []],
  ])("a file of %i bytes goes up as %j", (size, expected) => {
    expect(byteRanges(size)).toEqual(expected);
  });
});

describe("extOf", () => {
  it.each([
    ["visit.m4a", "m4a"],
    ["Recording 12.M4A", "m4a"],
    ["shop.visit.webm", "webm"],
    ["call.mp3", null],
    ["no-extension", null],
    [".wav", null],
  ])("%s -> %s", (name, expected) => {
    expect(extOf(name)).toBe(expected);
  });
});
