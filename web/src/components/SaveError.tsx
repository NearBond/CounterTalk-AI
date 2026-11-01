import type { ApiError } from "../lib/api";
import { isRetryable } from "../lib/retry";
import { RetryAlert } from "./RetryAlert";

interface Props {
  error: ApiError | null;
  /** Sends it again: offered only when that could help, never for an answer that won't change. */
  onRetry: () => void;
}

/** A save's failure, after its retries: its message, and Try again if it's worth it (§9.11). */
export function SaveError({ error, onRetry }: Props) {
  if (error === null) return null;
  return (
    <RetryAlert small onRetry={isRetryable(error) ? onRetry : undefined}>
      {error.message}
    </RetryAlert>
  );
}
