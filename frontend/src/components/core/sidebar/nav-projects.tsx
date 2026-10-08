import { Frame, MoreHorizontal } from "lucide-react";

import {
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { Link, useLocation } from "react-router";

import type { Project } from "@/types/project";

type NavProjectsProps = {
  projects: Project[];
};

export function NavProjects({ projects }: NavProjectsProps) {
  const location = useLocation();

  if (projects.length === 0) {
    return null;
  }

  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>Recent projects</SidebarGroupLabel>
      <SidebarMenu>
        {projects.map((project) => (
          <SidebarMenuItem key={project.id}>
            <SidebarMenuButton
              isActive={location.pathname.startsWith(
                `/projects/${project.id}`
              )}
              render={
                <Link to={`/projects/${project.id}`}>
                  <Frame />
                  <span>{project.name}</span>
                </Link>
              }
            />
          </SidebarMenuItem>
        ))}
        <SidebarMenuItem>
          <Link to="/projects">
            <SidebarMenuButton className="text-sidebar-foreground/70">
              <MoreHorizontal className="text-sidebar-foreground/70" />
              <span>More</span>
            </SidebarMenuButton>
          </Link>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarGroup>
  );
}
