import type { RouteObject } from "react-router";
import { ADMINS, Home, Only, RECORDS, SignedIn, TEAM_WORK, WHOLE_ORG } from "./lib/session";
import { CatalogPage } from "./pages/CatalogPage";
import { DrillPage } from "./pages/DrillPage";
import { HealthPage } from "./pages/HealthPage";
import { InboxPage } from "./pages/InboxPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RecordPage } from "./pages/RecordPage";
import { SignInPage } from "./pages/SignInPage";
import { SummariesPage } from "./pages/SummariesPage";
import { TriagePage } from "./pages/TriagePage";
import { VisitPage } from "./pages/VisitPage";
import { VisitsPage } from "./pages/VisitsPage";

/** The app's pages. A data router (createBrowserRouter) serves them, so a page with unsaved
 * changes can ask before any navigation, the back button's included (batch 14). Every page but
 * sign-in, health and not-found needs a signed-in user (batch 16), and some only some roles. */
export const routes: RouteObject[] = [
  { path: "/signin", element: <SignInPage /> },
  { path: "/health", element: <HealthPage /> },
  {
    element: <SignedIn />,
    children: [
      { path: "/", element: <Home visits={<VisitsPage />} /> },
      {
        path: "/record",
        element: (
          <Only roles={RECORDS}>
            <RecordPage />
          </Only>
        ),
      },
      // Whose visit it is, the server says (a team member reads those routed to their team).
      { path: "/visits/:visitId", element: <VisitPage /> },
      {
        path: "/inbox",
        element: (
          <Only roles={TEAM_WORK}>
            <InboxPage />
          </Only>
        ),
      },
      {
        path: "/inbox/triage",
        element: (
          <Only roles={WHOLE_ORG}>
            <TriagePage />
          </Only>
        ),
      },
      {
        path: "/summaries",
        element: (
          <Only roles={TEAM_WORK}>
            <SummariesPage />
          </Only>
        ),
      },
      {
        path: "/summaries/drill",
        element: (
          <Only roles={TEAM_WORK}>
            <DrillPage />
          </Only>
        ),
      },
      {
        path: "/catalog",
        element: (
          <Only roles={ADMINS}>
            <CatalogPage />
          </Only>
        ),
      },
    ],
  },
  // The server answers every unknown path with the app, so the app must answer them too.
  { path: "*", element: <NotFoundPage /> },
];
