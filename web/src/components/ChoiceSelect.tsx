import type { Choice } from "../types/api";

interface Props {
  label: string;
  choices: Choice[];
  value: string;
  disabled?: boolean;
  onChange: (key: string) => void;
}

/** One of a list's keys, picked by its label: every picker in the app uses this (R1). */
export function ChoiceSelect({ label, choices, value, disabled = false, onChange }: Props) {
  return (
    <label className="flex min-w-0 flex-col gap-1 text-sm">
      {label}
      <select
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className="w-full min-w-0 rounded-md border border-gray-300 p-2"
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
