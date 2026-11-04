import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeApi, jsonResponse as json } from "../testUtils";
import type { AskAnswer, Finding } from "../types/api";
import { AskBox } from "./AskBox";

const FINDING: Finding = {
  insight: {
    id: "i-1",
    seq: 0,
    kind: "problem",
    category: "pricing",
    severity: "medium",
    summary_en: "Lay's costs more here than at the next shop.",
    verified: true,
    visible: true,
    added_by_rep: false,
    sku: "Lay's",
    competitor: null,
    scheme_key: null,
    ai_team: "sales_ops",
    ai_team_reason: null,
    quotes: [{ seq: 0, turn_id: 2, speaker_role: "shopkeeper", text: "Lays mehnga hai", match: "exact", start_ms: 12_000 }],
    routes: [
      {
        slot: 1,
        team_id: "t-sales",
        team_key: "sales_ops",
        team_name: "Sales operations",
        routed_by: "table",
        version: 1,
        issue_id: "issue-1",
        issue_status: "open",
        resolution_note: null,
      },
    ],
    suggested_team: null, chosen_by_rep: false, left_by_rep: false, routing_version: 1, locked_team_id: null,
  },
  shop_name: "Sunrise Kirana",
  area: "Old Market",
  rep_name: "Demo Rep",
  visit_id: "v-1",
  visited_at: "2026-11-02T06:30:00Z",
};

const ANSWER: AskAnswer = {
  understood: true,
  reason: null,
  chips: [
    { name: "Category", value: "Pricing and margins" },
    { name: "Product", value: "Lay's" },
    { name: "Area", value: "Old Market" },
  ],
  findings: [FINDING],
};

function open(answer: () => Response) {
  const api = fakeApi({ "POST /api/ask": answer });
  render(
    <MemoryRouter>
      <AskBox />
    </MemoryRouter>,
  );
  return api;
}

async function ask(question: string) {
  await userEvent.type(screen.getByLabelText("In Hindi, Marathi or English"), question);
  await userEvent.click(screen.getByRole("button", { name: "Ask" }));
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("AskBox", () => {
  it("shows what was understood as chips, then each finding with its quote's ▶", async () => {
    const api = open(() => json(200, ANSWER));

    await ask("  Lay's pricing in Old Market  ");

    const chips = await screen.findByRole("list", { name: "What was understood" });
    expect(within(chips).getAllByRole("listitem").map((chip) => chip.textContent)).toEqual([
      "Category: Pricing and margins",
      "Product: Lay's",
      "Area: Old Market",
    ]);
    const finding = within(screen.getByRole("list", { name: "Findings" })).getByRole("listitem");
    expect(finding).toHaveTextContent("Sunrise Kirana (Old Market)");
    expect(finding).toHaveTextContent("Sales operations: Open");
    expect(finding).toHaveTextContent(FINDING.insight.summary_en);
    expect(within(finding).getByRole("button", { name: /^Play from / })).toBeInTheDocument();
    expect(api.sent()).toEqual([{ route: "POST /api/ask", body: { question: "Lay's pricing in Old Market" } }]);
  });

  it("says why a question wasn't understood, and when nothing matches", async () => {
    let answer: AskAnswer = { understood: false, reason: "It's about the weather.", chips: [], findings: [] };
    open(() => json(200, answer));

    await ask("Kal barish?");
    expect(await screen.findByRole("status")).toHaveTextContent("That couldn't be understood: It's about the weather.");

    answer = { ...ANSWER, findings: [] };
    await userEvent.click(screen.getByRole("button", { name: "Ask" }));
    expect(await screen.findByText("Nothing matches that.")).toBeInTheDocument();
  });

  it("past the day's questions says so, with no Try again", async () => {
    const said = "That's 30 questions today, the most a day. Ask again tomorrow.";
    open(() => json(429, { error: { code: "ask_limit", message: said } }));

    await ask("Supply?");

    expect(await screen.findByRole("alert")).toHaveTextContent(said);
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("can't send an empty question", () => {
    open(() => json(200, ANSWER));

    expect(screen.getByRole("button", { name: "Ask" })).toBeDisabled();
  });
});
