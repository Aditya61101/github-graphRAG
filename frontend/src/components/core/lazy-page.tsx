import { Component, Suspense, type ReactNode } from "react";
import { useLocation } from "react-router";
import { ContentLoading } from "@/components/core/query-feedback";
import { Button } from "@/components/ui/button";

class PageLoadBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return (
        <section
          role="alert"
          className="space-y-3 rounded-xl border bg-card p-6"
        >
          <h1 className="text-xl font-semibold">
            This page could not be loaded
          </h1>
          <p className="text-sm text-muted-foreground">
            Page files may be unavailable after a connection interruption or an
            update. Check your connection and reload to try again.
          </p>
          <Button variant="outline" onClick={() => window.location.reload()}>
            Reload page
          </Button>
        </section>
      );
    }
    return this.props.children;
  }
}

export function LazyPage({
  children,
  label,
}: {
  children: ReactNode;
  label: string;
}) {
  const { pathname } = useLocation();
  return (
    <PageLoadBoundary key={pathname}>
      <Suspense fallback={<ContentLoading label={label} />}>
        {children}
      </Suspense>
    </PageLoadBoundary>
  );
}
