import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";

export function TechnicalDetails({
  title,
  value,
}: {
  title: string;
  value: unknown;
}) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const text = useMemo(
    () => (open ? JSON.stringify(value, null, 2) : ""),
    [open, value]
  );
  return (
    <details
      className="rounded-lg border p-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm text-muted-foreground">
        {title}
      </summary>
      {open && (
        <div className="mt-3 space-y-2">
          <Button
            variant="outline"
            size="xs"
            onClick={async () => {
              try {
                await navigator.clipboard.writeText(text);
                setCopied(true);
                setCopyFailed(false);
              } catch {
                setCopyFailed(true);
              }
            }}
          >
            Copy JSON
          </Button>
          <span role="status" className="ml-2 text-xs">
            {copyFailed
              ? "Clipboard unavailable; select and copy the text below."
              : copied
                ? "Copied"
                : ""}
          </span>
          <pre className="max-h-96 overflow-auto text-xs break-words whitespace-pre-wrap">
            {text}
          </pre>
        </div>
      )}
    </details>
  );
}
