/**
 * Grounded search over the studio docs.
 *
 * Shows the retrieved passages and their cosine distances alongside the answer.
 * That transparency is the point: a RAG system you cannot inspect is one you
 * cannot debug, and "why did it say that?" should be answerable by looking.
 */
import { useCallback, useState } from "react";
import { ApiError, api } from "../lib/api";
import type { RagAnswer } from "../lib/api";
import { plainText } from "../lib/text";

const EXAMPLES = [
  "why not Vertex AI Vector Search?",
  "how does the daily image cap work?",
  "what replaced DynamoDB?",
  "how is the agent loop structured?",
  "what is the difference between A2A and MCP?",
];

export function Docs() {
  const [query, setQuery] = useState("");
  const [answer, setAnswer] = useState<RagAnswer | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const search = useCallback(async (value: string) => {
    const q = value.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    setQuery(q);
    try {
      setAnswer(await api.ragSearch(q));
    } catch (err) {
      setError(err instanceof ApiError ? err.code.replace(/_/g, " ") : "Search failed.");
    } finally {
      setBusy(false);
    }
  }, []);

  const reingest = useCallback(async () => {
    setBusy(true);
    try {
      const stats = await api.reingest();
      setError(`Re-ingested ${stats.chunks} chunks from ${stats.documents} documents.`);
    } catch {
      setError("Re-ingest failed.");
    } finally {
      setBusy(false);
    }
  }, []);

  return (
    <section className="wsk-stack">
      <div>
        <h1 className="wsk-h1">Ask the docs</h1>
        <p className="wsk-sub">
          Retrieval runs over Firestore's native vector index (<code>find_nearest</code>, cosine) —
          not Vertex AI Vector Search, which needs always-on index nodes. Passages and distances are
          shown so you can see exactly what grounded the answer.
        </p>
      </div>

      <div className="wsk-card">
        <form
          className="wsk-composer"
          style={{ marginTop: 0 }}
          onSubmit={(event) => {
            event.preventDefault();
            void search(query);
          }}
        >
          <input
            className="wsk-input"
            placeholder="Ask about the architecture, cost model, guardrails…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            aria-label="Search the documentation"
          />
          <button className="wsk-btn" type="submit" disabled={busy || !query.trim()}>
            {busy ? "…" : "Search"}
          </button>
        </form>

        <div className="wsk-suggest">
          {EXAMPLES.map((example) => (
            <button key={example} onClick={() => void search(example)} disabled={busy}>
              {example}
            </button>
          ))}
        </div>

        {error && (
          <div className="wsk-error" style={{ marginTop: 12 }}>
            {error}
          </div>
        )}
      </div>

      {answer && (
        <div className="wsk-card">
          <div className="wsk-row" style={{ marginBottom: 12 }}>
            <span className={`wsk-chip ${answer.grounded ? "ok" : "warn"}`}>
              {answer.grounded ? "grounded" : "no match"}
            </span>
            <span className="wsk-chip">{answer.passages.length} passages</span>
          </div>

          <p style={{ margin: "0 0 20px", lineHeight: 1.65, whiteSpace: "pre-wrap" }}>
            {plainText(answer.answer)}
          </p>

          {answer.passages.length > 0 && (
            <>
              <h2 className="wsk-h2">Retrieved passages</h2>
              {answer.passages.map((passage, index) => (
                <div className="wsk-passage" key={`${passage.doc}-${index}`}>
                  <div className="wsk-row">
                    <strong style={{ fontSize: 13 }}>
                      {passage.doc} › {passage.heading}
                    </strong>
                    <span className="wsk-chip">d={passage.distance.toFixed(3)}</span>
                  </div>
                  <p>{plainText(passage.text).slice(0, 460)}</p>
                </div>
              ))}
            </>
          )}
        </div>
      )}

      <div className="wsk-card">
        <h2 className="wsk-h2">Corpus</h2>
        <p style={{ margin: "0 0 12px", fontSize: 13, color: "var(--wsk-text-dim)", lineHeight: 1.6 }}>
          The corpus is the <code>docs/</code> directory itself, so the agent's answers cannot drift
          from what a human reader would find. Ingestion is idempotent — chunk ids are content
          hashes, so re-running only rewrites what changed.
        </p>
        <button className="wsk-btn ghost sm" onClick={() => void reingest()} disabled={busy}>
          Re-ingest corpus
        </button>
      </div>
    </section>
  );
}
