import { CalendarClock, FileIcon } from "lucide-react";

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import type { Project } from "@/types/project";

type ProjectCardProps = {
  project: Project;
};

export function ProjectCard({ project }: ProjectCardProps) {
  return (
    <Card className="h-full cursor-pointer transition-all duration-200 hover:ring-primary/70">
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2">
              <FileIcon className="size-4" />
              {project.name}
            </CardTitle>
            <CardDescription className="mt-2 line-clamp-2">
              {project.description}
            </CardDescription>
          </div>
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap items-center justify-between gap-4 text-sm text-muted-foreground">
          <div className="flex items-center gap-1.5">
            <CalendarClock className="size-4" />
            <span>Updated {project.updatedAt}</span>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
