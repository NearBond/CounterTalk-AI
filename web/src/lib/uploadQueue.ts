/**
 * Uploads a visit's segments, one at a time and in order, each with its SHA-256 (BRIEF §2).
 *
 * - A passing failure (offline, 5xx) is retried with backoff and jitter, for as long as it takes:
 *   a phone out of signal catches up when it's back. Later segments wait their turn.
 * - A failure retrying can't fix (a 4xx) stops the queue, and drain() rejects with it.
 * - stop() cancels the upload in flight and every wait; it's safe to call twice (§9.10).
 */
import { ApiError, abortedError } from "./api";
import { type Sleep, withRetry } from "./retry";
import { sha256Hex } from "./visits";

export type Upload = (seq: number, body: Blob, sha256: string, signal: AbortSignal) => Promise<unknown>;

export interface QueueState {
  /** Segments not uploaded yet, the one being sent included. */
  waiting: number;
  uploaded: number;
  /** True while the segment at the front is between tries. */
  retrying: boolean;
  /** The failure that stopped the queue, if one did. */
  failed: ApiError | null;
}

export interface QueueOptions {
  upload: Upload;
  onChange?: (state: QueueState) => void;
  hash?: (body: Blob) => Promise<string>;
  random?: () => number;
  sleep?: Sleep;
}

type Item = { seq: number; body: Blob };
type Waiter = { resolve: () => void; reject: (error: unknown) => void };

export class UploadQueue {
  readonly state: QueueState = { waiting: 0, uploaded: 0, retrying: false, failed: null };
  private readonly items: Item[] = [];
  private readonly waiters: Waiter[] = [];
  private readonly controller = new AbortController();
  private running = false;
  private stopped: ApiError | null = null;

  constructor(private readonly options: QueueOptions) {}

  /** Throws once the queue has stopped or failed, so no segment is dropped without a word. */
  add(seq: number, body: Blob): void {
    if (this.stopped !== null) throw this.stopped;
    if (this.state.failed !== null) throw this.state.failed;
    this.items.push({ seq, body });
    this.change({ waiting: this.items.length });
    if (!this.running) void this.run();
  }

  /** Resolves once every added segment is uploaded; rejects if the queue stopped first. */
  drain(): Promise<void> {
    const ended = this.stopped ?? this.state.failed;
    if (ended !== null) return Promise.reject(ended);
    if (this.items.length === 0) return Promise.resolve();
    return new Promise((resolve, reject) => this.waiters.push({ resolve, reject }));
  }

  stop(): void {
    if (this.stopped !== null) return;
    this.stopped = abortedError("The upload was stopped.");
    this.controller.abort();
    if (this.state.retrying) this.change({ retrying: false });
    this.settle(this.stopped);
  }

  private async run(): Promise<void> {
    this.running = true;
    const { signal } = this.controller;
    try {
      for (let item = this.items[0]; item !== undefined; item = this.items[0]) {
        const { seq, body } = item;
        const sha256 = await (this.options.hash ?? sha256Hex)(body); // once, however many tries
        await withRetry(() => this.options.upload(seq, body, sha256, signal), {
          attempts: Infinity,
          signal,
          onRetry: () => this.change({ retrying: true }),
          random: this.options.random,
          sleep: this.options.sleep,
        });
        this.items.shift();
        this.change({ waiting: this.items.length, uploaded: this.state.uploaded + 1, retrying: false });
      }
      this.settle(null);
    } catch (error) {
      if (this.stopped !== null) return; // stop() has already told the waiters
      if (!(error instanceof ApiError)) console.error("A segment couldn't be read to upload it", error);
      const failed =
        error instanceof ApiError
          ? error
          : new ApiError(0, "upload_failed", "A piece of the recording couldn't be read to upload it.");
      this.change({ retrying: false, failed });
      this.settle(failed);
    } finally {
      this.running = false;
    }
  }

  private change(update: Partial<QueueState>): void {
    Object.assign(this.state, update);
    this.options.onChange?.({ ...this.state });
  }

  private settle(error: ApiError | null): void {
    for (const waiter of this.waiters.splice(0)) {
      if (error === null) waiter.resolve();
      else waiter.reject(error);
    }
  }
}
