import { BookOpen, GitForkIcon, Settings } from "lucide-react";

export const NAV_DATA = {
  header: {
    name: "DecisionGuard",
    logo: GitForkIcon,
    subtext: "Guard your decisions",
  },
  navMain: [
    {
      title: "Projects",
      url: "/projects",
      icon: BookOpen,
    },
    {
      title: "Settings",
      url: "/settings",
      icon: Settings,
    },
  ],
};
