/**
 * The conversational surface: the agent turn stream.
 *
 * Three behaviours here map to the three tool classes:
 *   - server_dispatch -> render whatever the backend returned
 *   - client_action   -> apply it locally (theme switch)
 *   - intent          -> show a confirm bar; nothing is written until the user
 *                        approves, at which point we call the ordinary REST
 *                        endpoint the tool named
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { ToolCard } from "../components/ToolCard";
import { ApiError, api } from "../lib/api";
import type { ToolCall, TurnResult } from "../lib/api";
import { plainText } from "../lib/text";
import { useTheme } from "../lib/theme";

type Entry =
  | { kind: "user"; id: string; text: string }
  | { kind: "agent"; id: string; text: string; meta?: TurnResult }
  | { kind: "tool"; id: string; call: ToolCall };

const SUGGESTIONS = [
  "what can you do?",
  "draw a red fox asleep in the snow, 16:9",
  "show me my recent creations",
  "how does the cost model work?",
  "critique: a cat on a roof",
  "switch to the glacier theme",
];

const SESSION_ID = "default";

export function Companion({ onMutate }: { onMutate: () => void }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resolvedIntents, setResolvedIntents] = useState<Set<string>>(new Set());
  const { setTheme } = useTheme();
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries, busy]);

  const send = useCallback(
    async (message: string) => {
      const text = message.trim();
      if (!text || busy) return;

      setError(null);
      setBusy(true);
      setDraft("");
      const stamp = `${Date.now()}`;
      setEntries((prev) => [...prev, { kind: "user", id: `u-${stamp}`, text }]);

      try {
        const result = await api.turn({ message: text, sessionId: SESSION_ID });

        const toolEntries: Entry[] = result.toolCalls.map((call, index) => ({
          kind: "tool",
          id: `t-${stamp}-${index}`,
          call,
        }));

        // client-action tools are instructions for the browser, applied here.
        let mutated = false;
        for (const call of result.toolCalls) {
          if (call.kind === "client_action" && call.name === "set_theme") {
            const theme = call.result?.theme;
            if (typeof theme === "string") setTheme(theme);
          }
          if (call.name === "generate_image" && !call.error) mutated = true;
        }

        setEntries((prev) => [
          ...prev,
          ...toolEntries,
          { kind: "agent", id: `a-${stamp}`, text: result.text, meta: result },
        ]);
        if (mutated) onMutate();
      } catch (err) {
        const message =
          err instanceof ApiError
            ? err.isRateLimit
              ? `Rate limited — retry in ${Math.ceil(err.retryAfterMs / 1000)}s.`
              : err.code.replace(/_/g, " ")
            : "Something went wrong.";
        setError(message);
      } finally {
        setBusy(false);
      }
    },
    [busy, onMutate, setTheme],
  );

  const confirmIntent = useCallback(
    async (call: ToolCall, id: string) => {
      const mediaId = String(call.result?.mediaId ?? "");
      if (!mediaId) return;
      try {
        if (call.name === "share_image") await api.share(mediaId);
        if (call.name === "delete_image") await api.remove(mediaId);
        setResolvedIntents((prev) => new Set(prev).add(id));
        setEntries((prev) => [
          ...prev,
          {
            kind: "agent",
            id: `a-confirm-${id}`,
            text: call.name === "share_image" ? "Published to the gallery." : "Deleted.",
          },
        ]);
        onMutate();
      } catch (err) {
        setError(err instanceof ApiError ? err.code.replace(/_/g, " ") : "Action failed.");
      }
    },
    [onMutate],
  );

  return (
    <section>
      <h1 className="wsk-h1">Companion</h1>
      <p className="wsk-sub">
        The same engine as the Studio tab, reached by conversation instead of forms. Every tool call
        is shown with its class — <code>server-dispatch</code> ran on the backend,{" "}
        <code>client-action</code> was applied by this browser, and <code>intent</code> is waiting
        for you before it writes anything.
      </p>

      <div className="wsk-card">
        <div className="wsk-chat">
          {entries.length === 0 && !busy && (
            <div className="wsk-empty">Ask for something — try one of the prompts below.</div>
          )}

          {entries.map((entry) => {
            if (entry.kind === "user")
              return (
                <div className="wsk-bubble user" key={entry.id}>
                  {entry.text}
                </div>
              );
            if (entry.kind === "agent")
              return (
                <div key={entry.id} style={{ alignSelf: "flex-start", maxWidth: "min(90%, 640px)" }}>
                  {/* The docs tool's closing turn is verbatim corpus text, so it
                      can arrive as markdown. */}
                  <div className="wsk-bubble agent">{plainText(entry.text)}</div>
                  {entry.meta && (
                    <div className="wsk-row" style={{ marginTop: 6, gap: 6 }}>
                      <span className="wsk-chip">{entry.meta.latencyMs}ms</span>
                      <span className="wsk-chip">
                        {entry.meta.usage.totalTokens} tok
                      </span>
                      <span className="wsk-chip">{entry.meta.model}</span>
                    </div>
                  )}
                </div>
              );
            return (
              <ToolCard
                key={entry.id}
                call={entry.call}
                pendingIntent={
                  entry.call.kind === "intent" &&
                  !entry.call.error &&
                  !resolvedIntents.has(entry.id)
                }
                onConfirm={(call) => confirmIntent(call, entry.id)}
                onDismiss={() => setResolvedIntents((prev) => new Set(prev).add(entry.id))}
              />
            );
          })}

          {busy && (
            <div className="wsk-bubble agent">
              <span className="wsk-spin" /> thinking…
            </div>
          )}
          <div ref={endRef} />
        </div>

        {error && (
          <div className="wsk-error" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}

        <form
          className="wsk-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void send(draft);
          }}
        >
          <input
            className="wsk-input"
            placeholder="Ask for an image, your library, or how this thing works…"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={busy}
            aria-label="Message the agent"
          />
          <button className="wsk-btn" type="submit" disabled={busy || !draft.trim()}>
            Send
          </button>
        </form>

        <div className="wsk-suggest">
          {SUGGESTIONS.map((suggestion) => (
            <button key={suggestion} onClick={() => void send(suggestion)} disabled={busy}>
              {suggestion}
            </button>
          ))}
        </div>
      </div>
    </section>
  );
}
