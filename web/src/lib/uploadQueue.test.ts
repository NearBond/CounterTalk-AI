import { describe, expect, it, vi } from "vitest";
import { ABORTED, ApiError, NETWORK_ERROR } from "./api";
import { type QueueState, UploadQueue } from "./uploadQueue";

const ABC_SHA256 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
const offline = () => new ApiError(0, NETWORK_ERROR, "offline");
const blob = (text: string) => new Blob([new TextEncoder().encode(text)]);
const noWait = () => Promise.resolve();

type Sent = { seq: number; sha256: string; text: string };

/** An upload that records what it sent, failing the first `failures[seq]` tries of a segment. */
function server(failures: Record<number, ApiError[]> = {}) {
  const sent: Sent[] = [];
  const tries: number[] = [];
  const upload = vi.fn(async (seq: number, body: Blob, sha256: string) => {
    tries.push(seq);
    const error = failures[seq]?.shift();
    if (error !== undefined) throw error;
    sent.push({ seq, sha256, text: await body.text() });
  });
  return { upload, sent, tries };
}

describe("UploadQueue", () => {
  it("uploads each segment once, in order, with its SHA-256", async () => {
    const api = server();
    const queue = new UploadQueue({ upload: api.upload, sleep: noWait });

    queue.add(0, blob("abc"));
    queue.add(1, blob("second"));
    await queue.drain();

    expect(api.sent.map((s) => [s.seq, s.text])).toEqual([
      [0, "abc"],
      [1, "second"],
    ]);
    expect(api.sent[0]?.sha256).toBe(ABC_SHA256);
  });

  it("retries a passing failure, with backoff, and keeps later segments waiting", async () => {
    const api = server({ 0: [offline(), offline(), new ApiError(503, "busy", "busy")] });
    const waits: number[] = [];
    const hash = vi.fn(async () => "a".repeat(64));
    const queue = new UploadQueue({
      upload: api.upload,
      hash,
      random: () => 0.5,
      sleep: async (ms) => void waits.push(ms),
    });

    queue.add(0, blob("zero"));
    queue.add(1, blob("one"));
    await queue.drain();

    expect(api.tries).toEqual([0, 0, 0, 0, 1]);
    expect(waits).toHaveLength(3);
    expect(waits[1]).toBe(2 * (waits[0] ?? 0));
    expect(hash).toHaveBeenCalledTimes(2); // once per segment, not once per try
  });

  it("stops at a failure retrying can't fix, and drain says why", async () => {
    const refused = new ApiError(409, "visit_not_recording", "This visit isn't recording any more");
    const api = server({ 1: [refused] });
    const queue = new UploadQueue({ upload: api.upload, sleep: noWait });

    queue.add(0, blob("zero"));
    queue.add(1, blob("one"));
    queue.add(2, blob("two"));

    await expect(queue.drain()).rejects.toBe(refused);
    expect(api.sent.map((s) => s.seq)).toEqual([0]);
    expect(queue.state.failed).toBe(refused);
    expect(() => queue.add(3, blob("three"))).toThrow(refused); // never silently dropped
  });

  it("a segment that can't be read stops the queue, with the cause logged", async () => {
    const unreadable = new TypeError("crypto.subtle is undefined");
    const logged = vi.spyOn(console, "error").mockImplementation(() => {});
    const queue = new UploadQueue({ upload: vi.fn(), hash: () => Promise.reject(unreadable) });

    queue.add(0, blob("zero"));

    await expect(queue.drain()).rejects.toMatchObject({ code: "upload_failed" });
    expect(logged).toHaveBeenCalledWith(expect.any(String), unreadable);
  });

  it("stop() cancels the upload in flight and its retries, and is safe to call twice", async () => {
    let signal: AbortSignal | undefined;
    const upload = vi.fn(
      (_seq: number, _body: Blob, _sha: string, given: AbortSignal) =>
        new Promise<void>((_resolve, reject) => {
          signal = given;
          given.addEventListener("abort", () => reject(new ApiError(0, ABORTED, "cancelled")));
        }),
    );
    const queue = new UploadQueue({ upload, sleep: noWait });
    queue.add(0, blob("zero"));
    const drained = queue.drain();
    await vi.waitFor(() => expect(upload).toHaveBeenCalledTimes(1));

    queue.stop();
    queue.stop();

    expect(signal?.aborted).toBe(true);
    await expect(drained).rejects.toMatchObject({ code: ABORTED });
    expect(() => queue.add(1, blob("one"))).toThrow();
    expect(upload).toHaveBeenCalledTimes(1);
  });

  it("a queue stopped while it waits to retry no longer says it's retrying", async () => {
    const api = server({ 0: [offline()] });
    const seen: QueueState[] = [];
    const waitForever = (_ms: number, given?: AbortSignal) =>
      new Promise<void>((_resolve, reject) =>
        given?.addEventListener("abort", () => reject(new ApiError(0, ABORTED, "cancelled"))),
      );
    const queue = new UploadQueue({ upload: api.upload, sleep: waitForever, onChange: (state) => seen.push({ ...state }) });
    queue.add(0, blob("zero"));
    await vi.waitFor(() => expect(queue.state.retrying).toBe(true));

    queue.stop();

    expect(queue.state.retrying).toBe(false);
    expect(seen.at(-1)?.retrying).toBe(false);
  });

  it("reports what's waiting, what's uploaded and whether it's retrying", async () => {
    const api = server({ 0: [offline()] });
    const seen: QueueState[] = [];
    const queue = new UploadQueue({
      upload: api.upload,
      sleep: noWait,
      onChange: (state) => seen.push({ ...state }),
    });

    queue.add(0, blob("zero"));
    await queue.drain();

    expect(seen.at(0)).toMatchObject({ waiting: 1, uploaded: 0, retrying: false });
    expect(seen.some((state) => state.retrying)).toBe(true);
    expect(seen.at(-1)).toMatchObject({ waiting: 0, uploaded: 1, retrying: false, failed: null });
  });

  it("drains at once when nothing is waiting", async () => {
    await expect(new UploadQueue({ upload: vi.fn() }).drain()).resolves.toBeUndefined();
  });
});
