import { useId, useState } from "react";
import { ChevronDownIcon, GitForkIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type { OnboardingStatus } from "@/types/auth";

type InstallationGroup = OnboardingStatus["installations"][number];

function RepositoryGroup({
  installation,
}: {
  installation: InstallationGroup;
}) {
  const [open, setOpen] = useState(false);
  const triggerId = useId();

  return (
    <Collapsible
      open={open}
      onOpenChange={setOpen}
      className="rounded-lg border"
    >
      <h2>
        <CollapsibleTrigger
          id={triggerId}
          render={<Button variant="ghost" />}
          className="h-auto w-full justify-between gap-3 px-3 py-3 text-left whitespace-normal"
        >
          <span className="min-w-0">
            <span className="block font-medium break-words">
              {installation.account_login}
            </span>
            <span className="block text-xs font-normal text-muted-foreground">
              {installation.account_type} · {installation.repositories.length}{" "}
              accessible public{" "}
              {installation.repositories.length === 1
                ? "repository"
                : "repositories"}
            </span>
          </span>
          <ChevronDownIcon
            aria-hidden="true"
            className={`size-4 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
          />
        </CollapsibleTrigger>
      </h2>
      <CollapsibleContent role="region" aria-labelledby={triggerId}>
        {installation.repositories.length > 0 ? (
          <ul className="space-y-3 border-t p-3">
            {installation.repositories.map((repository) => (
              <li
                key={repository.github_repository_id}
                className="flex items-start gap-2 text-sm"
              >
                <GitForkIcon
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-muted-foreground"
                />
                <a
                  href={repository.repository_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="min-w-0 rounded-sm break-all text-foreground underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring/50"
                >
                  {repository.full_name}
                  <span className="sr-only"> (opens in a new tab)</span>
                </a>
              </li>
            ))}
          </ul>
        ) : (
          <p className="border-t p-3 text-sm text-muted-foreground">
            No public repositories are currently accessible to your account in
            this installation.
          </p>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function RepositoryGroups({
  installations,
}: {
  installations: OnboardingStatus["installations"];
}) {
  return (
    <div className="space-y-3" aria-label="GitHub installations">
      {installations.map((installation) => (
        <RepositoryGroup key={installation.id} installation={installation} />
      ))}
    </div>
  );
}
