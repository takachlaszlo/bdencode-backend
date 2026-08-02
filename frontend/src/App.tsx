import { Navigate, Route, Routes } from "react-router";
import { Layout } from "./components/Layout";
import { DashboardPage } from "./pages/DashboardPage";
import { JobsPage } from "./pages/JobsPage";
import { NewEncodePage } from "./pages/NewEncodePage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { ComparisonsPage } from "./pages/ComparisonsPage";
import { SystemPage } from "./pages/SystemPage";

export function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="new" element={<NewEncodePage />} />
        <Route path="queue" element={<JobsPage mode="queue" />} />
        <Route path="archive" element={<JobsPage mode="archive" />} />
        <Route path="jobs/:jobId" element={<JobDetailPage />} />
        <Route path="comparisons" element={<ComparisonsPage />} />
        <Route path="settings" element={<SystemPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
