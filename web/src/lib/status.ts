/** What each visit status reads as, and how it's shown (BRIEF §6). */
import type { Visit, VisitStatus } from "../types/api";

export type Tone = "busy" | "good" | "bad";

export const STATUS: Record<VisitStatus, { label: string; tone: Tone }> = {
  recording: { label: "Recording", tone: "busy" },
  uploaded: { label: "Waiting to transcribe", tone: "busy" },
  transcribing: { label: "Transcribing", tone: "busy" },
  transcribed: { label: "Transcribed", tone: "good" },
  extracting: { label: "Finding insights", tone: "busy" },
  review: { label: "Ready to review", tone: "good" },
  confirmed: { label: "Confirmed", tone: "good" },
  failed: { label: "Failed", tone: "bad" },
};

/** Whether a page should keep polling: the server says a visit still has work queued or running. */
export function anyProcessing(visits: Visit[]): boolean {
  return visits.some((visit) => visit.processing);
}
