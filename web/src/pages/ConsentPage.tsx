import { useEffect, useState } from "react";
import { useParams } from "react-router";
import { RetryAlert } from "../components/RetryAlert";
import { ApiError, answerConsent, getConsentPage, messageOf } from "../lib/api";
import type { ConsentDecision, ConsentLanguage, ConsentPage as Page } from "../types/api";

/** What the page says before it knows a shop: in each language, as it can't know which. */
const NOT_A_LINK = "यह लिंक मान्य नहीं है। · This link isn't valid. · ही लिंक वैध नाही.";

type State =
  | { name: "loading" }
  | { name: "missing" }
  | { name: "failed"; message: string }
  | { name: "ready"; page: Page };

/** `/consent/<token>` (batch 17): a shop's consent page, public, on the shopkeeper's phone. The
 * company's and the shop's names, the notice in the language picked, the shop's current answer,
 * and I agree and No, as easy as each other, which change it at any time. */
export function ConsentPage() {
  const { token = "" } = useParams();
  const [state, setState] = useState<State>({ name: "loading" });
  const [language, setLanguage] = useState<ConsentLanguage>("hi");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  // The answer last tapped: Try again sends it again.
  const [tapped, setTapped] = useState<ConsentDecision | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getConsentPage(token, controller.signal)
      .then((page) => setState({ name: "ready", page }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) setState({ name: "missing" });
        else setState({ name: "failed", message: messageOf(error, "The page couldn't be loaded.") });
      });
    return () => controller.abort();
  }, [token, attempt]);

  const answer = async (decision: ConsentDecision) => {
    setTapped(decision);
    setSaving(true);
    setSaveError(null);
    try {
      const page = await answerConsent(token, { decision, language });
      setState({ name: "ready", page });
    } catch (error) {
      setSaveError(messageOf(error, "Your answer wasn't saved. Try again."));
    } finally {
      setSaving(false);
    }
  };

  if (state.name === "loading") {
    return (
      <main className="mx-auto w-full max-w-xl px-4 py-8">
        <p role="status">…</p>
      </main>
    );
  }
  if (state.name === "missing") {
    return (
      <main className="mx-auto w-full max-w-xl px-4 py-8">
        <h1 className="text-xl font-semibold">{NOT_A_LINK}</h1>
      </main>
    );
  }
  if (state.name === "failed") {
    return (
      <main className="mx-auto w-full max-w-xl px-4 py-8">
        <RetryAlert onRetry={() => setAttempt((n) => n + 1)}>{state.message}</RetryAlert>
      </main>
    );
  }

  const { page } = state;
  const text = page.notice[language];
  const answered =
    page.decision === "yes" ? text.answered_yes : page.decision === "no" ? text.answered_no : text.not_answered;
  const languages = Object.entries(page.notice) as Array<[ConsentLanguage, Page["notice"][ConsentLanguage]]>;
  return (
    <main lang={language} className="mx-auto flex w-full max-w-xl flex-col gap-4 px-4 py-6">
      <div role="group" aria-label="भाषा · Language · भाषा" className="flex flex-wrap gap-2">
        {languages.map(([code, words]) => (
          <button
            key={code}
            type="button"
            lang={code}
            aria-pressed={code === language}
            onClick={() => setLanguage(code)}
            className={`rounded-md border px-3 py-1 ${code === language ? "border-blue-700 bg-blue-50 font-medium" : "border-gray-300"}`}
          >
            {words.name}
          </button>
        ))}
      </div>
      <p className="text-gray-700">
        {page.company} · {page.shop}
      </p>
      <h1 className="text-2xl font-semibold">{text.title}</h1>
      <p>{text.what}</p>
      <p>{text.why}</p>
      <section aria-labelledby="benefits" className="flex flex-col gap-1">
        <h2 id="benefits" className="font-semibold">
          {text.benefits_title}
        </h2>
        <ul className="list-disc pl-6">
          {text.benefits.map((benefit) => (
            <li key={benefit}>{benefit}</li>
          ))}
        </ul>
      </section>
      <p>{text.real_time}</p>
      <p>{text.retention}</p>
      <p>{text.no_change}</p>
      <p>{text.change_mind}</p>
      <p role="status" className="font-medium">
        {answered}
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          disabled={saving}
          aria-pressed={page.decision === "yes"}
          onClick={() => void answer("yes")}
          className="min-w-32 rounded-md bg-green-700 px-4 py-3 font-medium text-white disabled:opacity-50"
        >
          {text.agree}
        </button>
        <button
          type="button"
          disabled={saving}
          aria-pressed={page.decision === "no"}
          onClick={() => void answer("no")}
          className="min-w-32 rounded-md bg-gray-800 px-4 py-3 font-medium text-white disabled:opacity-50"
        >
          {text.refuse}
        </button>
      </div>
      {saveError !== null && (
        <RetryAlert onRetry={tapped === null ? undefined : () => void answer(tapped)} disabled={saving}>
          {saveError}
        </RetryAlert>
      )}
    </main>
  );
}
