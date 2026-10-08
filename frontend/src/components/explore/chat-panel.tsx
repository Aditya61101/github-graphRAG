import { useEffect, useRef, useState } from "react";

import { useMutation } from "@tanstack/react-query";
import { ArrowUpIcon, SparklesIcon } from "lucide-react";

import { exploreService } from "@/api/explore-service";
import { Button } from "@/components/ui/button";
import type { ExploreChatMessage } from "@/types/explore";
import { cn } from "cn";

export function ChatPanel() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ExploreChatMessage[]>([]);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    if (!scrollRef.current) {
      return;
    }

    scrollRef.current.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  const sendMessageMutation = useMutation({
    mutationFn: exploreService.sendMessage,
    onSuccess: (response) => {
      setMessages((current) => [...current, response]);
    },
  });

  const sendMessage = () => {
    const value = input.trim();

    if (!value) {
      return;
    }

    const userMessage: ExploreChatMessage = {
      id: Date.now(),
      role: "user",
      content: value,
    };

    setMessages((current) => [...current, userMessage]);
    setInput("");

    requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });

    sendMessageMutation.mutate({
      message: value,
    });
  };

  return (
    <div className="relative flex h-full flex-col overflow-hidden rounded-lg border border-border/60 bg-card text-card-foreground shadow-2xl backdrop-blur-xl">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(61,189,162,0.12),transparent_28%),radial-gradient(circle_at_bottom_right,rgba(59,130,246,0.1),transparent_22%)]" />

      <div className="relative flex h-full min-h-0 flex-1 flex-col gap-2">
        <div className="scrollbar-macos flex min-h-0 flex-1 flex-col overflow-y-auto py-2">
          {messages.length === 0 ? (
            <div className="mx-auto flex max-w-sm flex-1 items-center px-2">
              <div className="flex flex-col justify-center text-center">
                <div className="mx-auto mb-6 flex size-10 items-center justify-center rounded-xl border border-border/70 bg-muted/30">
                  <SparklesIcon className="size-4 text-primary" />
                </div>

                <h1 className="text-2xl font-semibold tracking-tight">
                  Explore your repositories
                </h1>

                <p className="mx-auto mt-4 max-w-xl text-sm text-muted-foreground">
                  Ask architectural questions, inspect repository relationships,
                  and navigate insights.
                </p>
              </div>
            </div>
          ) : (
            <div
              ref={scrollRef}
              className="scrollbar-macos flex h-130 flex-col gap-3 overflow-y-auto px-4 py-2"
            >
              {messages.map((message) => (
                <div
                  key={message.id}
                  className={message.role === "user" ? "ml-auto" : "mr-auto"}
                >
                  <div
                    className={cn(
                      "max-w-2xl rounded-2xl px-4 py-2 text-xs wrap-break-word",
                      message.role === "user"
                        ? "ml-12 rounded-br-md bg-primary wrap-break-word text-primary-foreground shadow-lg"
                        : "rounded-bl-md border border-border/60 bg-transparent text-foreground"
                    )}
                  >
                    {message.content}
                  </div>
                </div>
              ))}

              {sendMessageMutation.isPending ? (
                <div className="mr-auto">
                  <div className="flex items-center gap-2 bg-transparent px-4 py-2 text-xs text-muted-foreground">
                    <span className="size-2 animate-pulse rounded-full bg-yellow-500" />
                    Thinking...
                  </div>
                </div>
              ) : null}
            </div>
          )}
        </div>

        <div className="absolute bottom-0 mx-auto w-full max-w-4xl p-2">
          <div className="rounded-lg border border-border bg-background/80 p-4 shadow-2xl transition-all duration-300">
            <textarea
              ref={textareaRef}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  sendMessage();
                }
              }}
              className="field-sizing-content max-h-20 w-full resize-none border-none bg-transparent text-sm outline-none placeholder:text-sm placeholder:text-muted-foreground"
              placeholder="Help me understand the repository structure."
            />

            <div className="flex items-center justify-end gap-3">
              <Button
                size="icon"
                onClick={sendMessage}
                disabled={sendMessageMutation.isPending}
              >
                <ArrowUpIcon className="size-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
