import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import { routes } from "../App";
import { ADMIN, fakeApi, type Handler, jsonResponse as json } from "../testUtils";
import type { CatalogItem, NameReview } from "../types/api";

const RIVAL: CatalogItem = {
  id: "i-rival",
  key: "rival",
  kind: "competitor",
  company: "Rival Foods",
  brand: "Rival",
  product: null,
  active: true,
  version: 3,
  aliases: [{ id: "a-1", text: "राइवल", source: "pack" }],
};
const CRUNCHY: CatalogItem = { ...RIVAL, id: "i-crunchy", key: "crunchy", kind: "own", company: "Tiny Snacks", brand: "Crunchy", aliases: [] };
const RAIVAL: NameReview = { id: "r-1", kind: "competitor", text: "Raival", visit_ids: ["v-1", "v-2"] };
const PAGE = { items: [CRUNCHY, RIVAL], next_cursor: null };

function open(routesByCall: Record<string, Handler | Handler[]> = {}) {
  const api = fakeApi({
    "GET /api/auth/me": () => json(200, ADMIN),
    "GET /api/catalog/items": () => json(200, PAGE),
    "GET /api/catalog/reviews": () => json(200, [RAIVAL]),
    ...routesByCall,
  });
  render(<RouterProvider router={createMemoryRouter(routes, { initialEntries: ["/catalog"] })} />);
  return api;
}

async function card(name: string) {
  const list = await screen.findByRole("list", { name: "Catalog items" });
  const found = within(list).getAllByRole("listitem").find((item) => item.textContent?.startsWith(name));
  if (found === undefined) throw new Error(`no card for ${name}`);
  return within(found);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("CatalogPage", () => {
  it("shows the names to review and the items, deactivated ones marked", async () => {
    open({ "GET /api/catalog/items": () => json(200, { ...PAGE, items: [CRUNCHY, { ...RIVAL, active: false }] }) });

    const names = await screen.findByRole("list", { name: "Names to review" });
    expect(names).toHaveTextContent("Raival · Competitor · said in 2 visits");
    expect((await card("Rival")).getByText(/Deactivated: never linked, nor listened for/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Catalog" })).toBeInTheDocument();
  });

  it("attaches a name to an item of its kind", async () => {
    const api = open({ "POST /api/catalog/reviews/r-1/resolve": () => json(200, { item: RIVAL }) });

    const names = within(await screen.findByRole("list", { name: "Names to review" }));
    // Only the competitor's items: the name was said as one.
    expect(names.getByLabelText("It's another name of")).toHaveTextContent("Rival");
    expect(names.getByLabelText("It's another name of")).not.toHaveTextContent("Crunchy");
    await userEvent.click(names.getByRole("button", { name: "Attach" }));

    await vi.waitFor(() =>
      expect(api.sent()).toEqual([{ route: "POST /api/catalog/reviews/r-1/resolve", body: { action: "attach", item_id: "i-rival" } }]),
    );
  });

  it("deactivates an item on the version it read", async () => {
    const api = open({ "PATCH /api/catalog/items/i-rival": () => json(200, { ...RIVAL, active: false, version: 4 }) });

    await userEvent.click((await card("Rival")).getByRole("button", { name: "Deactivate" }));

    await vi.waitFor(() =>
      expect(api.sent()).toEqual([{ route: "PATCH /api/catalog/items/i-rival", body: { version: 3, active: false } }]),
    );
  });

  it("says so when someone changed the item meanwhile, and reloads", async () => {
    const stale = { error: { code: "item_changed", message: "Someone changed this item since you opened it: reload it" } };
    open({ "PATCH /api/catalog/items/i-rival": () => json(409, stale) });

    await userEvent.click((await card("Rival")).getByRole("button", { name: "Deactivate" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Someone changed this item since you opened it: reload it.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument(); // reloads the list
  });

  it("adds and removes another name", async () => {
    const api = open({
      "POST /api/catalog/items/i-rival/aliases": () => json(201, RIVAL),
      "DELETE /api/catalog/aliases/a-1": () => new Response(null, { status: 204 }),
    });
    const rival = await card("Rival");

    await userEvent.type(rival.getByLabelText("Another name, in any script"), "Raival");
    await userEvent.click(rival.getByRole("button", { name: "Add" }));
    await userEvent.click((await card("Rival")).getByRole("button", { name: "Remove राइवल" }));

    await vi.waitFor(() =>
      expect(api.sent()).toEqual([
        { route: "POST /api/catalog/items/i-rival/aliases", body: { text: "Raival" } },
        { route: "DELETE /api/catalog/aliases/a-1", body: undefined },
      ]),
    );
  });

  it("adds an item, a brand as a whole without a product", async () => {
    const api = open({ "POST /api/catalog/items": () => json(201, { ...RIVAL, id: "i-zorb", brand: "Zorb" }) });

    await userEvent.click(await screen.findByRole("button", { name: "Add an item" }));
    const form = within(screen.getByRole("region", { name: "Add an item" }));
    await userEvent.type(form.getByLabelText("Company"), "Zorb Co");
    await userEvent.type(form.getByLabelText("Brand"), "Zorb");
    await userEvent.click(form.getByRole("button", { name: "Add it" }));

    await vi.waitFor(() =>
      expect(api.sent()).toEqual([
        { route: "POST /api/catalog/items", body: { kind: "competitor", company: "Zorb Co", brand: "Zorb", product: null } },
      ]),
    );
  });

  it("keeps an edit when someone else changed the item, to check and save again", async () => {
    const stale = { error: { code: "item_changed", message: "Someone changed this item since you opened it: reload it" } };
    open({ "PATCH /api/catalog/items/i-rival": () => json(409, stale) });
    const rival = await card("Rival");

    await userEvent.click(rival.getByRole("button", { name: "Edit" }));
    await userEvent.clear(rival.getByLabelText("Brand"));
    await userEvent.type(rival.getByLabelText("Brand"), "Rival Max");
    await userEvent.click(rival.getByRole("button", { name: "Save" }));

    expect(await rival.findByText(/Someone changed this item meanwhile/)).toBeInTheDocument();
    expect(rival.getByLabelText("Brand")).toHaveValue("Rival Max");
  });

  it("forgets what was typed on Cancel, and can't save a blank name", async () => {
    open();
    const rival = await card("Rival");

    await userEvent.click(rival.getByRole("button", { name: "Edit" }));
    await userEvent.clear(rival.getByLabelText("Company"));
    expect(rival.getByRole("button", { name: "Save" })).toBeDisabled();
    await userEvent.click(rival.getByRole("button", { name: "Cancel" }));
    await userEvent.click(rival.getByRole("button", { name: "Edit" }));

    expect(rival.getByLabelText("Company")).toHaveValue("Rival Foods");
  });

  it("attaches to an item that's still a choice once the list changes", async () => {
    const zorb: CatalogItem = { ...RIVAL, id: "i-zorb", brand: "Zorb", aliases: [] };
    const api = open({
      "GET /api/catalog/items": [() => json(200, PAGE), () => json(200, { ...PAGE, items: [CRUNCHY, { ...RIVAL, active: false }, zorb] })],
      "PATCH /api/catalog/items/i-rival": () => json(200, { ...RIVAL, active: false, version: 4 }),
      "POST /api/catalog/reviews/r-1/resolve": () => json(200, { item: zorb }),
    });

    await userEvent.click((await card("Rival")).getByRole("button", { name: "Deactivate" }));
    const names = within(await screen.findByRole("list", { name: "Names to review" }));
    await vi.waitFor(() => expect(names.getByLabelText("It's another name of")).toHaveValue("i-zorb"));
    await userEvent.click(names.getByRole("button", { name: "Attach" }));

    await vi.waitFor(() =>
      expect(api.sent().at(-1)).toEqual({
        route: "POST /api/catalog/reviews/r-1/resolve",
        body: { action: "attach", item_id: "i-zorb" },
      }),
    );
  });
});

