import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { sourcesWith } from "../testUtils";
import { QuoteLine } from "./QuoteLine";

describe("QuoteLine", () => {
  it("plays from where the words were said, and has no ▶ for words not found", async () => {
    const onPlay = vi.fn();
    const { rerender } = render(
      <QuoteLine
        quote={{ seq: 0, turn_id: 4, speaker_role: "shopkeeper", text: "Payment nahi aaya", match: "exact", start_ms: 67_520 }}
        lang="hi"
        onPlay={onPlay}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: "Play from 1:07" }));
    expect(onPlay).toHaveBeenCalledWith(67_520);
    expect(screen.getByText("Payment nahi aaya")).toHaveAttribute("lang", "hi");

    rerender(
      <QuoteLine
        quote={{ seq: 0, turn_id: null, speaker_role: null, text: "Discount do", match: "none", start_ms: null }}
        onPlay={onPlay}
      />,
    );
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("is the app's one ▶ for a quote: no page draws its own (R1)", () => {
    expect(sourcesWith("aria-label={`Play from", "QuoteLine.tsx")).toEqual([]);
  });
});
