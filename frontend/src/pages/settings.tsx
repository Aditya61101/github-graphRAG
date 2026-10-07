import {
  BlocksIcon,
  GiftIcon,
  GlobeIcon,
  Settings2Icon,
  Trash2Icon,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";

const settingsNavigation = [
  {
    label: "General",
    icon: Settings2Icon,
    active: true,
  },
  {
    label: "Connected apps",
    icon: BlocksIcon,
    active: false,
  },
];

const connectedApps = [
  {
    name: "GitHub",
    description: "Sync repositories, pull requests, and code insights.",
    icon: GiftIcon,
    connected: true,
  },
  {
    name: "Vercel",
    description: "Deploy and preview GraphRAG applications.",
    icon: GlobeIcon,
    connected: false,
  },
];

export default function SettingsPage() {
  return (
    <div className="flex flex-1 flex-col gap-8 p-4 pt-0">
      <section className="space-y-1">
        <h1 className="text-3xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Manage your project preferences, integrations, and workspace
          configuration.
        </p>
      </section>

      <section className="grid gap-8 lg:grid-cols-[240px_1fr]">
        <aside className="space-y-2">
          {settingsNavigation.map((item) => {
            const Icon = item.icon;

            return (
              <button
                key={item.label}
                className={`flex w-full items-center gap-3 rounded-lg px-4 py-3 text-left text-sm transition-colors ${
                  item.active
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

        <div className="space-y-8">
          <section className="space-y-1">
            <h2 className="text-2xl font-semibold tracking-tight">General</h2>
            <p className="text-sm text-muted-foreground">
              Configure your workspace details and project preferences.
            </p>
          </section>

          <div className="rounded-xl border">
            <div className="space-y-6 p-6">
              <div className="grid gap-6 lg:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="project-name">Project name</Label>
                  <Input id="project-name" placeholder="GraphRAG Workspace" />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="project-slug">Project slug</Label>
                  <Input id="project-slug" placeholder="graphrag-workspace" />
                </div>
              </div>

              <div className="space-y-2">
                <Label htmlFor="project-description">Project description</Label>
                <Input
                  id="project-description"
                  placeholder="Describe your GraphRAG workspace and integrations"
                />
              </div>

              <div className="grid gap-6 lg:grid-cols-2">
                <div className="space-y-2">
                  <Label htmlFor="default-branch">Default branch</Label>
                  <Input id="default-branch" placeholder="main" />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="visibility">Visibility</Label>
                  <Input id="visibility" placeholder="Private" />
                </div>
              </div>

              <div className="flex justify-end gap-3">
                <Button variant="outline">Cancel</Button>
                <Button>Save changes</Button>
              </div>
            </div>
          </div>

          <section className="space-y-4 rounded-xl border p-6">
            <div>
              <h3 className="text-lg font-semibold">Connected apps</h3>
              <p className="text-sm text-muted-foreground">
                Connect external services to enhance your GraphRAG workflow.
              </p>
            </div>

            <Separator />

            <div className="space-y-4">
              {connectedApps.map((app) => {
                const Icon = app.icon;

                return (
                  <div
                    key={app.name}
                    className="flex flex-col gap-4 rounded-lg border p-4 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="flex items-start gap-3">
                      <div className="flex size-10 items-center justify-center rounded-lg bg-muted">
                        <Icon className="size-5" />
                      </div>

                      <div>
                        <h4 className="font-medium">{app.name}</h4>
                        <p className="text-sm text-muted-foreground">
                          {app.description}
                        </p>
                      </div>
                    </div>

                    <Button variant={app.connected ? "outline" : "default"}>
                      {app.connected ? "Disconnect" : "Connect"}
                    </Button>
                  </div>
                );
              })}
            </div>
          </section>

          <section className="rounded-xl border border-destructive/40">
            <div className="space-y-4 p-6">
              <div>
                <h3 className="text-lg font-semibold text-destructive">
                  Delete project
                </h3>
                <p className="text-sm text-muted-foreground">
                  Permanently remove this project, repository mappings, and
                  generated GraphRAG data. This action cannot be undone.
                </p>
              </div>

              <div className="flex flex-col gap-3 rounded-lg border border-destructive/30 bg-destructive/5 p-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="font-medium">Delete this project</p>
                  <p className="text-sm text-muted-foreground">
                    Make sure you have exported any required data before
                    deletion.
                  </p>
                </div>

                <Button variant="destructive">
                  <Trash2Icon className="size-4" />
                  Delete project
                </Button>
              </div>
            </div>
          </section>
        </div>
      </section>
    </div>
  );
}
