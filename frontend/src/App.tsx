import { BrowserRouter, Navigate, Route, Routes } from "react-router";

import { Login } from "@/features/auth/components/login";
import { NotFound } from "@/features/errors/not-found";
import { Register } from "@/features/auth/components/register";

import AuthLayout from "@/features/auth/auth-layout";
import AuthSuccess from "@/features/auth/auth-success";
import Dashboard from "@/features/dashboard/page";
import ProtectedRoute from "@/features/auth/protected-route";

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
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="/dashboard" element={<Dashboard />} />
        </Route>

        <Route path="*" element={<NotFound />} />
      </Routes>
    </BrowserRouter>
  );
}
