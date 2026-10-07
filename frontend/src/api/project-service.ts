import type { Project } from "@/types/project";
import { MOCK_PROJECTS } from "@/constants/projects";

export const projectService = {
  getProjects: async (): Promise<Project[]> => {
    const projects = await new Promise<Project[]>((resolve) => {
      setTimeout(() => {
        resolve([]);
      }, 1000);
    });
    return projects;
  },
  getProjectById: async (projectId: Project["id"]): Promise<Project> => {
    const projects = await new Promise<Project>((resolve) => {
      setTimeout(() => {
        resolve(MOCK_PROJECTS.find((p) => p.id === projectId)!);
      }, 3000);
    });
    return projects;
  },
};
