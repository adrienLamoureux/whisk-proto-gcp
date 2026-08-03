/**
 * Renders one tool call, styled by its *class* rather than its name.
 *
 * The three classes look different on purpose -- the point of the architecture
 * is that a client-action, a server-dispatch and a confirm-intent are genuinely
 * different things, and the UI should make that legible instead of flattening
 * them into "the agent did something".
 */
import type { Media, ToolCall } from "../lib/api";
import { plainText } from "../lib/text";

interface Props {
  call: ToolCall;
  pendingIntent: boolean;
  onConfirm: (call: ToolCall) => void;
  onDismiss: (call: ToolCall) => void;
}

const KIND_LABEL: Record<ToolCall["kind"], string> = {
  server_dispatch: "server-dispatch",
  client_action: "client-action",
  intent: "intent · needs confirm",
};

export function ToolCard({ call, pendingIntent, onConfirm, onDismiss }: Props) {
  const result = call.result ?? {};
  const items = Array.isArray(result.items) ? (result.items as Media[]) : null;
  const imageUrl = typeof result.imageUrl === "string" ? result.imageUrl : null;
  const capability = Array.isArray(result.items) && result.title ? result : null;

  return (
    <div className="wsk-tool">
      <div className="wsk-tool-head">
        <span className="wsk-tool-name">{call.name}</span>
        <span className="wsk-chip">{KIND_LABEL[call.kind]}</span>
        {call.error ? (
          <span className="wsk-chip danger">{call.error}</span>
        ) : (
          <span className="wsk-chip ok">ok</span>
        )}
      </div>

      <div className="wsk-tool-body">
        {call.error && (
          <p style={{ margin: 0, color: "var(--wsk-danger)" }}>
            {humaniseError(call.error, result)}
          </p>
        )}

        {imageUrl && (
          <img
            src={imageUrl}
            alt={String(result.prompt ?? "generated image")}
            style={{
              width: "100%",
              maxWidth: 320,
              borderRadius: "var(--wsk-radius-sm)",
              display: "block",
            }}
          />
        )}

        {capability && (
          <ul style={{ margin: 0, paddingLeft: 18, lineHeight: 1.75 }}>
            {(capability.items as { label: string; example: string }[]).map((item) => (
              <li key={item.label}>
                <strong>{item.label}</strong>{" "}
                <span style={{ color: "var(--wsk-text-faint)" }}>— “{item.example}”</span>
              </li>
            ))}
          </ul>
        )}

        {items && !capability && items.length > 0 && (
          <div className="wsk-media-grid" style={{ gridTemplateColumns: "repeat(auto-fill, minmax(110px, 1fr))" }}>
            {items.map((item) => (
              <figure className="wsk-media" key={item.mediaId} style={{ margin: 0 }}>
                <img src={item.url} alt={item.prompt} loading="lazy" />
              </figure>
            ))}
          </div>
        )}

        {items && items.length === 0 && (
          <p style={{ margin: 0, color: "var(--wsk-text-faint)" }}>Nothing here yet.</p>
        )}

        {typeof result.answer === "string" && (
          <>
            <p style={{ margin: "0 0 8px", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
              {plainText(result.answer)}
            </p>
            {Array.isArray(result.citations) && result.citations.length > 0 && (
              <div className="wsk-row">
                {(result.citations as { doc: string; heading: string }[]).map((c, i) => (
                  <span className="wsk-chip" key={`${c.doc}-${i}`}>
                    {c.doc} › {c.heading}
                  </span>
                ))}
              </div>
            )}
          </>
        )}

        {typeof result.critique === "string" && (
          <>
            <pre>{result.critique}</pre>
            <span className="wsk-chip accent" style={{ marginTop: 8 }}>
              via {String(result.via ?? "in-process")}
            </span>
          </>
        )}

        {!imageUrl && !items && !result.answer && !result.critique && !call.error && (
          <pre>{JSON.stringify(result, null, 2)}</pre>
        )}
      </div>

      {pendingIntent && (
        <div className="wsk-confirm">
          <span>{String(result.summary ?? "Confirm this action?")}</span>
          <button
            className={`wsk-btn sm ${call.name === "delete_image" ? "danger" : ""}`}
            onClick={() => onConfirm(call)}
          >
            Confirm
          </button>
          <button className="wsk-btn ghost sm" onClick={() => onDismiss(call)}>
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

function humaniseError(code: string, result: Record<string, unknown>): string {
  switch (code) {
    case "image_daily_cap_reached":
      return `Daily image cap reached (${result.capacity ?? 50}/day). Resets at midnight UTC.`;
    case "daily_cap_reached":
      return "Daily token budget spent. Resets at midnight UTC.";
    case "rate_limited":
      return "Going a bit fast — try again in a moment.";
    case "prompt_required":
      return "I need something to draw.";
    case "media_not_found":
      return "I couldn't find that image.";
    case "unknown_theme":
      return "That isn't one of the ten themes.";
    default:
      return code.replace(/_/g, " ");
  }
}
