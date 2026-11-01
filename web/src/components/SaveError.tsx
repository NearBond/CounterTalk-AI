import type { ApiError } from "../lib/api";
import { isRetryable } from "../lib/retry";

interface Props {
  error: ApiError | null;
  /** Sends it again: offered only when that could help, never for an answer that won't change. */
  onRetry: () => void;
}

/** A save's failure, after its retries: its message, and Try again if it's worth it (§9.11). */
export function SaveError({ error, onRetry }: Props) {
  if (error === null) return null;
  return (
    <p role="alert" className="text-sm text-red-700">
      {error.message}{" "}
      {isRetryable(error) && (
        <button type="button" onClick={onRetry} className="underline">
          Try again
        </button>
      )}
    </p>
  );
}
