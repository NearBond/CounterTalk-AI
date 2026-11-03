import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeApi, jsonResponse as json } from "../testUtils";
import type { Shop } from "../types/api";
import { ConsentNeeded } from "./ConsentNeeded";

const SHOP: Shop = { id: "s1", name: "Corner Daily Needs", area: "Station Road", do_not_record: false, consent: null };
const LINK = { path: "/consent/tok", message: "नमस्ते!\nHello!\nनमस्कार!" };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ConsentNeeded", () => {
  it("shares a consent link on WhatsApp with the message, a line per language", async () => {
    fakeApi({ "POST /api/shops/s1/consent-links": () => json(201, LINK) });
    vi.stubGlobal("navigator", { ...navigator, share: undefined });
    render(<ConsentNeeded shop={SHOP} onAnswered={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "Share consent link" }));

    const url = `${window.location.origin}/consent/tok`;
    const whatsapp = await screen.findByRole("link", { name: "Send it on WhatsApp" });
    expect(whatsapp).toHaveAttribute("href", `https://wa.me/?text=${encodeURIComponent(`${LINK.message}\n${url}`)}`);
    expect(screen.getByText(url)).toBeInTheDocument();
  });

  it("asks for the shop's answer again once the link is shared", async () => {
    fakeApi({ "POST /api/shops/s1/consent-links": () => json(201, LINK) });
    vi.stubGlobal("navigator", { ...navigator, share: undefined });
    const answered = vi.fn();
    render(<ConsentNeeded shop={SHOP} onAnswered={answered} />);

    await userEvent.click(screen.getByRole("button", { name: "Share consent link" }));
    await userEvent.click(await screen.findByRole("button", { name: "Check for the shop's answer" }));

    expect(answered).toHaveBeenCalledOnce();
  });

  it("opens the phone's share sheet where there is one", async () => {
    fakeApi({ "POST /api/shops/s1/consent-links": () => json(201, LINK) });
    const share = vi.fn(async () => {});
    vi.stubGlobal("navigator", { ...navigator, share });
    render(<ConsentNeeded shop={SHOP} onAnswered={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "Share consent link" }));

    await vi.waitFor(() =>
      expect(share).toHaveBeenCalledWith({ text: `${LINK.message}\n${window.location.origin}/consent/tok` }),
    );
  });

  it("marks the paper form in the language it was signed in", async () => {
    const api = fakeApi({ "POST /api/shops/s1/consent": () => json(201, { consent: "yes" }) });
    const answered = vi.fn();
    render(<ConsentNeeded shop={SHOP} onAnswered={answered} />);

    await userEvent.click(screen.getByRole("button", { name: "Signed on paper" }));
    await userEvent.selectOptions(screen.getByLabelText("The form's language"), "mr");
    await userEvent.click(screen.getByRole("button", { name: "Mark the signed form" }));

    await vi.waitFor(() => expect(answered).toHaveBeenCalledOnce());
    expect(api.sent()).toEqual([{ route: "POST /api/shops/s1/consent", body: { language: "mr" } }]);
  });

  it("offers only the link to a shop that said no on its page", () => {
    render(<ConsentNeeded shop={{ ...SHOP, consent: "no" }} onAnswered={() => {}} />);

    expect(screen.getByText(/said no to recording on its consent page/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Share consent link" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Signed on paper" })).not.toBeInTheDocument();
  });

  it("says when the link couldn't be made", async () => {
    fakeApi({
      "POST /api/shops/s1/consent-links": () => json(500, { error: { code: "unexpected_error", message: "Something went wrong." } }),
    });
    render(<ConsentNeeded shop={SHOP} onAnswered={() => {}} />);

    await userEvent.click(screen.getByRole("button", { name: "Share consent link" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong.");
  });
});
