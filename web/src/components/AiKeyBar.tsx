import { useState } from "react";

// Dismissed for this browser session only: a new session warns again while the key is missing.
const DISMISSED = "countertalk.aiKeyBar.dismissed";

function dismissedBefore(): boolean {
  try {
    return sessionStorage.getItem(DISMISSED) === "1";
  } catch {
    return false; // storage blocked: it just shows
  }
}

/** The thin bar over every signed-in page while the server has no MODEL_API_KEY (batch F8; the
 * user's words, 5 Nov). Calm marigold, not an error: everything else works. ✕ hides it for the
 * session; it slides down, and stays still for reduced motion. */
export function AiKeyBar() {
  const [hidden, setHidden] = useState(dismissedBefore);
  if (hidden) return null;
  const dismiss = () => {
    try {
      sessionStorage.setItem(DISMISSED, "1");
    } catch {
      // storage blocked: hidden until the page reloads
    }
    setHidden(true);
  };
  return (
    <div
      role="status"
      className="animate-slide-down border-b border-marigold bg-marigold-soft text-ink motion-reduce:animate-none"
    >
      <div className="mx-auto flex w-full max-w-md items-start gap-2 px-4 py-2 sm:max-w-3xl">
        <p className="min-w-0 flex-1 text-sm leading-snug wrap-break-word">
          <span aria-hidden="true">⚠ </span>
          The Meta API key isn't set, so new recordings can't be processed, and Ask questions, briefs and summaries
          won't work. Everything else does.
        </p>
        <button
          type="button"
          onClick={dismiss}
          aria-label="Dismiss this warning"
          className="-my-2 -mr-3 inline-flex size-11 shrink-0 items-center justify-center rounded-md text-ink-2 hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-[-4px]"
        >
          <span aria-hidden="true">✕</span>
        </button>
      </div>
    </div>
  );
}
