import { useEffect } from "react";
import { useNavigate } from "react-router";

import { useAuth } from "@/contexts/auth-context";
import { LoadingScreen } from "@/components/core/loading-screen";

export default function AuthSuccess() {
  const navigate = useNavigate();
  const { login } = useAuth();

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);

    const token = params.get("token");

    if (token) {
      login(token);
      navigate("/");
    } else {
      navigate("/auth/login");
    }
  }, [login, navigate]);

  return <LoadingScreen loadingText="Logging you in, please wait..." />;
}
