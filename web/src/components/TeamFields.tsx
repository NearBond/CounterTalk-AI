import type { Choice, Insight, Team } from "../types/api";
import { ChoiceSelect } from "./ChoiceSelect";

// The main team's choice that sends it to no team: the manager's triage.
const LEAVE = "leave";
const NONE: Choice = { key: "", label: "None" };

interface Props {
  insight: Insight;
  teams: Team[];
  /** The teams picked, the main one first; none leaves it for the manager. */
  chosen: string[];
  disabled: boolean;
  onChange: (chosen: string[]) => void;
}

/** A finding's teams in its Edit form, for the visit's rep in review (batch F1): today's routing
 * comes filled in, as the suggestion. A main team, a second one, or Leave for the manager; the
 * category's override team stays, shown, and can't be taken off. */
export function TeamFields({ insight, teams, chosen, disabled, onChange }: Props) {
  // The server says which team is locked: the rule's override, as it decides it now.
  const locked = teams.find((team) => team.id === insight.locked_team_id);
  const options = teams.filter((team) => team.id !== locked?.id).map((team) => ({ key: team.id, label: team.name }));
  const picks = chosen.filter((id) => id !== locked?.id);

  return (
    <>
      {locked !== undefined ? (
        <>
          <p className="text-sm">
            <span className="font-medium">{locked.name}</span> always gets it, for this category and severity.
          </p>
          <ChoiceSelect
            label="Also send to"
            choices={[NONE, ...options]}
            value={picks[0] ?? ""}
            disabled={disabled}
            onChange={(value) => onChange(value === "" ? [locked.id] : [value, locked.id])}
          />
        </>
      ) : (
        <>
          <ChoiceSelect
            label="Main team"
            choices={[...options, { key: LEAVE, label: "Leave for the manager" }]}
            value={picks[0] ?? LEAVE}
            disabled={disabled}
            onChange={(value) =>
              onChange(value === LEAVE ? [] : [value, ...picks.slice(1).filter((id) => id !== value)])
            }
          />
          <ChoiceSelect
            label="Second team"
            choices={[NONE, ...options.filter((team) => team.key !== picks[0])]}
            value={picks[1] ?? ""}
            disabled={disabled || picks.length === 0}
            onChange={(value) => onChange(value === "" ? picks.slice(0, 1) : [picks[0] as string, value])}
          />
        </>
      )}
      {insight.suggested_team !== null && <p className="text-xs text-gray-600">Suggested: {insight.suggested_team}</p>}
    </>
  );
}
