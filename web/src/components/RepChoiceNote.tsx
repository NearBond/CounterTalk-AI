import type { Insight } from "../types/api";

/** Whether the rep chose a finding's teams themselves in review (batch F1): "Suggested: <team> ·
 * chosen by the rep", or "Rep didn't pick a team" for one they left for the manager. Nothing
 * when the suggestion stood. The cards' one way to say it (R1). */
export function RepChoiceNote({ insight }: { insight: Insight }) {
  if (insight.left_by_rep) {
    return <p className="text-sm font-medium text-amber-900">Rep didn't pick a team</p>;
  }
  if (!insight.chosen_by_rep) return null;
  return (
    <p className="text-xs text-gray-600">Suggested: {insight.suggested_team ?? "no team"} · chosen by the rep</p>
  );
}
