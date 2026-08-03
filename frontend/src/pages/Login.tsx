/**
 * Sign in / sign up.
 *
 * Two modes, chosen by what the server reports at `/config.json`:
 *   identity-platform -- real email/password against Identity Platform
 *   dev-header        -- pick a username; the backend accepts a header, and only
 *                        while it is running locally
 *
 * The dev path is visible rather than hidden behind a query param, because the
 * whole point of local mode is that a reader can run this without a GCP project.
 */
import { useState } from "react";
import { useAuth } from "../lib/auth";

type Tab = "signin" | "signup";

export function Login() {
  const { config, error, signIn, signUp, signInLocal } = useAuth();
  const [tab, setTab] = useState<Tab>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [devUser, setDevUser] = useState("dev-user");
  const [devAdmin, setDevAdmin] = useState(false);
  const [busy, setBusy] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);

  const isDev = config?.provider === "dev-header";

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setLocalError(null);

    if (password.length < 6) {
      setLocalError("Password must be at least 6 characters.");
      return;
    }

    setBusy(true);
    try {
      if (tab === "signin") await signIn(email.trim(), password);
      else await signUp(email.trim(), password);
    } catch {
      // `useAuth` already surfaced the message; this just releases the button.
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="wsk-login">
      <div className="wsk-login-card">
        <div className="wsk-brand" style={{ justifyContent: "center", marginBottom: 6 }}>
          <span>Whisk Studio</span>
          <span>GCP</span>
        </div>
        <p className="wsk-login-tagline">
          An anime image studio on serverless GCP — Gemini agent, AnimagineXL, zero idle cost.
        </p>

        {isDev ? (
          <>
            <div className="wsk-login-note">
              Running in <code>local</code> mode: no Identity Platform project, no cloud calls.
              Pick any name — the backend accepts it as a header, and that path is disabled once
              deployed.
            </div>
            <form
              className="wsk-stack"
              style={{ gap: 12 }}
              onSubmit={(event) => {
                event.preventDefault();
                signInLocal(devUser, devAdmin);
              }}
            >
              <div>
                <label className="wsk-label" htmlFor="devuser">
                  Username
                </label>
                <input
                  id="devuser"
                  className="wsk-input"
                  value={devUser}
                  onChange={(event) => setDevUser(event.target.value)}
                  autoFocus
                />
              </div>
              <label className="wsk-check">
                <input
                  type="checkbox"
                  checked={devAdmin}
                  onChange={(event) => setDevAdmin(event.target.checked)}
                />
                <span>Sign in as admin (unlocks the Sanctum tab)</span>
              </label>
              <button className="wsk-btn" type="submit">
                Enter the studio
              </button>
            </form>
          </>
        ) : (
          <>
            <div className="wsk-login-tabs" role="tablist">
              <button
                role="tab"
                aria-selected={tab === "signin"}
                className="wsk-tab"
                onClick={() => setTab("signin")}
              >
                Sign in
              </button>
              <button
                role="tab"
                aria-selected={tab === "signup"}
                className="wsk-tab"
                onClick={() => setTab("signup")}
              >
                Create account
              </button>
            </div>

            <form className="wsk-stack" style={{ gap: 12 }} onSubmit={submit}>
              <div>
                <label className="wsk-label" htmlFor="email">
                  Email
                </label>
                <input
                  id="email"
                  className="wsk-input"
                  type="email"
                  autoComplete="email"
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  autoFocus
                />
              </div>
              <div>
                <label className="wsk-label" htmlFor="password">
                  Password
                </label>
                <input
                  id="password"
                  className="wsk-input"
                  type="password"
                  autoComplete={tab === "signin" ? "current-password" : "new-password"}
                  required
                  minLength={6}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </div>
              <button className="wsk-btn" type="submit" disabled={busy || !email || !password}>
                {busy ? "…" : tab === "signin" ? "Sign in" : "Create account"}
              </button>
            </form>
          </>
        )}

        {(localError || error) && (
          <div className="wsk-error" style={{ marginTop: 14 }}>
            {localError ?? error}
          </div>
        )}

        <p className="wsk-login-foot">
          {isDev
            ? "Identity Platform replaces this when WHISK_MODE=gcp."
            : "Secured by Google Identity Platform."}
        </p>
      </div>
    </div>
  );
}
