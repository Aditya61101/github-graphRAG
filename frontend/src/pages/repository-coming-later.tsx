import { useLocation } from "react-router";

const names: Record<string, string> = {
  explore: "Knowledge Explorer",
  decisions: "Decisions",
  "pull-requests": "Pull Requests",
  settings: "Settings",
};

export default function RepositoryComingLater() {
  const { pathname } = useLocation();
  return (
    <section className="space-y-3 rounded-xl border p-6">
      <h1 className="text-2xl font-semibold">
        {names[pathname.split("/").at(-1) ?? ""]}
      </h1>
      <p className="text-sm text-muted-foreground">
        Coming later. This repository feature is not available yet.
      </p>
    </section>
  );
}
