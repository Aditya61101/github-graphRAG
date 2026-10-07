import { BookOpen, GitForkIcon } from "lucide-react";

export const NAV_DATA = {
  header: {
    name: "Decision Guard",
    logo: GitForkIcon,
    subtext: "Guard your decisions",
  },
  navMain: [
    {
      title: "Projects",
      url: "/projects",
      icon: BookOpen,
      isActive: true,
    },
  ],
};
