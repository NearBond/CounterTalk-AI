import { Link, NavLink } from "react-router";
import { useSignOut } from "../lib/hooks";
import { PAGE_LINKS } from "../lib/session";
import type { Me } from "../types/api";
import { RetryAlert } from "./RetryAlert";

/** The pages' width, so the header lines up with them. */
const WIDTH = "mx-auto w-full max-w-md px-4 sm:max-w-3xl";

/** The mark's mask: one header on a page, so one id. */
const MARK_GAP = "app-header-mark-gap";
const FRONT_BUBBLE = "M25 16H37A7 7 0 0 1 44 23V39L36 34H25A7 7 0 0 1 18 27V23A7 7 0 0 1 25 16Z";

/** v2's mark: an ink speech bubble behind a marigold one with a voice in it. */
function BrandMark() {
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true" focusable="false" className="size-7 flex-none">
      <defs>
        <mask id={MARK_GAP} maskUnits="userSpaceOnUse" x="0" y="0" width="48" height="48">
          <rect width="48" height="48" fill="#fff" />
          <path d={FRONT_BUBBLE} fill="#000" stroke="#000" strokeWidth="5" strokeLinejoin="round" />
        </mask>
      </defs>
      <path
        className="fill-ink"
        mask={`url(#${MARK_GAP})`}
        d="M11 6H23A7 7 0 0 1 30 13V17A7 7 0 0 1 23 24H12L4 29V13A7 7 0 0 1 11 6Z"
      />
      <path className="fill-marigold" d={FRONT_BUBBLE} />
      <rect className="fill-ink" x="25.3" y="22" width="2.6" height="6" rx="1.3" />
      <rect className="fill-ink" x="29.7" y="19" width="2.6" height="12" rx="1.3" />
      <rect className="fill-ink" x="34.1" y="21" width="2.6" height="8" rx="1.3" />
      <rect className="fill-ink" x="4" y="42" width="40" height="3" rx="1.5" />
    </svg>
  );
}

/** A page is current on its sub-pages too, unless one of them has a link of its own (Triage
 * under Inbox, and every page under "/"). */
function endsHere(to: string): boolean {
  const under = to.endsWith("/") ? to : `${to}/`;
  return PAGE_LINKS.some((page) => page.to !== to && page.to.startsWith(under));
}

/** v2's nav link: ink-2, with a marigold line that grows in on hover and stays on the current page. */
function pageLink({ isActive }: { isActive: boolean }): string {
  const line =
    "after:absolute after:inset-x-0 after:bottom-1.5 after:h-0.5 after:origin-left after:rounded-full" +
    " after:bg-marigold after:transition-transform after:duration-250 after:ease-out motion-reduce:after:transition-none";
  const state = isActive ? "text-ink after:scale-x-100" : "text-ink-2 after:scale-x-0 hover:text-ink hover:after:scale-x-100";
  return `relative flex min-h-10 items-center text-sm font-medium transition-colors ${line} ${state}`;
}

function nameLink({ isActive }: { isActive: boolean }): string {
  return (
    "flex min-h-10 min-w-0 items-center text-sm leading-tight underline decoration-line-strong" +
    ` underline-offset-4 transition-colors hover:text-ink hover:decoration-ink-3 ${isActive ? "text-ink" : "text-ink-2"}`
  );
}

/** The signed-in pages' header in v2's look: the mark, their links, who's signed in (a link to
 * their profile, batch 17e), and Sign out. On a phone the links take a second row. */
export function AppHeader({ me }: { me: Me }) {
  const { leave, busy, failed } = useSignOut();
  const who = me.team === null ? me.name : `${me.name}, ${me.team.name}`;

  return (
    <header className="border-b border-line bg-paper">
      <div className={`${WIDTH} flex flex-wrap items-center gap-x-4 py-1 md:gap-x-8 md:py-2`}>
        <Link
          to="/"
          aria-label="CounterTalk home"
          className="flex min-h-11 shrink-0 items-center gap-2 rounded-md text-ink"
        >
          <BrandMark />
          <span className="font-display text-lg leading-none font-bold tracking-[-0.01em] md:text-xl">
            Counter<i>Talk</i>
          </span>
        </Link>
        <nav aria-label="Pages" className="order-last flex w-full flex-wrap gap-x-5 md:order-none md:w-auto md:gap-x-7">
          {PAGE_LINKS.filter((page) => page.roles.includes(me.role)).map(({ to, label }) => (
            <NavLink key={to} to={to} end={endsHere(to)} className={pageLink}>
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="flex min-w-0 flex-1 items-center justify-end gap-3 md:min-w-52">
          <NavLink to="/profile" className={nameLink} title={who}>
            <span className="block max-w-60 truncate">{who}</span>
          </NavLink>
          <button
            type="button"
            onClick={() => void leave()}
            disabled={busy}
            className="inline-flex min-h-10 shrink-0 cursor-pointer items-center rounded-[10px] border-[1.5px] border-ink px-3 text-sm leading-tight font-semibold whitespace-nowrap text-ink transition-colors enabled:hover:bg-card disabled:cursor-default disabled:opacity-50"
          >
            Sign out
          </button>
        </div>
      </div>
      {failed !== null && (
        <div className={`${WIDTH} pb-2`}>
          <RetryAlert small>{failed}</RetryAlert>
        </div>
      )}
    </header>
  );
}
