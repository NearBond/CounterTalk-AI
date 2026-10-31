import { useEffect, useState } from "react";
import { ApiError, getHealth } from "../lib/api";

type Check = { state: "checking" } | { state: "ok" } | { state: "failed"; reason: string };

const MESSAGE_STYLE = {
  checking: "text-gray-600",
  ok: "font-medium text-green-700",
  failed: "text-red-700",
} as const;

async function checkHealth(signal: AbortSignal): Promise<Check> {
  try {
    const health = await getHealth(signal);
    if (health.status === "ok" && health.database === "ok") return { state: "ok" };
    return {
      state: "failed",
      reason:
        health.database === "ok"
          ? "The API reported a problem."
          : "The API is up, but the database isn't answering.",
    };
  } catch (error) {
    return {
      state: "failed",
      reason: error instanceof ApiError ? error.message : "Something went wrong while checking.",
    };
  }
}

export function HomePage() {
  const [attempt, setAttempt] = useState(0);
  const [check, setCheck] = useState<Check>({ state: "checking" });

  useEffect(() => {
    // Leaving the page, or starting a newer check, cancels this one; an answer that still
    // arrives is ignored (CODING_GUIDELINES §9.9).
    const controller = new AbortController();
    const run = async () => {
      const next = await checkHealth(controller.signal);
      if (!controller.signal.aborted) setCheck(next);
    };
    void run();
    return () => controller.abort();
  }, [attempt]);

  const checking = check.state === "checking";
  const tryAgain = () => {
    // aria-disabled rather than disabled, so the button keeps keyboard focus during the retry.
    if (checking) return;
    setCheck({ state: "checking" });
    setAttempt((count) => count + 1);
  };

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-6 px-4 py-10">
      <h1 className="text-2xl font-semibold">CounterTalk</h1>
      {/* One live region that stays mounted, so screen readers announce each change. */}
      <p role="status" className={MESSAGE_STYLE[check.state]}>
        {check.state === "checking" && "Checking the API…"}
        {check.state === "ok" && "API ok, database ok"}
        {check.state === "failed" && check.reason}
      </p>
      {(check.state === "failed" || (checking && attempt > 0)) && (
        <button
          type="button"
          onClick={tryAgain}
          aria-disabled={checking}
          className="self-start rounded-md bg-gray-900 px-4 py-2 text-white focus-visible:outline-2 focus-visible:outline-offset-2 aria-disabled:cursor-wait aria-disabled:opacity-60"
        >
          Try again
        </button>
      )}
    </main>
  );
}
