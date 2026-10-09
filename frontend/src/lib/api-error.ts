import axios from "axios";

export function isAccessDenied(error: unknown): boolean {
  return axios.isAxiosError(error) && error.response?.status === 403;
}

export function getApiErrorMessage(error: unknown, fallback: string): string {
  if (isAccessDenied(error)) {
    return "GitHub access is unavailable. Check your App installation, repository grants, or reconnect your GitHub account.";
  }
  if (
    axios.isAxiosError<{ detail?: unknown }>(error) &&
    typeof error.response?.data.detail === "string"
  ) {
    return error.response.data.detail;
  }
  return fallback;
}
