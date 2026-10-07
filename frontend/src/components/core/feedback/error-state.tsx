import { CircleXIcon, type LucideIcon } from "lucide-react";

type ErrorStateProps = {
  Icon?: LucideIcon;
  text?: string;
  subtext?: string;
};

export function ErrorState({
  Icon = CircleXIcon,
  text = "Something went wrong!",
  subtext = "An unknown error occurred. Please try again later.",
}: ErrorStateProps) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center">
      <Icon className="mb-4 size-16 text-destructive" />
      <div className="mb-6">
        <h4 className="text-center">{text}</h4>
        <p className="text-center text-sm text-muted-foreground">{subtext}</p>
      </div>
    </div>
  );
}
