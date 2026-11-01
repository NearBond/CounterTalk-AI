import type { RouteObject } from "react-router";
import { HealthPage } from "./pages/HealthPage";
import { InboxPage } from "./pages/InboxPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RecordPage } from "./pages/RecordPage";
import { TriagePage } from "./pages/TriagePage";
import { VisitPage } from "./pages/VisitPage";
import { VisitsPage } from "./pages/VisitsPage";

/** The app's pages. A data router (createBrowserRouter) serves them, so a page with unsaved
 * changes can ask before any navigation, the back button's included (batch 14). */
export const routes: RouteObject[] = [
  { path: "/", element: <VisitsPage /> },
  { path: "/record", element: <RecordPage /> },
  { path: "/visits/:visitId", element: <VisitPage /> },
  { path: "/inbox", element: <InboxPage /> },
  { path: "/inbox/triage", element: <TriagePage /> },
  { path: "/health", element: <HealthPage /> },
  // The server answers every unknown path with the app, so the app must answer them too.
  { path: "*", element: <NotFoundPage /> },
];
