import { useState } from "react";

import {
  BlocksIcon,
  GlobeIcon,
  MailIcon,
  Settings2Icon,
  ShieldCheckIcon,
  UserCircle2Icon,
} from "lucide-react";

import { AppHeader } from "@/components/core/app-header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { GithubIcon } from "@/components/icons";

const settingsNavigation = [
  {
    id: "general",
    label: "General",
    icon: Settings2Icon,
  },
  {
    id: "connected-apps",
    label: "Connected apps",
    icon: BlocksIcon,
  },
] as const;

const connectedApps = [
  {
    name: "Confluence",
    description:
      "Import architecture decision records and internal engineering documentation.",
    icon: GlobeIcon,
    connected: true,
    status: "24 synced pages",
  },
  {
    name: "Google Drive",
    description:
      "Connect shared folders containing ADRs, RFCs, and technical specifications.",
    icon: GlobeIcon,
    connected: false,
    status: "Not connected",
  },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] =
    useState<(typeof settingsNavigation)[number]["id"]>("general");

  const headerCrumbs = [{ label: "Settings", pathname: "/settings" }];

  return (
    <>
      <AppHeader crumbs={headerCrumbs} />

      <div className="flex flex-1 flex-col gap-6 p-4 pt-0">
        <section className="space-y-1">
          <h1 className="text-3xl font-semibold tracking-tight">Settings</h1>
          <p className="text-sm text-muted-foreground">
            Manage your account, profile preferences, and connected
            integrations.
          </p>
        </section>

        <section className="grid gap-6 lg:grid-cols-[220px_1fr]">
          <aside className="space-y-2">
            {settingsNavigation.map((item) => {
              const Icon = item.icon;

              return (
                <button
                  key={item.id}
                  onClick={() => setActiveTab(item.id)}
                  className={`flex w-full items-center gap-3 rounded-lg px-4 py-3 text-left text-sm transition-colors ${
                    activeTab === item.id
                      ? "bg-muted font-medium text-foreground"
                      : "text-muted-foreground hover:bg-muted/50 hover:text-foreground"
                  }`}
                >
                  <Icon className="size-4" />
                  {item.label}
                </button>
              );
            })}
          </aside>

          <div className="space-y-6">
            {activeTab === "general" && (
              <section className="rounded-xl border bg-background">
                <div className="border-b px-6 py-4">
                  <h2 className="text-xl font-semibold tracking-tight">
                    General
                  </h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Manage your profile information and account preferences.
                  </p>
                </div>

                <div className="space-y-6 p-6">
                  <div className="flex items-center gap-4 rounded-xl border p-4">
                    <div className="flex size-16 items-center justify-center rounded-full bg-primary/10 text-primary">
                      <UserCircle2Icon className="size-9" />
                    </div>

                    <div>
                      <h3 className="text-lg font-semibold">Samrat Roy</h3>
                      <p className="text-sm text-muted-foreground">
                        AI Engineer • GraphRAG Workspace Owner
                      </p>
                    </div>
                  </div>

                  <div className="grid gap-5 lg:grid-cols-2">
                    <div className="space-y-2">
                      <Label htmlFor="full-name">Full name</Label>
                      <Input id="full-name" value="Samrat Roy" readOnly />
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="display-name">Display name</Label>
                      <Input id="display-name" value="Samrat" readOnly />
                    </div>
                  </div>

                  <div className="grid gap-5 lg:grid-cols-2">
                    <div className="space-y-2">
                      <Label htmlFor="email">Email address</Label>

                      <div className="relative">
                        <MailIcon className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />

                        <Input
                          id="email"
                          value="samrat@graphrag.dev"
                          className="pl-9"
                          readOnly
                        />
                      </div>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="role">Workspace role</Label>
                      <Input id="role" value="Administrator" readOnly />
                    </div>
                  </div>

                  <div className="grid gap-4 sm:grid-cols-3">
                    <div className="rounded-xl border p-4">
                      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        Projects
                      </p>
                      <p className="mt-2 text-2xl font-semibold">5</p>
                    </div>

                    <div className="rounded-xl border p-4">
                      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        Repositories
                      </p>
                      <p className="mt-2 text-2xl font-semibold">12</p>
                    </div>

                    <div className="rounded-xl border p-4">
                      <p className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                        Indexed entities
                      </p>
                      <p className="mt-2 text-2xl font-semibold">82K</p>
                    </div>
                  </div>

                  <div className="flex justify-end gap-3">
                    <Button variant="outline">Cancel</Button>
                    <Button>Save changes</Button>
                  </div>
                </div>
              </section>
            )}

            {activeTab === "connected-apps" && (
              <section className="rounded-xl border bg-background">
                <div className="border-b px-6 py-4">
                  <h2 className="text-xl font-semibold tracking-tight">
                    Connected apps
                  </h2>
                  <p className="mt-1 text-sm text-muted-foreground">
                    Manage integrations connected to your GraphRAG workspace.
                  </p>
                </div>

                <div className="space-y-6 p-6">
                  <div>
                    <div className="mb-4 flex items-center gap-2">
                      <BlocksIcon className="size-4 text-primary" />
                      <h3 className="font-semibold">ADR integrations</h3>
                    </div>

                    <div className="space-y-4">
                      {connectedApps.map((app) => {
                        const Icon = app.icon;

                        return (
                          <div
                            key={app.name}
                            className="flex flex-col gap-4 rounded-xl border p-4 sm:flex-row sm:items-center sm:justify-between"
                          >
                            <div className="flex items-start gap-3">
                              <div className="flex size-11 items-center justify-center rounded-lg bg-muted">
                                <Icon className="size-5" />
                              </div>

                              <div>
                                <h4 className="font-medium">{app.name}</h4>

                                <p className="mt-1 text-sm text-muted-foreground">
                                  {app.description}
                                </p>

                                <p className="mt-3 text-xs font-medium text-muted-foreground">
                                  {app.status}
                                </p>
                              </div>
                            </div>

                            <Button
                              variant={app.connected ? "outline" : "default"}
                            >
                              {app.connected ? "Manage" : "Connect"}
                            </Button>
                          </div>
                        );
                      })}
                    </div>
                  </div>

                  <div>
                    <div className="mb-4 flex items-center gap-2">
                      <ShieldCheckIcon className="size-4 text-emerald-500" />
                      <h3 className="font-semibold">Authenticated provider</h3>
                    </div>

                    <div className="flex flex-col gap-4 rounded-xl border p-4 sm:flex-row sm:items-center sm:justify-between">
                      <div className="flex items-start gap-3">
                        <div className="flex size-11 items-center justify-center rounded-lg bg-violet-500/15 text-violet-500">
                          <GithubIcon className="size-5" />
                        </div>

                        <div>
                          <h4 className="font-medium">GitHub</h4>
                          <p className="text-sm text-muted-foreground">
                            Connected through GitHub OAuth for repositories,
                            commits, PRs, and GraphRAG ingestion workflows.
                          </p>
                        </div>
                      </div>

                      <div className="rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-medium text-emerald-600 dark:text-emerald-400">
                        Connected
                      </div>
                    </div>
                  </div>
                </div>
              </section>
            )}
          </div>
        </section>
      </div>
    </>
  );
}
