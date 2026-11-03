import { Fragment } from "react";
import { RetryAlert } from "../components/RetryAlert";
import { useSignOut } from "../lib/hooks";
import { positionOf, useMe } from "../lib/session";

/** `/profile`, everyone's own (batch 17e; the user, 3 Nov), from the name in the header: who's
 * signed in, as GET /api/auth/me says, read-only, and Sign out. */
export function ProfilePage() {
  const me = useMe();
  const { leave, busy, failed } = useSignOut();
  const details: Array<[string, string]> = [
    ["Name", me.name],
    ["Email", me.email],
    ["Company", me.org_name],
    ["Position", positionOf(me)],
    ...(me.team === null ? [] : [["Team", me.team.name] as [string, string]]),
  ];

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
      <h1 className="text-2xl font-semibold">Profile</h1>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2">
        {details.map(([term, value]) => (
          <Fragment key={term}>
            <dt className="text-gray-600">{term}</dt>
            <dd className="min-w-0 [overflow-wrap:anywhere]">{value}</dd>
          </Fragment>
        ))}
      </dl>
      <button
        type="button"
        onClick={() => void leave()}
        disabled={busy}
        className="self-start rounded-md border border-gray-300 px-4 py-2 disabled:opacity-50"
      >
        Sign out
      </button>
      {failed !== null && <RetryAlert small>{failed}</RetryAlert>}
    </main>
  );
}
