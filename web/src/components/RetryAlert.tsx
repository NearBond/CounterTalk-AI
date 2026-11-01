import type { ReactNode, Ref } from "react";

interface Props {
  children: ReactNode;
  /** Offered only when trying again could help: left out, there's no Try again. */
  onRetry?: () => void;
  disabled?: boolean;
  /** So ▶ can move focus to it (the visit page's recording). */
  retryRef?: Ref<HTMLButtonElement>;
  small?: boolean;
}

/** A failure the rep can read, and Try again when it could help (§9.11): the one way the app
 * shows one. */
export function RetryAlert({ children, onRetry, disabled = false, retryRef, small = false }: Props) {
  return (
    <p role="alert" className={small ? "text-sm text-red-700" : "text-red-700"}>
      {children}
      {onRetry !== undefined && (
        <>
          {" "}
          <button
            ref={retryRef}
            type="button"
            onClick={onRetry}
            disabled={disabled}
            className="underline disabled:opacity-50"
          >
            Try again
          </button>
        </>
      )}
    </p>
  );
}
