import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import AuthLayout from "@/features/auth/auth-layout";
import { Login } from "@/features/auth/components/login";
import { Register } from "@/features/auth/components/register";
import AuthSuccess from "@/features/auth/auth-success";
import ProtectedRoute from "@/features/auth/protected-route";
import Dashboard from "@/features/dashboard/page";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/auth" element={<AuthLayout />}>
          <Route path="sign-in" element={<Login />} />
          <Route path="sign-up" element={<Register />} />
          <Route path="success" element={<AuthSuccess />} />
        </Route>

        {/* Protected routes */}
        <Route element={<ProtectedRoute />}>
          <Route path="/dashboard" element={<Dashboard />} />
        </Route>

        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
