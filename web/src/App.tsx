import { Route, Routes } from "react-router";
import { HomePage } from "./pages/HomePage";
import { NotFoundPage } from "./pages/NotFoundPage";

export function App() {
  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      {/* The server answers every unknown path with the app, so the app must answer them too. */}
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  );
}
