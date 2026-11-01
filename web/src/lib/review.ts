/**
 * The review's logic outside React (batch 14): what a card's edit changes, the playbook's labels,
 * and confirm, which finishes the rep's work and so is retried (§9.11).
 */
import type { Choice, Insight, InsightChange, Visit } from "../types/api";
import { postConfirm } from "./api";
import { COMPLETE_ATTEMPTS, SAVE_ATTEMPTS } from "./limits";
import { withRetry } from "./retry";

/** What a card's edit form holds. */
export interface Draft {
  summary_en: string;
  category: string;
  severity: string;
}

export function draftOf(insight: Insight): Draft {
  return { summary_en: insight.summary_en, category: insight.category, severity: insight.severity };
}

/** Only what changed, the summary trimmed as the server trims it: empty means nothing to save. */
export function changesOf(insight: Insight, draft: Draft): InsightChange {
  const change: InsightChange = {};
  const summary = draft.summary_en.trim();
  if (summary !== insight.summary_en) change.summary_en = summary;
  if (draft.category !== insight.category) change.category = draft.category;
  if (draft.severity !== insight.severity) change.severity = draft.severity;
  return change;
}

/** A key's label from the playbook's choices; the key itself if it has none. */
export function labelOf(choices: Choice[], key: string): string {
  return choices.find((choice) => choice.key === key)?.label ?? key;
}

/** Confirms the visit, retried for about 30 s: confirming again returns the same visit. */
export function confirmReview(visitId: string, signal?: AbortSignal): Promise<Visit> {
  return withRetry(() => postConfirm(visitId, signal), { attempts: COMPLETE_ATTEMPTS, signal });
}

/** A review save, retried through a passing failure: each is safe to repeat (an added finding or
 * note carries its own ID). */
export function saveWithRetry<T>(call: (signal: AbortSignal) => Promise<T>, signal: AbortSignal): Promise<T> {
  return withRetry(() => call(signal), { attempts: SAVE_ATTEMPTS, signal });
}

/** A new ID for a finding or a note, made once per add so a retry sends the same one. */
export function newId(): string {
  return crypto.randomUUID();
}
