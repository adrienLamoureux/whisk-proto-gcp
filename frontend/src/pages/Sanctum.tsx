/** Admin: model override, cost dashboard, and the mounted protocol surfaces. */
import { useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../lib/api";
import { useAuth } from "../lib/auth";

interface CostRow {
  userId: string;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  turnCount: number;
  estimatedUsd: number;
}

const MODEL_CHOICES = [
  { id: "", label: "Use deploy default" },
  { id: "gemini-3.6-flash", label: "Gemini 3.6 Flash — stable, the right tier for tool turns" },
  { id: "gemini-3.1-pro-preview", label: "Gemini 3.1 Pro — 1M context, preview" },
];

export function Sanctum() {
  const [model, setModel] = useState<{ modelId: string; default: string; overridden: boolean } | null>(null);
  const [choice, setChoice] = useState("");
  const [rows, setRows] = useState<CostRow[]>([]);
  const [surfaces, setSurfaces] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);
  const { account, config } = useAuth();
  const admin = account?.isAdmin ?? false;

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [modelInfo, surfaceInfo] = await Promise.all([api.agentModel(), api.surfaces()]);
      setModel(modelInfo);
      setSurfaces(surfaceInfo.surfaces);
    } catch (err) {
      setError(err instanceof ApiError ? err.code : "Failed to load.");
    }
    try {
      setRows((await api.cost()).items);
    } catch (err) {
      // Expected when not admin -- the model/surfaces panels still render.
      if (!(err instanceof ApiError && err.status === 403)) setError("Failed to load costs.");
      setRows([]);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, admin]);

  const save = useCallback(async () => {
    try {
      await api.setAgentModel(choice);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.code.replace(/_/g, " ") : "Save failed.");
    }
  }, [choice, refresh]);

  return (
    <section className="wsk-stack">
      <div>
        <h1 className="wsk-h1">Sanctum</h1>
        <p className="wsk-sub">
          Admin surface. The model override lives in Firestore with a 60s per-instance cache, so
          switching models is a database write rather than a redeploy.
        </p>
      </div>

      <div className="wsk-card">
        <h2 className="wsk-h2">Identity</h2>
        <p style={{ margin: "0 0 12px", fontSize: 13, color: "var(--wsk-text-dim)", lineHeight: 1.6 }}>
          {config?.provider === "identity-platform" ? (
            <>
              Identity Platform JWT, verified server-side on every request. Admin is a{" "}
              <strong>custom claim</strong> set out-of-band with the Admin SDK — never something a
              client can assert, so editing it here would be meaningless.
            </>
          ) : (
            <>
              In <code>local</code> mode identity is a request header chosen at sign-in. That path is
              gated on the server running locally, so it is unreachable once deployed.
            </>
          )}
        </p>
        <div className="wsk-row">
          <span className="wsk-chip accent">{account?.email}</span>
          <span className="wsk-chip">uid: {account?.uid}</span>
          <span className={`wsk-chip ${admin ? "ok" : ""}`}>
            {admin ? "admin" : "standard user"}
          </span>
          <span className="wsk-chip">{config?.provider}</span>
        </div>
      </div>

      <div className="wsk-grid-2">
        <div className="wsk-card">
          <h2 className="wsk-h2">Agent model</h2>
          {model ? (
            <div className="wsk-stack" style={{ gap: 12 }}>
              <div className="wsk-row">
                <span className="wsk-chip accent">{model.modelId}</span>
                {model.overridden && <span className="wsk-chip warn">overridden</span>}
              </div>
              <div>
                <label className="wsk-label" htmlFor="model">
                  Override
                </label>
                <select
                  id="model"
                  className="wsk-select"
                  value={choice}
                  onChange={(event) => setChoice(event.target.value)}
                >
                  {MODEL_CHOICES.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </div>
              <button className="wsk-btn" onClick={() => void save()} disabled={!admin}>
                {admin ? "Save override" : "Admin required"}
              </button>
            </div>
          ) : (
            <div className="wsk-empty">Loading…</div>
          )}
        </div>

        <div className="wsk-card">
          <h2 className="wsk-h2">Mounted surfaces</h2>
          <p style={{ margin: "0 0 12px", fontSize: 13, color: "var(--wsk-text-dim)", lineHeight: 1.6 }}>
            Three protocols on one Cloud Run service. Splitting them would triple the cold starts
            and save nothing, since billing is per request.
          </p>
          <div className="wsk-stack" style={{ gap: 10 }}>
            {Object.entries(surfaces).map(([key, value]) => {
              const info = value as { mountPath?: string; protocol?: string; tools?: string[] };
              return (
                <div key={key} className="wsk-row">
                  <span className="wsk-chip accent">{key}</span>
                  <code style={{ fontSize: 12, color: "var(--wsk-text-dim)" }}>
                    {info.mountPath}
                  </code>
                  {info.tools && <span className="wsk-chip">{info.tools.length} tools</span>}
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <div className="wsk-card">
        <h2 className="wsk-h2">Cost by user</h2>
        {error && <div className="wsk-error" style={{ marginBottom: 12 }}>{error}</div>}
        {rows.length === 0 ? (
          <div className="wsk-empty">
            {admin ? "No spend recorded yet." : "Turn on admin to view."}
          </div>
        ) : (
          <>
            <div className="wsk-table-wrap">
              <table className="wsk-table">
                <thead>
                  <tr>
                    <th>User</th>
                    <th style={{ textAlign: "right" }}>Turns</th>
                    <th style={{ textAlign: "right" }}>In</th>
                    <th style={{ textAlign: "right" }}>Out</th>
                    <th style={{ textAlign: "right" }}>Est. USD</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.userId}>
                      <td>{row.userId}</td>
                      <td className="num">{row.turnCount}</td>
                      <td className="num">{row.inputTokens.toLocaleString()}</td>
                      <td className="num">{row.outputTokens.toLocaleString()}</td>
                      <td className="num">${row.estimatedUsd.toFixed(4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p style={{ margin: "12px 0 0", fontSize: 12, color: "var(--wsk-text-faint)", lineHeight: 1.6 }}>
              Backed by a denormalised rollup collection maintained on write, so this is a bounded
              ordered read rather than a scan over every user. Estimates are for spotting outliers,
              not billing.
            </p>
          </>
        )}
      </div>
    </section>
  );
}
