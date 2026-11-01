import { Route, Routes } from "react-router";
import { HomePage } from "./pages/HomePage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { RecordPage } from "./pages/RecordPage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/record" element={<RecordPage />} />
      {/* The server answers every unknown path with the app, so the app must answer them too. */}
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
