import { describe, expect, it } from "vitest";
import { MAX_SEGMENT_BYTES } from "../types/api";
import { byteRanges, extOf, sha256Hex } from "./visits";

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

describe("sha256Hex", () => {
  it("hashes a blob's bytes", async () => {
    const blob = new Blob([new TextEncoder().encode("abc")]);

    expect(await sha256Hex(blob)).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });
});
