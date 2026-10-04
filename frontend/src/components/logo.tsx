import { Link } from "react-router";
import { GitForkIcon } from "lucide-react";

export function Logo() {
  return (
    <Link to="/" className="flex items-center gap-2 self-center font-medium">
      <div className="flex size-6 items-center justify-center rounded-md bg-primary text-primary-foreground">
        <GitForkIcon className="size-4" />
      </div>
      GraphRAG Inc.
    </Link>
  );
}
