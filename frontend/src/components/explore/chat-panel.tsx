import { useEffect, useRef, useState } from "react";

import { ArrowUpIcon, PlusIcon, SparklesIcon } from "lucide-react";

import { Button } from "@/components/ui/button";

type Message = {
  id: number;
  role: "user" | "assistant";
  content: string;
};

export function ChatPanel() {
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);

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

  const sendMessage = () => {
    const value = input.trim();

    if (!value) {
      return;
    }

    const userMessage: Message = {
      id: Date.now(),
      role: "user",
      content: value,
    };

    setMessages((current) => [...current, userMessage]);
    setInput("");

    requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });

    window.setTimeout(() => {
      const agentMessage: Message = {
        id: Date.now() + 1,
        role: "assistant",
        content:
          "I can help analyse repositories, explain architecture, and explore GraphRAG relationships. Backend integration can be connected next.",
      };

      setMessages((current) => [...current, agentMessage]);
    }, 700);
  };

  return (
    <div className="relative flex h-full flex-col overflow-hidden rounded-lg border border-border/60 bg-card text-card-foreground shadow-2xl backdrop-blur-xl">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(61,189,162,0.12),transparent_28%),radial-gradient(circle_at_bottom_right,rgba(59,130,246,0.1),transparent_22%)] opacity-80 dark:opacity-70" />

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
              className="scrollbar-macos flex h-128 flex-col gap-3 overflow-y-auto px-4 py-2"
            >
              {messages.map((message) => (
                <div
                  key={message.id}
                  className={message.role === "user" ? "ml-auto" : "mr-auto"}
                >
                  <div
                    className={
                      message.role === "user"
                        ? "max-w-2xl rounded-2xl rounded-br-md bg-primary px-4 py-3 text-sm wrap-break-word text-primary-foreground shadow-lg"
                        : "max-w-2xl rounded-2xl rounded-bl-md border border-border/60 bg-transparent px-4 py-3 text-sm wrap-break-word text-foreground"
                    }
                  >
                    {message.content}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="mx-auto w-full max-w-4xl border-border/60 p-2">
          <div className="rounded-lg border border-border/70 bg-background/80 p-4 shadow-2xl transition-all duration-300">
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

            <div className="flex items-center justify-between gap-3">
              <Button variant="ghost" size="icon">
                <PlusIcon className="size-4" />
              </Button>

              <Button size="icon" onClick={sendMessage}>
                <ArrowUpIcon className="size-4" />
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
