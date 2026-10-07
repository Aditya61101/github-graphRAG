import { useEffect, useState, type ComponentProps } from "react";

import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { NavMain } from "@/components/core/sidebar/nav-main";
import { NavProjects } from "@/components/core/sidebar/nav-projects";
import { NavUser } from "@/components/core/sidebar/nav-user";
import { NAV_DATA } from "@/constants/navbar";
import { projectService } from "@/api/project-service";
import type { Project } from "@/types/project";

export function AppSidebar({ ...props }: ComponentProps<typeof Sidebar>) {
  const [recentProjects, setRecentProjects] = useState<Project[]>([]);

  useEffect(() => {
    const loadProjects = async () => {
      const projects = await projectService.getProjects();
      const recentProjects = projects.slice(0, 3);
      setRecentProjects(recentProjects);
    };

    void loadProjects();
  }, []);

  return (
    <Sidebar collapsible="icon" {...props}>
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" className="cursor-auto">
              <div className="flex aspect-square size-8 items-center justify-center rounded-lg bg-sidebar-primary text-sidebar-primary-foreground">
                <NAV_DATA.header.logo className="size-4" />
              </div>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-medium">
                  {NAV_DATA.header.name}
                </span>
                <span className="truncate text-xs text-muted-foreground">
                  {NAV_DATA.header.subtext}
                </span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>
      <SidebarContent>
        <NavMain items={NAV_DATA.navMain} />
        <NavProjects projects={recentProjects} />
      </SidebarContent>
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>
      <SidebarRail />
    </Sidebar>
  );
}
