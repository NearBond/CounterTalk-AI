import { Link } from "react-router";
import { useSignOut } from "../lib/hooks";
import { PAGE_LINKS } from "../lib/session";
import type { Me } from "../types/api";
import { RetryAlert } from "./RetryAlert";

/** The signed-in pages' header: their links, who's signed in (a link to their profile, batch
 * 17e), and Sign out. */
export function AppHeader({ me }: { me: Me }) {
  const { leave, busy, failed } = useSignOut();

  return (
    <header className="mx-auto flex w-full max-w-md flex-col gap-2 px-4 pt-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <nav aria-label="Pages" className="flex flex-wrap gap-4">
          {PAGE_LINKS.filter((page) => page.roles.includes(me.role)).map(({ to, label }) => (
            <Link key={to} to={to} className="text-blue-700 underline">
              {label}
            </Link>
          ))}
        </nav>
        <Link to="/profile" className="ml-auto text-sm text-gray-700 underline">
          {me.team === null ? me.name : `${me.name}, ${me.team.name}`}
        </Link>
        <button
          type="button"
          onClick={() => void leave()}
          disabled={busy}
          className="rounded-md border border-gray-300 px-3 py-1 text-sm disabled:opacity-50"
        >
          Sign out
        </button>
      </div>
      {failed !== null && <RetryAlert small>{failed}</RetryAlert>}
    </header>
  );
}
