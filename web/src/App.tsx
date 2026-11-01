import { Route, Routes } from "react-router";
import { HealthPage } from "./pages/HealthPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RecordPage } from "./pages/RecordPage";
import { VisitPage } from "./pages/VisitPage";
import { VisitsPage } from "./pages/VisitsPage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<VisitsPage />} />
      <Route path="/record" element={<RecordPage />} />
      <Route path="/visits/:visitId" element={<VisitPage />} />
      <Route path="/health" element={<HealthPage />} />
      {/* The server answers every unknown path with the app, so the app must answer them too. */}
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
