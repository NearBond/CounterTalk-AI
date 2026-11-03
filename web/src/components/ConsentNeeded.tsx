import { useState } from "react";
import { markPaperConsent, messageOf, shareConsentLink } from "../lib/api";
import type { ConsentLanguage, Shop } from "../types/api";
import { RetryAlert } from "./RetryAlert";

/** The languages a paper form is printed in, as the rep picks the one signed. */
const FORM_LANGUAGES: Array<[ConsentLanguage, string]> = [
  ["hi", "हिन्दी"],
  ["en", "English"],
  ["mr", "मराठी"],
];

interface Shared {
  url: string;
  /** The message and the link, as WhatsApp sends them. */
  text: string;
}

interface Props {
  shop: Shop;
  /** The shop's answer may have changed (a paper form marked, or the rep asks after sharing
   * the link): the page reads its shops again. */
  onAnswered: () => void;
}

/** A shop without a yes can't be recorded (batch 17; the user, 3 Nov). Its consent comes before
 * the visit: the rep shares the shop's consent link on WhatsApp, from their own phone, or marks
 * the paper form the shopkeeper signed with the invoice. A shop that said no on its page changes
 * that there, so only its link is offered. */
export function ConsentNeeded({ shop, onAnswered }: Props) {
  const [shared, setShared] = useState<Shared | null>(null);
  const [paper, setPaper] = useState(false);
  const [language, setLanguage] = useState<ConsentLanguage>("hi");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const refused = shop.consent === "no";

  const share = async () => {
    setBusy(true);
    setError(null);
    try {
      const link = await shareConsentLink(shop.id);
      const url = new URL(link.path, window.location.origin).toString();
      const text = `${link.message}\n${url}`;
      setShared({ url, text });
      // The phone's share sheet where there is one; otherwise the WhatsApp link below.
      if (typeof navigator.share === "function") {
        await navigator.share({ text }).catch(() => {}); // closed without sharing: the link stays here
      }
    } catch (failure) {
      setError(messageOf(failure, "The consent link couldn't be made. Try again."));
    } finally {
      setBusy(false);
    }
  };

  const markPaper = async () => {
    setBusy(true);
    setError(null);
    try {
      await markPaperConsent(shop.id, language);
      onAnswered();
    } catch (failure) {
      setError(messageOf(failure, "The paper form wasn't marked. Try again."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section aria-labelledby="consent-heading" className="flex flex-col gap-3 rounded-md border border-amber-300 bg-amber-50 p-3">
      <h2 id="consent-heading" className="text-lg font-semibold">
        Consent needed
      </h2>
      <p>
        {refused
          ? "This shop said no to recording on its consent page. It can change its answer there: share its link again."
          : "This shop hasn't agreed to recording yet. Share its consent link on WhatsApp, or mark the paper form it signed with the invoice."}
      </p>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => void share()}
          className="rounded-md bg-green-700 px-4 py-2 font-medium text-white disabled:opacity-50"
        >
          Share consent link
        </button>
        {!refused && !paper && (
          <button
            type="button"
            disabled={busy}
            onClick={() => setPaper(true)}
            className="rounded-md border border-gray-400 bg-white px-4 py-2 font-medium disabled:opacity-50"
          >
            Signed on paper
          </button>
        )}
      </div>
      {shared !== null && (
        <div className="flex flex-col gap-1">
          <a
            href={`https://wa.me/?text=${encodeURIComponent(shared.text)}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-700 underline"
          >
            Send it on WhatsApp
          </a>
          <p className="break-all text-sm text-gray-700">{shared.url}</p>
          {/* The shop answers on its own phone: the rep asks again when it says it has. */}
          <button
            type="button"
            onClick={onAnswered}
            className="self-start rounded-md border border-gray-400 bg-white px-3 py-1 text-sm"
          >
            Check for the shop's answer
          </button>
        </div>
      )}
      {!refused && paper && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="flex flex-col gap-1">
            <span className="font-medium">The form's language</span>
            <select
              value={language}
              onChange={(event) => setLanguage(event.target.value as ConsentLanguage)}
              className="rounded-md border border-gray-300 px-3 py-2"
            >
              {FORM_LANGUAGES.map(([code, name]) => (
                <option key={code} value={code} lang={code}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => void markPaper()}
            className="rounded-md bg-gray-900 px-4 py-2 font-medium text-white disabled:opacity-50"
          >
            Mark the signed form
          </button>
        </div>
      )}
      {error !== null && <RetryAlert>{error}</RetryAlert>}
    </section>
  );
}
