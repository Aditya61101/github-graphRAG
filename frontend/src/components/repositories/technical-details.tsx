import { useState } from "react";

export function TechnicalDetails({
  title,
  value,
}: {
  title: string;
  value: unknown;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="rounded-lg border p-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm text-muted-foreground">
        {title}
      </summary>
      {open && (
        <pre className="mt-3 max-h-96 overflow-auto text-xs break-words whitespace-pre-wrap">
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </details>
  );
}
