import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { Login } from "@/features/auth/components/login";
import { NotFound } from "@/features/errors/not-found";
import { Register } from "@/features/auth/components/register";

import AuthLayout from "@/features/auth/auth-layout";
import AuthSuccess from "@/features/auth/auth-success";
import DashboardLayout from "@/features/dashboard/dashboard-layout";
import ProtectedRoute from "@/features/auth/protected-route";
import RepositoriesPage from "@/features/dashboard/pages/repositories";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Public auth routes */}
        <Route path="/auth" element={<AuthLayout />}>
          <Route path="sign-in" element={<Login />} />
          <Route path="sign-up" element={<Register />} />
          <Route path="success" element={<AuthSuccess />} />
        </Route>

        {/* Protected routes */}
        <Route element={<ProtectedRoute />}>
          {/* Default home route */}
          <Route path="/" element={<Navigate to="/repositories" replace />} />
          {/* Dashboard routes */}
          <Route element={<DashboardLayout />}>
            <Route path="/repositories" element={<RepositoriesPage />} />
          </Route>
        </Route>

        {/* Catch-all route */}
        <Route path="*" element={<NotFound />} />
      </Routes>
    </BrowserRouter>
  );
}
