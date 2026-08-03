/** The form-driven surface. Same backend, same guardrails, no conversation. */
import { useCallback, useEffect, useState } from "react";
import { MediaGrid } from "../components/MediaGrid";
import { Meter } from "../components/Meter";
import { ApiError, api } from "../lib/api";
import type { Media, Pending, Usage } from "../lib/api";
import { useGenerationPoll } from "../lib/usePolling";

const STYLES = ["anime", "manga", "photoreal", "chibi"];
const ASPECTS = ["1:1", "3:4", "16:9", "9:16"];

export function Studio({ version, onMutate }: { version: number; onMutate: () => void }) {
  const [prompt, setPrompt] = useState("a red fox asleep in the snow");
  const [style, setStyle] = useState("anime");
  const [aspect, setAspect] = useState("3:4");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [items, setItems] = useState<Media[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [media, quota] = await Promise.all([api.media(), api.usage()]);
      setItems(media.items);
      setUsage(quota);
    } catch {
      /* a failed refresh should not clobber what is already on screen */
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, version]);

  // A prediction that missed the server's wait window finishes here.
  const poll = useGenerationPoll(pending, () => {
    setPending(null);
    void refresh();
    onMutate();
  });

  const generate = useCallback(async () => {
    if (!prompt.trim() || busy || pending) return;
    setBusy(true);
    setError(null);
    try {
      const result = await api.generate({ prompt: prompt.trim(), style, aspect });
      if (result.pending) {
        // AnimagineXL took longer than the wait window — usually a cold GPU.
        setPending(result.pending);
      } else {
        await refresh();
        onMutate();
      }
    } catch (err) {
      if (err instanceof ApiError && err.isRateLimit) {
        setError(
          err.code === "image_daily_cap_reached"
            ? "Daily image cap reached. It resets at midnight UTC."
            : `Slow down a moment — retry in ${Math.ceil(err.retryAfterMs / 1000)}s.`,
        );
      } else {
        setError(err instanceof ApiError ? err.code.replace(/_/g, " ") : "Generation failed.");
      }
    } finally {
      setBusy(false);
    }
  }, [prompt, style, aspect, busy, pending, refresh, onMutate]);

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      try {
        await fn();
        await refresh();
        onMutate();
      } catch (err) {
        setError(err instanceof ApiError ? err.code.replace(/_/g, " ") : "Action failed.");
      }
    },
    [refresh, onMutate],
  );

  return (
    <section className="wsk-stack">
      <div>
        <h1 className="wsk-h1">Studio</h1>
        <p className="wsk-sub">
          AnimagineXL 3.1 on Replicate — an SDXL anime fine-tune, pinned by version hash. In{" "}
          <code>local</code> mode the provider is swapped for a deterministic generator: same code
          path, same quota accounting, no cloud spend.
        </p>
      </div>

      <div className="wsk-grid-2">
        <div className="wsk-card">
          <h2 className="wsk-h2">Generate</h2>
          <div className="wsk-stack" style={{ gap: 12 }}>
            <div>
              <label className="wsk-label" htmlFor="prompt">
                Prompt
              </label>
              <textarea
                id="prompt"
                className="wsk-textarea"
                value={prompt}
                onChange={(event) => setPrompt(event.target.value)}
                maxLength={1000}
              />
            </div>
            <div className="wsk-row">
              <div style={{ flex: 1, minWidth: 130 }}>
                <label className="wsk-label" htmlFor="style">
                  Style
                </label>
                <select
                  id="style"
                  className="wsk-select"
                  value={style}
                  onChange={(event) => setStyle(event.target.value)}
                >
                  {STYLES.map((option) => (
                    <option key={option}>{option}</option>
                  ))}
                </select>
              </div>
              <div style={{ flex: 1, minWidth: 130 }}>
                <label className="wsk-label" htmlFor="aspect">
                  Aspect
                </label>
                <select
                  id="aspect"
                  className="wsk-select"
                  value={aspect}
                  onChange={(event) => setAspect(event.target.value)}
                >
                  {ASPECTS.map((option) => (
                    <option key={option}>{option}</option>
                  ))}
                </select>
              </div>
            </div>
            <button
              className="wsk-btn"
              onClick={() => void generate()}
              disabled={busy || !!pending || !prompt.trim()}
            >
              {busy ? "Starting…" : pending ? "Rendering…" : "Generate"}
            </button>

            {pending && (
              <div className="wsk-pending">
                <span className="wsk-spin" />
                <div>
                  <strong>Rendering on a Replicate GPU</strong>
                  <p>
                    It missed the {`server's`} short wait window — usually a cold container. Polling
                    with backoff; {Math.round(poll.elapsedMs / 1000)}s elapsed.
                  </p>
                </div>
              </div>
            )}

            {(error || poll.error) && <div className="wsk-error">{error ?? poll.error}</div>}
          </div>
        </div>

        <div className="wsk-card">
          <h2 className="wsk-h2">Your quota today</h2>
          {usage ? (
            <div className="wsk-stack" style={{ gap: 16 }}>
              <Meter label="Images" used={usage.imagesToday} cap={usage.imageCap} />
              <Meter label="Agent tokens" used={usage.tokensToday} cap={usage.tokenCap} />
              <p style={{ margin: 0, fontSize: 12, color: "var(--wsk-text-faint)", lineHeight: 1.6 }}>
                Two independent counters with independent UTC-midnight rollovers. Images are the
                dominant cost, so they get their own ceiling rather than sharing the token budget.
              </p>
            </div>
          ) : (
            <div className="wsk-empty">Loading…</div>
          )}
        </div>
      </div>

      <div className="wsk-card">
        <div className="wsk-row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <h2 className="wsk-h2" style={{ margin: 0 }}>
            Your library
          </h2>
          <span className="wsk-chip">{items.length} items</span>
        </div>
        <MediaGrid
          items={items}
          emptyLabel="Nothing generated yet."
          onFavorite={(media) => void act(() => api.favorite(media.mediaId, !media.favorite))}
          onShare={(media) => void act(() => api.share(media.mediaId))}
          onDelete={(media) => void act(() => api.remove(media.mediaId))}
        />
      </div>
    </section>
  );
}
