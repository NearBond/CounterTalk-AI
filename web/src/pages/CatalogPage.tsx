import { type FormEvent, useState } from "react";
import { ChoiceSelect } from "../components/ChoiceSelect";
import { RetryAlert } from "../components/RetryAlert";
import { SaveError } from "../components/SaveError";
import {
  addAlias,
  addCatalogItem,
  ApiError,
  changeCatalogItem,
  listCatalog,
  listNameReviews,
  removeAlias,
  resolveName,
} from "../lib/api";
import { countOf } from "../lib/format";
import { usePolling, useSave } from "../lib/hooks";
import type { CatalogItem, CatalogKind, CatalogNames, NameReview } from "../types/api";

const KINDS: Array<{ key: CatalogKind; label: string }> = [
  { key: "own", label: "Ours" },
  { key: "competitor", label: "A competitor's" },
];
const KIND_LABEL: Record<CatalogKind, string> = { own: "Ours", competitor: "Competitor" };
const INPUT = "w-full min-w-0 rounded-md border border-gray-300 p-2";
const BUTTON = "rounded-md bg-blue-700 px-3 py-1.5 text-white disabled:opacity-50";
const QUIET = "rounded-md border border-gray-300 px-3 py-1.5 disabled:opacity-50";
const NO_NAMES: CatalogNames = { company: "", brand: "", product: "" };

function nameOf(item: CatalogItem): string {
  return item.product === null ? item.brand : `${item.brand} ${item.product}`;
}

function named(names: CatalogNames): boolean {
  return names.company.trim() !== "" && names.brand.trim() !== "";
}

/** The names as sent: an empty product is the brand as a whole. */
function sent(names: CatalogNames): CatalogNames {
  return { ...names, product: names.product?.trim() || null };
}

/** `/catalog` (batch 16b): an admin keeps the org's own products and its competitors' up to date
 * here, instead of in the org pack's files. New visits use a change at once; past insights keep
 * their links. Items are deactivated, never deleted, as insights point at them. */
export function CatalogPage() {
  const [search, setSearch] = useState("");
  const [asked, setAsked] = useState("");
  const [kind, setKind] = useState<CatalogKind | "">("");
  const filtered = asked.trim() !== "" || kind !== "";
  const items = usePolling((signal) => listCatalog(asked, kind, signal), {
    key: JSON.stringify([asked, kind]),
    shouldPoll: () => false,
  });
  // Every item, for the review queue's Attach: the list itself when it isn't filtered.
  const every = usePolling((signal) => listCatalog("", "", signal), {
    key: "every",
    shouldPoll: () => false,
    skip: !filtered,
  });
  const all = filtered ? every : items;
  const reviews = usePolling((signal) => listNameReviews(signal), { key: "reviews", shouldPoll: () => false });
  const changed = () => {
    items.reload();
    if (filtered) every.reload();
    reviews.reload();
  };
  const find = (event: FormEvent) => {
    event.preventDefault();
    setAsked(search);
  };

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-5 px-4 py-8">
      <h1 className="text-2xl font-semibold">Catalog</h1>

      <section aria-labelledby="reviews-heading" className="flex flex-col gap-2">
        <h2 id="reviews-heading" className="text-lg font-semibold">
          Names to review
        </h2>
        <p className="text-sm text-gray-600">Names a visit mentioned that matched no item, or two.</p>
        {reviews.error !== null && (
          <RetryAlert onRetry={reviews.reload}>The names couldn't be loaded: {reviews.error.message}</RetryAlert>
        )}
        {filtered && every.error !== null && (
          <RetryAlert onRetry={every.reload}>The items to attach to couldn't be loaded: {every.error.message}</RetryAlert>
        )}
        {reviews.data !== undefined && reviews.data.length === 0 && <p className="text-gray-600">None waiting.</p>}
        {reviews.data !== undefined && all.data !== undefined && reviews.data.length > 0 && (
          <ul aria-label="Names to review" className="flex flex-col gap-3">
            {reviews.data.map((review) => (
              <ReviewRow key={review.id} review={review} items={all.data?.items ?? []} onResolved={changed} />
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="items-heading" className="flex flex-col gap-3">
        <h2 id="items-heading" className="text-lg font-semibold">
          Items
        </h2>
        <form onSubmit={find} className="flex items-end gap-2">
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
            Search
            <input type="search" value={search} onChange={(e) => setSearch(e.target.value)} className={INPUT} />
          </label>
          <button type="submit" className={QUIET}>
            Search
          </button>
        </form>
        <ChoiceSelect
          label="Kind"
          choices={[{ key: "", label: "All" }, ...KINDS]}
          value={kind}
          onChange={(value) => setKind(value as CatalogKind | "")}
        />
        <AddItem onAdded={changed} />
        {items.error !== null && (
          <RetryAlert onRetry={items.reload}>The catalog couldn't be loaded: {items.error.message}</RetryAlert>
        )}
        {items.data !== undefined && items.data.items.length === 0 && <p className="text-gray-600">No items here.</p>}
        {items.data !== undefined && items.data.items.length > 0 && (
          <ul aria-label="Catalog items" className="flex flex-col gap-3">
            {items.data.items.map((item) => (
              <ItemCard key={item.id} item={item} onChanged={changed} />
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

function NamesFields({ names, onChange }: { names: CatalogNames; onChange: (names: CatalogNames) => void }) {
  return (
    <div className="grid grid-cols-1 gap-2">
      <label className="flex flex-col gap-1 text-sm">
        Company
        <input value={names.company} onChange={(e) => onChange({ ...names, company: e.target.value })} className={INPUT} />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Brand
        <input value={names.brand} onChange={(e) => onChange({ ...names, brand: e.target.value })} className={INPUT} />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Product (empty for the brand as a whole)
        <input
          value={names.product ?? ""}
          onChange={(e) => onChange({ ...names, product: e.target.value })}
          className={INPUT}
        />
      </label>
    </div>
  );
}

/** Add an item: the form is shown only while open, so Cancel forgets what was typed. */
function AddItem({ onAdded }: { onAdded: () => void }) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} className={`${QUIET} self-start`}>
        Add an item
      </button>
    );
  }
  return (
    <AddItemForm
      onClose={() => setOpen(false)}
      onAdded={() => {
        setOpen(false);
        onAdded();
      }}
    />
  );
}

function AddItemForm({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const [kind, setKind] = useState<CatalogKind>("competitor");
  const [names, setNames] = useState<CatalogNames>(NO_NAMES);
  const save = useSave();
  const add = () => save.run((signal) => addCatalogItem({ kind, ...sent(names) }, signal), onAdded);
  return (
    <section aria-label="Add an item" className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
      <ChoiceSelect label="Whose" choices={KINDS} value={kind} onChange={(value) => setKind(value as CatalogKind)} />
      <NamesFields names={names} onChange={setNames} />
      <SaveError error={save.error} onRetry={save.retry} />
      <div className="flex gap-2">
        <button type="button" onClick={add} disabled={save.saving || !named(names)} className={BUTTON}>
          {save.saving ? "Adding…" : "Add it"}
        </button>
        <button type="button" onClick={onClose} className={QUIET}>
          Cancel
        </button>
      </div>
    </section>
  );
}

function ItemCard({ item, onChanged }: { item: CatalogItem; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const [alias, setAlias] = useState("");
  const save = useSave();
  const stale = save.error instanceof ApiError && save.error.code === "item_changed";

  return (
    <li className={`flex flex-col gap-2 rounded-lg border p-3 ${item.active ? "border-gray-200" : "border-dashed border-gray-300 text-gray-600"}`}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="font-medium">{nameOf(item)}</span>
        <span className="text-sm text-gray-600">
          {item.company} · {KIND_LABEL[item.kind]}
          {!item.active && " · Deactivated: never linked, nor listened for"}
        </span>
      </div>
      {item.aliases.length > 0 && (
        <ul aria-label={`${nameOf(item)}'s other names`} className="flex flex-wrap gap-2">
          {item.aliases.map((each) => (
            <li key={each.id} className="flex items-center gap-1 rounded bg-gray-100 px-2 py-0.5 text-sm">
              {each.text}
              <button
                type="button"
                aria-label={`Remove ${each.text}`}
                disabled={save.saving}
                onClick={() => save.run((signal) => removeAlias(each.id, signal), onChanged)}
                className="text-gray-600 disabled:opacity-50"
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}
      {stale && <RetryAlert onRetry={onChanged}>Someone changed this item since you opened it: reload it.</RetryAlert>}
      {!stale && <SaveError error={save.error} onRetry={save.retry} />}
      {editing ? (
        <EditNames
          item={item}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
        />
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setEditing(true)} className={QUIET}>
            Edit
          </button>
          <button
            type="button"
            disabled={save.saving}
            onClick={() =>
              save.run((signal) => changeCatalogItem(item.id, { version: item.version, active: !item.active }, signal), onChanged)
            }
            className={QUIET}
          >
            {item.active ? "Deactivate" : "Reactivate"}
          </button>
        </div>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.run(
            (signal) => addAlias(item.id, { text: alias }, signal),
            () => {
              setAlias("");
              onChanged();
            },
          );
        }}
        className="flex items-end gap-2"
      >
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
          Another name, in any script
          <input value={alias} onChange={(e) => setAlias(e.target.value)} className={INPUT} />
        </label>
        <button type="submit" disabled={save.saving || !alias.trim()} className={QUIET}>
          Add
        </button>
      </form>
    </li>
  );
}

/** An item's names, edited on the version read: shown only while editing, so Cancel forgets
 * them, and a reload after someone else's change keeps what was typed. */
function EditNames({ item, onClose, onSaved }: { item: CatalogItem; onClose: () => void; onSaved: () => void }) {
  const [names, setNames] = useState<CatalogNames>({ company: item.company, brand: item.brand, product: item.product ?? "" });
  const save = useSave();
  const stale = save.error instanceof ApiError && save.error.code === "item_changed";
  return (
    <>
      <NamesFields names={names} onChange={setNames} />
      {stale ? (
        <p className="text-sm text-amber-800">
          Someone changed this item meanwhile: check its names above, then save again.
        </p>
      ) : (
        <SaveError error={save.error} onRetry={save.retry} />
      )}
      <div className="flex gap-2">
        <button
          type="button"
          disabled={save.saving || !named(names)}
          onClick={() => save.run((signal) => changeCatalogItem(item.id, { version: item.version, ...sent(names) }, signal), onSaved)}
          className={BUTTON}
        >
          Save
        </button>
        <button type="button" onClick={onClose} className={QUIET}>
          Cancel
        </button>
      </div>
    </>
  );
}

function ReviewRow({ review, items, onResolved }: { review: NameReview; items: CatalogItem[]; onResolved: () => void }) {
  const choices = items
    .filter((item) => item.kind === review.kind && item.active)
    .map((item) => ({ key: item.id, label: nameOf(item) }));
  const [picked, setPicked] = useState("");
  // What's picked, while it's still one of the choices; else the first.
  const target = choices.some((choice) => choice.key === picked) ? picked : (choices[0]?.key ?? "");
  const [creating, setCreating] = useState(false);
  const [names, setNames] = useState<CatalogNames>({ ...NO_NAMES, brand: review.text });
  const save = useSave();
  const resolve = (body: Parameters<typeof resolveName>[1]) =>
    save.run((signal) => resolveName(review.id, body, signal), onResolved);

  return (
    <li className="flex flex-col gap-2 rounded-lg border border-gray-200 p-3">
      <p>
        <span className="font-medium">{review.text}</span>{" "}
        <span className="text-sm text-gray-600">
          · {KIND_LABEL[review.kind]} · said in {countOf(review.visit_ids.length, "visit", "visits")}
        </span>
      </p>
      <SaveError error={save.error} onRetry={save.retry} />
      {choices.length > 0 && (
        <div className="flex items-end gap-2">
          <div className="min-w-0 flex-1">
            <ChoiceSelect label="It's another name of" choices={choices} value={target} onChange={setPicked} />
          </div>
          <button type="button" disabled={save.saving || target === ""} onClick={() => resolve({ action: "attach", item_id: target })} className={BUTTON}>
            Attach
          </button>
        </div>
      )}
      {creating ? (
        <>
          <NamesFields names={names} onChange={setNames} />
          <div className="flex gap-2">
            <button
              type="button"
              disabled={save.saving || !named(names)}
              onClick={() => resolve({ action: "create", item: sent(names) })}
              className={BUTTON}
            >
              Create the item
            </button>
            <button type="button" onClick={() => setCreating(false)} className={QUIET}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => setCreating(true)} className={QUIET}>
            Create an item
          </button>
          <button type="button" disabled={save.saving} onClick={() => resolve({ action: "dismiss" })} className={QUIET}>
            Dismiss
          </button>
        </div>
      )}
    </li>
  );
}
