import { useCallback, useEffect, useState } from "react";
import { MediaGrid } from "./components/MediaGrid";
import { api, setAuthHeaderSource } from "./lib/api";
import type { Media } from "./lib/api";
import { useAuth } from "./lib/auth";
import { THEMES, useTheme } from "./lib/theme";
import { Companion } from "./pages/Companion";
import { Docs } from "./pages/Docs";
import { Login } from "./pages/Login";
import { Sanctum } from "./pages/Sanctum";
import { Studio } from "./pages/Studio";

const TABS = ["studio", "companion", "gallery", "docs", "sanctum"] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const { ready, account, config, signOut, getToken } = useAuth();
  const [tab, setTab] = useState<Tab>("studio");
  // Bumped whenever media changes anywhere, so sibling views refetch without a
  // store: the data is small and server-owned, and a cache here would just be a
  // second source of truth to keep honest.
  const [version, setVersion] = useState(0);
  const [mode, setMode] = useState("local");
  const { theme, scheme, setTheme, toggleScheme } = useTheme();

  const bump = useCallback(() => setVersion((v) => v + 1), []);

  // Install the auth header source once the mode is known. Every API call goes
  // through it, so no call site needs to know whether it is sending a bearer
  // token or a dev header.
  useEffect(() => {
    if (!account || !config) return;
    if (config.provider === "identity-platform") {
      setAuthHeaderSource(async () => {
        const token = await getToken();
        const headers: Record<string, string> = {};
        if (token) headers.Authorization = `Bearer ${token}`;
        return headers;
      });
    } else {
      setAuthHeaderSource(async () => {
        const headers: Record<string, string> = { "X-Whisk-User": account.uid };
        if (account.isAdmin) headers["X-Whisk-Admin"] = "true";
        return headers;
      });
    }
    bump();
  }, [account, config, getToken, bump]);

  useEffect(() => {
    if (!account) return;
    void api
      .surfaces()
      .then((info) => setMode(info.mode))
      .catch(() => setMode("unknown"));
  }, [account]);

  if (!ready) {
    return (
      <div className="wsk-login">
        <span className="wsk-spin" />
      </div>
    );
  }

  if (!account) return <Login />;

  // The admin tab is hidden for non-admins as a courtesy, not a control: the
  // routes behind it are gated server-side and a hand-crafted request still 403s.
  const visibleTabs = TABS.filter((name) => name !== "sanctum" || account.isAdmin);

  return (
    <div className="wsk-app">
      <header className="wsk-header">
        <div className="wsk-brand">
          <span>Whisk Studio</span>
          <span>GCP</span>
        </div>

        <nav className="wsk-nav" aria-label="Main">
          {visibleTabs.map((name) => (
            <button
              key={name}
              className="wsk-tab"
              aria-current={tab === name ? "page" : undefined}
              onClick={() => setTab(name)}
            >
              {name}
            </button>
          ))}
        </nav>

        <div className="wsk-row" style={{ gap: 8, width: "100%" }}>
          <div className="wsk-swatches" role="group" aria-label="Theme">
            {THEMES.map((name) => (
              <button
                key={name}
                className="wsk-swatch"
                aria-label={name}
                aria-pressed={theme === name}
                title={name}
                onClick={() => setTheme(name)}
                style={{ background: swatch(name) }}
              />
            ))}
          </div>
          <button className="wsk-btn ghost sm" onClick={toggleScheme}>
            {scheme === "dark" ? "☾ dark" : "☀ light"}
          </button>
          <div className="wsk-row" style={{ gap: 8, marginLeft: "auto" }}>
            <span className="wsk-chip" title={account.email}>
              {account.email}
            </span>
            <button className="wsk-btn ghost sm" onClick={signOut}>
              Sign out
            </button>
          </div>
        </div>
      </header>

      <main className="wsk-main">
        {tab === "studio" && <Studio version={version} onMutate={bump} />}
        {tab === "companion" && <Companion onMutate={bump} />}
        {tab === "gallery" && <Gallery version={version} />}
        {tab === "docs" && <Docs />}
        {tab === "sanctum" && account.isAdmin && <Sanctum />}
      </main>

      <footer className="wsk-footer">
        <span>
          mode <code>{mode}</code>
        </span>
        <span>FastAPI + ADK · Gemini · AnimagineXL · Firestore · GCS · Cloud Run</span>
        <span>
          <code>/api</code> REST · <code>/a2a/critic</code> A2A · <code>/mcp</code> MCP
        </span>
      </footer>
    </div>
  );
}

function Gallery({ version }: { version: number }) {
  const [items, setItems] = useState<Media[]>([]);

  useEffect(() => {
    void api
      .gallery()
      .then((result) => setItems(result.items))
      .catch(() => setItems([]));
  }, [version]);

  return (
    <section>
      <h1 className="wsk-h1">Community gallery</h1>
      <p className="wsk-sub">
        The one place media is world-readable. Sharing copies the object into a public prefix rather
        than moving it, so a later delete of the original cannot dangle a public URL.
      </p>
      <div className="wsk-card">
        <MediaGrid items={items} emptyLabel="Nothing shared yet — share something from Studio." />
      </div>
    </section>
  );
}

/** Preview colour for a theme, matching the hues in tokens.css. */
function swatch(name: string): string {
  const hues: Record<string, number> = {
    sakura: 340, moonrise: 232, bamboo: 146, ember: 22, void: 268,
    glacier: 194, dusk: 312, aurora: 162, crimson: 356, storm: 210,
  };
  return `hsl(${hues[name] ?? 340} 60% 60%)`;
}
