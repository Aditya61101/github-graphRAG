import { useNavigate } from "react-router";
import { ArrowLeftIcon, HomeIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

export function NotFound() {
  const navigate = useNavigate();

  return (
    <div className="m-auto flex min-h-svh flex-col items-center justify-center gap-2">
      <h1 className="text-8xl leading-tight font-bold">404</h1>
      <span className="font-medium">Oops! Page Not Found!</span>
      <p className="text-center text-muted-foreground">
        It seems like the page you're looking for <br />
        does not exist or might have been removed.
      </p>
      <div className="mt-6 flex gap-4">
        <Button variant="outline" onClick={() => navigate(-1)}>
          <ArrowLeftIcon className="size-4" />
          Go Back
        </Button>
        <Button onClick={() => navigate("/")}>
          <HomeIcon className="size-4" />
          Back to Home
        </Button>
      </div>
    </div>
  );
}
