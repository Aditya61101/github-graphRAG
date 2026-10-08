import type { Project } from "@/types/project";
import { MOCK_PROJECTS } from "@/constants/projects";

export type CreateProjectPayload = {
  name: string;
  description: string;
  repositoryId: string;
};

export const projectService = {
  getProjects: async (): Promise<Project[]> => {
    const projects = await new Promise<Project[]>((resolve) => {
      setTimeout(() => {
        resolve(MOCK_PROJECTS);
      }, 1000);
    });
    return projects;
  },
  getProjectById: async (projectId: Project["id"]): Promise<Project> => {
    const projects = await new Promise<Project>((resolve) => {
      setTimeout(() => {
        resolve(MOCK_PROJECTS.find((p) => p.id === projectId)!);
      }, 1000);
    });
    return projects;
  },
  createProject: async (payload: CreateProjectPayload): Promise<Project> => {
    const project = await new Promise<Project>((resolve) => {
      setTimeout(() => {
        resolve({
          id: crypto.randomUUID(),
          name: payload.name,
          description: payload.description,
          repositoryId: payload.repositoryId,
          repositoryName: "Connected Repository",
          updatedAt: new Date().toLocaleDateString("en-GB"),
        });
      }, 1000);
    });

    MOCK_PROJECTS.push(project);

    return project;
  },
};
