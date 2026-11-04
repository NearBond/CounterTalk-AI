/**
 * Who is signed in (batch 16). Every page but /signin, /health and not-found sits inside
 * <SignedIn>: it asks GET /api/auth/me first, sends anyone signed out to /signin (and comes back
 * after), and gives the pages the user (useMe). A 401 later, the session ended, does the same.
 * The server decides what each role may see (countertalk/access.py); the app only hides what
 * a role can't use, and <Only> shows not-found for a page that isn't theirs.
 */
import { createContext, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router";
import { AppHeader } from "../components/AppHeader";
import { RetryAlert } from "../components/RetryAlert";
import { NotFoundPage } from "../pages/NotFoundPage";
import type { Me, Role } from "../types/api";
import { ApiError, getMe, UNEXPECTED_RESPONSE, whenSignedOut } from "./api";

const MeContext = createContext<Me | null>(null);

/** Who sees the whole org: they pick a team, and they have triage. */
export const WHOLE_ORG: Role[] = ["manager", "admin"];
/** Who works issues: the inbox, summaries and drill-down. */
export const TEAM_WORK: Role[] = ["team_member", ...WHOLE_ORG];
/** Who records visits. */
export const RECORDS: Role[] = ["rep"];
/** Who keeps the catalog (batch 16b). */
export const ADMINS: Role[] = ["admin"];
/** Whose home is the visits list: a rep's own, the whole org's for a manager or admin. */
export const VISITS: Role[] = [...RECORDS, ...WHOLE_ORG];

/** The header's pages, each with who uses it: App.tsx's gates read the same lists. */
export const PAGE_LINKS: Array<{ to: string; label: string; roles: Role[] }> = [
  // Batch F4: everyone's own home; a manager's or admin's visits list moved to /visits.
  { to: "/", label: "Home", roles: [...VISITS, "team_member"] },
  { to: "/visits", label: "Visits", roles: WHOLE_ORG },
  // Batch 17e: what got fixed for each shop, and the rep's own counts.
  { to: "/updates", label: "Updates", roles: RECORDS },
  { to: "/inbox", label: "Inbox", roles: TEAM_WORK },
  { to: "/inbox/triage", label: "Triage", roles: WHOLE_ORG },
  { to: "/summaries", label: "Summaries", roles: TEAM_WORK },
  { to: "/catalog", label: "Catalog", roles: ADMINS },
];

/** Each role as the profile names it (batch 17e). */
const POSITIONS: Record<Role, string> = {
  rep: "Sales rep",
  team_member: "Team member",
  manager: "Manager",
  admin: "Admin",
};

export function positionOf(me: Me): string {
  return POSITIONS[me.role];
}

export function wholeOrg(me: Me): boolean {
  return WHOLE_ORG.includes(me.role);
}

/** The signed-in user: only inside <SignedIn>. */
export function useMe(): Me {
  const me = useContext(MeContext);
  if (me === null) throw new Error("useMe is only for pages inside <SignedIn>");
  return me;
}

/** Where a role starts: its own home (batch F4), for every role. */
export function homeFor(_me: Me): string {
  return "/";
}

/** /signin, with where to come back to. */
export function signInPath(next: string): string {
  return next === "/" ? "/signin" : `/signin?next=${encodeURIComponent(next)}`;
}

type Loaded = { me: Me } | { error: ApiError } | null;

export function SignedIn() {
  const location = useLocation();
  const navigate = useNavigate();
  const here = `${location.pathname}${location.search}${location.hash}`;
  const where = useRef(here);
  useEffect(() => {
    where.current = here; // where a 401 later comes back to
  });
  const [loaded, setLoaded] = useState<Loaded>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getMe(controller.signal)
      .then((me) => setLoaded({ me }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({ error: error instanceof ApiError ? error : new ApiError(0, UNEXPECTED_RESPONSE, String(error)) });
      });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    whenSignedOut(() => navigate(signInPath(where.current), { replace: true }));
    return () => whenSignedOut(null);
  }, [navigate]);

  if (loaded === null) {
    return (
      <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
        <p role="status" className="text-gray-600">
          Loading…
        </p>
      </main>
    );
  }
  if ("error" in loaded) {
    if (loaded.error.status === 401) return <Navigate to={signInPath(here)} replace />;
    return (
      <main className="mx-auto flex min-h-screen w-full max-w-md flex-col gap-4 px-4 py-8">
        <h1 className="text-2xl font-semibold">CounterTalk</h1>
        <RetryAlert onRetry={() => setAttempt(attempt + 1)}>
          We couldn't check who's signed in: {loaded.error.message}
        </RetryAlert>
      </main>
    );
  }
  return (
    <MeContext.Provider value={loaded.me}>
      {/* The recorder owns its page: nothing may lead away mid-recording (RecordPage). */}
      {location.pathname !== "/record" && <AppHeader me={loaded.me} />}
      <Outlet />
    </MeContext.Provider>
  );
}

/** A page only these roles use: anyone else gets not-found, as the server would answer. */
export function Only({ roles, children }: { roles: Role[]; children: ReactNode }) {
  const me = useMe();
  return roles.includes(me.role) ? children : <NotFoundPage />;
}

/** `/`: the visits list for those who have one; anyone else goes to their own home. */
/** `/`: each role's own home (batches F2 and F4): a rep's, a manager's or admin's, or a team
 * member's. */
export function Home({ repHome, managerHome, teamHome }: { repHome: ReactNode; managerHome: ReactNode; teamHome: ReactNode }) {
  const me = useMe();
  if (RECORDS.includes(me.role)) return repHome;
  return WHOLE_ORG.includes(me.role) ? managerHome : teamHome;
}

/** Whether this user keeps the catalog (an admin): their home adds its card (batch F4). */
export function keepsCatalog(me: Me): boolean {
  return ADMINS.includes(me.role);
}

