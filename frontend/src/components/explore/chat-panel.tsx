import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import axios from "axios";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Copy, Loader, Plus, Send } from "lucide-react";
import { exploreService } from "@/api/explore-service";
import {
  adaptQueryResponse,
  createConversationHistory,
  emptyContext,
} from "@/lib/explorer-model";
import { getApiErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import type {
  AnswerContext,
  ConversationHistory,
  ExploreChatMessage,
} from "@/types/explore";

type Props = {
  repositoryId: string;
  userId: string;
  token: string;
  onContext: (context: AnswerContext) => void;
  onConversationChange: () => void;
};
export function ChatPanel({
  repositoryId,
  userId,
  token,
  onContext,
  onConversationChange,
}: Props) {
  const queryClient = useQueryClient();
  const key = ["explorer-conversations", userId, token, repositoryId];
  const history = useQuery({
    queryKey: key,
    queryFn: createConversationHistory,
    initialData: createConversationHistory,
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const conversation = history.data.conversations.find(
    (item) => item.id === history.data.activeId
  )!;
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const request = useRef<AbortController | null>(null);
  const locked = useRef(false);
  const messages = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    messages.current?.scrollTo({ top: messages.current.scrollHeight });
  }, [conversation.id, conversation.messages.length, pending]);
  const update = (fn: (data: ConversationHistory) => ConversationHistory) =>
    queryClient.setQueryData<ConversationHistory>(key, (current) =>
      current ? fn(current) : current
    );
  const switchConversation = (id: string) => {
    if (denied || locked.current) return;
    update((data) => ({ ...data, activeId: id }));
    setInput("");
    setError(null);
    setDenied(false);
    onConversationChange();
  };
  const startConversation = () => {
    if (denied || locked.current) return;
    const fresh = createConversationHistory();
    update((data) => ({
      activeId: fresh.activeId,
      conversations: [...data.conversations, ...fresh.conversations],
    }));
    setInput("");
    setError(null);
    setDenied(false);
    onConversationChange();
  };
  const send = async (retry = false) => {
    const last = conversation.messages.at(-1);
    const query = retry && last?.role === "user" ? last.content : input.trim();
    if (denied || !query || locked.current || (!retry && last?.role === "user"))
      return;
    locked.current = true;
    setPending(true);
    setError(null);
    setDenied(false);
    const conversationId = conversation.id;
    const controller = new AbortController();
    request.current = controller;
    if (!retry) {
      const message: ExploreChatMessage = {
        id: crypto.randomUUID(),
        role: "user",
        content: query,
        sources: [],
        graphContext: emptyContext(),
      };
      update((data) => ({
        ...data,
        conversations: data.conversations.map((item) =>
          item.id === conversationId
            ? {
                ...item,
                title: item.messages.length ? item.title : query.slice(0, 60),
                messages: [...item.messages, message],
              }
            : item
        ),
      }));
      setInput("");
    }
    try {
      const response = await exploreService.sendMessage(
        repositoryId,
        conversationId,
        query,
        controller.signal
      );
      if (controller.signal.aborted || localStorage.getItem("token") !== token)
        return;
      const message = adaptQueryResponse(response);
      update((data) => ({
        ...data,
        conversations: data.conversations.map((item) =>
          item.id === conversationId
            ? { ...item, messages: [...item.messages, message] }
            : item
        ),
      }));
    } catch (failure) {
      if (!controller.signal.aborted) {
        const accessDenied =
          axios.isAxiosError(failure) &&
          [401, 403, 404].includes(failure.response?.status ?? 0);
        setDenied(accessDenied);
        if (accessDenied) onConversationChange();
        setError(
          getApiErrorMessage(
            failure,
            "The response could not be confirmed. Retrying may repeat a completed query."
          )
        );
      }
    } finally {
      locked.current = false;
      if (!controller.signal.aborted) {
        setPending(false);
        composer.current?.focus();
      }
    }
  };
  const unanswered = conversation.messages.at(-1)?.role === "user" && !pending;
  return (
    <section
      aria-label="Architecture Chat"
      className="flex h-full min-h-0 flex-col overflow-hidden rounded-xl border bg-card"
    >
      <header className="shrink-0 space-y-3 border-b p-3">
        <h2 className="font-semibold">Architecture Chat</h2>
        <div className="flex min-w-0 gap-2">
          <label className="sr-only" htmlFor="explorer-conversation">
            Conversation
          </label>
          <select
            id="explorer-conversation"
            className="min-w-0 flex-1 rounded-lg border bg-background px-2 py-1 text-sm"
            value={conversation.id}
            disabled={pending || denied}
            onChange={(event) => switchConversation(event.target.value)}
          >
            {history.data.conversations.map((item) => (
              <option key={item.id} value={item.id}>
                {item.title}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            variant="outline"
            disabled={pending || denied}
            onClick={startConversation}
          >
            <Plus aria-hidden="true" />
            New
          </Button>
        </div>
      </header>
      <div
        ref={messages}
        data-slot="chat-messages"
        aria-label="Conversation messages"
        className="min-h-0 flex-1 space-y-4 overflow-y-auto overscroll-contain p-3"
      >
        {!denied &&
          (conversation.messages.length ? (
            conversation.messages.map((message) => (
              <article
                key={message.id}
                data-message-id={message.id}
                className={
                  message.role === "user"
                    ? "ml-4 rounded-xl bg-muted p-3"
                    : "rounded-xl border p-3"
                }
              >
                <p className="mb-2 text-xs font-medium text-muted-foreground">
                  {message.role === "user" ? "You" : "Assistant"}
                </p>
                <Markdown content={message.content} />
                {message.role === "assistant" && (
                  <AssistantActions message={message} onContext={onContext} />
                )}
              </article>
            ))
          ) : (
            <div className="py-10 text-center">
              <p className="font-medium">
                Ask about this repository's architecture
              </p>
              <p className="mt-2 text-sm text-muted-foreground">
                Answers can include supporting sources and graph context.
              </p>
            </div>
          ))}
        {pending && (
          <p
            role="status"
            aria-busy="true"
            className="flex items-center gap-2 text-sm text-muted-foreground"
          >
            <Loader
              className="size-4 animate-spin motion-reduce:animate-none"
              aria-hidden="true"
            />
            Generating answer…
          </p>
        )}
        {error && (
          <div role="alert" className="space-y-2 rounded-lg border p-3 text-sm">
            <p>
              {denied
                ? "Repository chat access denied."
                : "Unable to confirm the answer."}
            </p>
            <p>{error}</p>
          </div>
        )}
        {unanswered && !denied && (
          <Button variant="outline" size="sm" onClick={() => void send(true)}>
            Retry last question
          </Button>
        )}
      </div>
      <form
        className="shrink-0 space-y-2 border-t bg-card p-3"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
      >
        <label htmlFor="explorer-question" className="sr-only">
          Ask an architectural question
        </label>
        <textarea
          id="explorer-question"
          ref={composer}
          value={input}
          disabled={pending || denied}
          onChange={(event) => setInput(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void send();
            }
          }}
          rows={3}
          placeholder="Ask an architectural question..."
          className="max-h-32 w-full resize-none rounded-lg border bg-background p-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <div className="flex justify-end">
          <Button
            size="sm"
            type="submit"
            disabled={pending || denied || !input.trim() || unanswered}
          >
            <Send aria-hidden="true" />
            Send
          </Button>
        </div>
      </form>
    </section>
  );
}
function Markdown({ content }: { content: string }) {
  return (
    <div className="space-y-2 text-sm leading-6 wrap-anywhere [&_a]:text-primary [&_a]:underline [&_blockquote]:border-l-2 [&_blockquote]:pl-3 [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_h1]:text-lg [&_h1]:font-semibold [&_h2]:text-base [&_h2]:font-semibold [&_h3]:font-semibold [&_li]:ml-5 [&_ol]:list-decimal [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:bg-muted [&_pre]:p-3 [&_table]:block [&_table]:overflow-x-auto [&_td]:border [&_td]:p-2 [&_th]:border [&_th]:p-2 [&_ul]:list-disc">
      <ReactMarkdown
        skipHtml
        remarkPlugins={[remarkGfm]}
        components={{
          a: (props) => (
            <a {...props} target="_blank" rel="noopener noreferrer" />
          ),
          img: ({ alt }) => <span>{alt || "Image"}</span>,
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}
function AssistantActions({
  message,
  onContext,
}: {
  message: ExploreChatMessage;
  onContext: (context: AnswerContext) => void;
}) {
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const [copyStatus, setCopyStatus] = useState("");
  const hasGraph =
    message.graphContext.node_ids.length +
      message.graphContext.edge_ids.length >
    0;
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopyStatus("Copied answer");
    } catch {
      setCopyStatus("Copy failed. Select the answer text to copy it.");
    }
  };
  return (
    <div className="mt-3 space-y-3">
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          variant="outline"
          aria-expanded={sourcesOpen}
          aria-controls={`sources-${message.id}`}
          onClick={() => setSourcesOpen(!sourcesOpen)}
        >
          Sources ({message.sources.length})
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={!hasGraph}
          onClick={() =>
            onContext({ messageId: message.id, context: message.graphContext })
          }
        >
          View in Graph
        </Button>
        <Button size="sm" variant="ghost" onClick={() => void copy()}>
          <Copy aria-hidden="true" />
          Copy
        </Button>
      </div>
      {!hasGraph && (
        <p className="text-xs text-muted-foreground">
          No displayable graph context returned.
        </p>
      )}
      {copyStatus && (
        <p role="status" className="text-xs text-muted-foreground">
          {copyStatus}
        </p>
      )}
      {sourcesOpen && (
        <div id={`sources-${message.id}`} className="space-y-2">
          {message.sources.length ? (
            message.sources.map((source, index) => (
              <blockquote
                key={index}
                className="space-y-2 rounded-lg border bg-muted/30 p-3"
              >
                <p className="text-xs font-medium wrap-anywhere">
                  {source.file_path}
                </p>
                <p className="text-sm leading-6 wrap-anywhere whitespace-pre-wrap">
                  {source.excerpt}
                </p>
              </blockquote>
            ))
          ) : (
            <p className="text-xs text-muted-foreground">
              No supporting sources returned.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
