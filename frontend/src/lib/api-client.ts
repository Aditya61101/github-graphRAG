import axios from "axios";

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_BACKEND_URL,
  headers: {
    "Content-Type": "application/json",
  },
});

apiClient.interceptors.request.use((config) => {
  const token = localStorage.getItem("token");

  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }

  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    // GitHub authorization expiry is distinct from an expired application JWT.
    const githubReconnect =
      (error.config?.url === "/repositories" ||
        error.config?.url?.startsWith("/repositories/")) &&
      error.response?.data?.detail === "GitHub reconnect required";
    if (
      !githubReconnect &&
      error.response?.status === 401 &&
      error.config?.headers?.Authorization ===
        `Bearer ${localStorage.getItem("token")}`
    ) {
      localStorage.removeItem("token");
      window.location.href = "/auth/login";
    }

    return Promise.reject(error);
  }
);
