/**
 * The browser recorder (BRIEF §2; CODING_GUIDELINES §9.9, §9.10): one MediaRecorder per visit,
 * Opus in WebM, a segment every SEGMENT_MS. Only the first segment carries the WebM header; the
 * server joins them (batch 5).
 *
 * The microphone is released on every path: a start that fails, stop(), abandon(), and a
 * microphone that goes away by itself. stop() and abandon() are safe to call twice.
 */
import { ApiError, abortedError } from "./api";
import { SEGMENT_MS } from "./limits";

/**
 * Decided in batch 7 (BUILD_PLAN): nothing plays back, so no echo cancellation; noise
 * suppression can eat the shopkeeper's quieter voice a metre away; gain control on.
 */
export const MIC_SETTINGS: MediaTrackConstraints = {
  echoCancellation: false,
  noiseSuppression: false,
  autoGainControl: true,
};
export const MIME_TYPE = "audio/webm;codecs=opus";

export interface RecorderEvents {
  /** Segment seq (0, 1, 2, …), as MediaRecorder hands it over. */
  onSegment: (seq: number, blob: Blob) => void;
  /** The microphone or the recorder stopped by itself, not through stop(). */
  onInterrupted: () => void;
}

export interface Recorder {
  /** Stops, and resolves once the last segment has been handed over. */
  stop: () => Promise<void>;
  /** Releases the microphone at once and drops what wasn't handed over: for leaving the page. */
  abandon: () => void;
}

function startError(error: unknown): ApiError {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return new ApiError(0, "microphone_refused", "The microphone wasn't allowed. Allow it for this site and try again.");
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return new ApiError(0, "microphone_missing", "No microphone was found.");
  }
  if (name === "NotReadableError") {
    return new ApiError(0, "microphone_busy", "The microphone is in use by another app.");
  }
  return new ApiError(0, "recorder_failed", "Recording couldn't start in this browser. Try Chrome or Edge.");
}

function release(stream: MediaStream): void {
  for (const track of stream.getTracks()) track.stop();
}

/** Asks for the microphone and starts recording. Aborting `signal` while the browser's prompt is
 * open releases the microphone as soon as it's granted. */
export async function startRecorder(
  events: RecorderEvents,
  signal?: AbortSignal,
  timesliceMs: number = SEGMENT_MS,
): Promise<Recorder> {
  let stream: MediaStream;
  try {
    if (typeof MediaRecorder === "undefined" || !MediaRecorder.isTypeSupported(MIME_TYPE)) {
      throw new DOMException("WebM audio isn't supported", "NotSupportedError");
    }
    stream = await navigator.mediaDevices.getUserMedia({ audio: MIC_SETTINGS });
  } catch (error) {
    throw startError(error);
  }
  if (signal?.aborted) {
    release(stream);
    throw abortedError("Recording was cancelled.");
  }

  let recorder: MediaRecorder;
  let stopping = false;
  let dropped = false;
  let stopped: Promise<void> | undefined;
  let next = 0;
  try {
    recorder = new MediaRecorder(stream, { mimeType: MIME_TYPE });
  } catch (error) {
    release(stream);
    throw startError(error);
  }
  const finished = new Promise<void>((resolve) => {
    recorder.addEventListener("stop", () => {
      release(stream);
      resolve();
    });
  });
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size > 0 && !dropped) events.onSegment(next++, event.data);
  });
  const interrupted = () => {
    if (stopping) return;
    events.onInterrupted();
  };
  recorder.addEventListener("error", interrupted);
  for (const track of stream.getAudioTracks()) track.addEventListener("ended", interrupted);

  const stop = (): Promise<void> => {
    stopping = true;
    stopped ??= (async () => {
      if (recorder.state !== "inactive") recorder.stop();
      await finished;
    })();
    return stopped;
  };
  const abandon = (): void => {
    stopping = true;
    dropped = true;
    if (recorder.state !== "inactive") recorder.stop();
    release(stream);
  };

  try {
    recorder.start(timesliceMs);
  } catch (error) {
    abandon();
    throw startError(error);
  }
  return { stop, abandon };
}
