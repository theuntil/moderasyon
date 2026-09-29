import { Navigate, Route, Routes } from "react-router-dom";

import { AuthProvider, useAuth } from "./auth";
import { Layout } from "./components/Layout";
import { Spinner } from "./components/ui";
import { can } from "./format";
import { AccountPage, ForcedPasswordChange } from "./pages/Account";
import { AdminsPage } from "./pages/Admins";
import { AiPage } from "./pages/Ai";
import { AuditPage } from "./pages/Audit";
import { LegalHoldsPage } from "./pages/LegalHolds";
import { RulesPage } from "./pages/Rules";
import { BlocklistPage } from "./pages/Blocklist";
import { DashboardPage } from "./pages/Dashboard";
import { DecisionsPage } from "./pages/Decisions";
import { IpRulesPage } from "./pages/IpRules";
import { LoginPage } from "./pages/Login";
import { ProjectDetailPage } from "./pages/ProjectDetail";
import { ProjectsPage } from "./pages/Projects";
import { QualityPage } from "./pages/Quality";
import { ReviewPage } from "./pages/Review";
import { SettingsPage } from "./pages/Settings";
import { SystemPage } from "./pages/System";
import type { Role } from "./types";

function Guarded({ min, children }: { min: Role; children: React.ReactNode }) {
  const { user } = useAuth();
  return can(user?.role, min) ? <>{children}</> : <Navigate to="/" replace />;
}

function Shell() {
  const { user, loading } = useAuth();
  if (loading) return <Spinner label="Oturum kontrol ediliyor" />;
  if (!user) return <LoginPage />;
  if (user.must_change_password) return <ForcedPasswordChange />;

  return (
    <Layout>
      <Routes>
        <Route path="/" element={<DashboardPage />} />
        <Route path="/review" element={<Guarded min="moderator"><ReviewPage /></Guarded>} />
        <Route path="/decisions" element={<DecisionsPage />} />
        <Route path="/blocklist" element={<Guarded min="moderator"><BlocklistPage /></Guarded>} />
        <Route path="/quality" element={<QualityPage />} />
        <Route path="/legal-holds" element={<Guarded min="moderator"><LegalHoldsPage /></Guarded>} />
        <Route path="/rules" element={<RulesPage />} />
        <Route path="/projects" element={<ProjectsPage />} />
        <Route path="/projects/:id" element={<ProjectDetailPage />} />
        <Route path="/ip-rules" element={<Guarded min="admin"><IpRulesPage /></Guarded>} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/admins" element={<Guarded min="admin"><AdminsPage /></Guarded>} />
        <Route path="/audit" element={<Guarded min="admin"><AuditPage /></Guarded>} />
        <Route path="/system" element={<SystemPage />} />
        <Route path="/ai" element={<AiPage />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Layout>
  );
}

export function App() {
  return (
    <AuthProvider>
      <Shell />
    </AuthProvider>
  );
}
