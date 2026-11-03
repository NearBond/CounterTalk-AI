import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { fakeApi, jsonResponse as json } from "../testUtils";
import type { ConsentPage, NoticeText } from "../types/api";

const TOKEN = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_abcde";

function words(language: string): NoticeText {
  return {
    name: `name-${language}`,
    title: `title-${language}`,
    what: `what-${language}`,
    why: `why-${language}`,
    benefits_title: `benefits-${language}`,
    benefits: [`one-${language}`, `two-${language}`, `three-${language}`],
    real_time: `real-time-${language}`,
    retention: `retention-${language}`,
    no_change: `no-change-${language}`,
    change_mind: `change-mind-${language}`,
    agree: `agree-${language}`,
    refuse: `refuse-${language}`,
    answered_yes: `yes-${language}`,
    answered_no: `no-${language}`,
    not_answered: `not-yet-${language}`,
  };
}

const PAGE: ConsentPage = {
  company: "PepsiCo India (example)",
  shop: "Corner Daily Needs",
  decision: null,
  notice_version: 1,
  notice: { hi: words("hi"), en: words("en"), mr: words("mr") },
};

function open(get: () => Response, post?: ((body: unknown) => Response) | Array<(body: unknown) => Response>) {
  const api = fakeApi({
    [`GET /api/consent/${TOKEN}`]: get,
    ...(post === undefined ? {} : { [`POST /api/consent/${TOKEN}`]: post }),
  });
  const router = createMemoryRouter(routes, { initialEntries: [`/consent/${TOKEN}`] });
  render(<RouterProvider router={router} />);
  return api;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ConsentPage", () => {
  it("shows the company, the shop and the whole notice, in Hindi first", async () => {
    open(() => json(200, PAGE));

    expect(await screen.findByRole("heading", { level: 1, name: "title-hi" })).toBeInTheDocument();
    expect(screen.getByText("PepsiCo India (example) · Corner Daily Needs")).toBeInTheDocument();
    for (const part of ["what-hi", "why-hi", "real-time-hi", "retention-hi", "no-change-hi", "change-mind-hi"]) {
      expect(screen.getByText(part)).toBeInTheDocument();
    }
    expect(screen.getAllByRole("listitem").map((item) => item.textContent)).toEqual(["one-hi", "two-hi", "three-hi"]);
    expect(screen.getByRole("status")).toHaveTextContent("not-yet-hi");
    expect(screen.getByRole("main")).toHaveAttribute("lang", "hi");
  });

  it("reads in the language picked, and saves the answer in it", async () => {
    const api = open(
      () => json(200, PAGE),
      () => json(200, { ...PAGE, decision: "yes" }),
    );

    await userEvent.click(await screen.findByRole("button", { name: "name-mr" }));
    await userEvent.click(screen.getByRole("button", { name: "agree-mr" }));

    expect(await screen.findByText("yes-mr")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("lang", "mr");
    expect(screen.getByRole("button", { name: "name-mr" })).toHaveAttribute("aria-pressed", "true");
    expect(api.sent()).toEqual([
      { route: `POST /api/consent/${TOKEN}`, body: { decision: "yes", language: "mr" } },
    ]);
  });

  it("takes a no as easily as a yes, and shows the answer the shop gave", async () => {
    const api = open(
      () => json(200, { ...PAGE, decision: "yes" }),
      () => json(200, { ...PAGE, decision: "no" }),
    );

    await userEvent.click(await screen.findByRole("button", { name: "refuse-hi" }));

    expect(await screen.findByText("no-hi")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "refuse-hi" })).toHaveAttribute("aria-pressed", "true");
    expect(api.sent()).toEqual([
      { route: `POST /api/consent/${TOKEN}`, body: { decision: "no", language: "hi" } },
    ]);
  });

  it("says a link that isn't one isn't valid, in each language, and shows nothing of any shop", async () => {
    open(() => json(404, { error: { code: "not_found", message: "Not Found" } }));

    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("This link isn't valid.");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("sends the answer again from Try again", async () => {
    const api = open(
      () => json(200, PAGE),
      [
        () => json(503, { error: { code: "busy", message: "The server is busy." } }),
        () => json(200, { ...PAGE, decision: "yes" }),
      ],
    );

    await userEvent.click(await screen.findByRole("button", { name: "agree-hi" }));
    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));

    expect(await screen.findByText("yes-hi")).toBeInTheDocument();
    expect(api.sent().map((request) => request.body)).toEqual([
      { decision: "yes", language: "hi" },
      { decision: "yes", language: "hi" },
    ]);
  });

  it("says when an answer wasn't saved", async () => {
    open(
      () => json(200, PAGE),
      () => json(500, { error: { code: "unexpected_error", message: "Something went wrong on our side." } }),
    );

    await userEvent.click(await screen.findByRole("button", { name: "agree-hi" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on our side.");
    expect(screen.getByRole("status")).toHaveTextContent("not-yet-hi");
  });
});
