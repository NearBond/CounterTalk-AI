import type { Choice } from "../types/api";

interface Props {
  label: string;
  choices: Choice[];
  value: string;
  disabled: boolean;
  onChange: (key: string) => void;
}

/** One of the playbook's keys, picked by its label: the review's pickers all use this (R1). */
export function ChoiceSelect({ label, choices, value, disabled, onChange }: Props) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      {label}
      <select
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className="rounded-md border border-gray-300 p-2"
      >
        {choices.map((choice) => (
          <option key={choice.key} value={choice.key}>
            {choice.label}
          </option>
        ))}
      </select>
    </label>
  );
}
