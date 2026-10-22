"use client";

import { useRouter } from "next/navigation";
import { useId, useState } from "react";

type Status = { tone: "good" | "bad"; text: string } | null;

async function readDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === "string" ? body.detail : `HTTP ${res.status}`;
  } catch {
    return `HTTP ${res.status}`;
  }
}

export function SettingsForm({
  allowedUrls,
  currentUrl,
  signedIn,
  fetchImpl = (input, init) => fetch(input, init),
}: {
  allowedUrls: string[];
  currentUrl: string;
  signedIn: boolean;
  fetchImpl?: (input: string, init?: RequestInit) => Promise<Response>;
}) {
  const router = useRouter();
  const [apiUrl, setApiUrl] = useState(currentUrl || allowedUrls[0] || "");
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<Status>(null);
  const [busy, setBusy] = useState(false);
  const urlId = useId();
  const tokenId = useId();
  const tokenHint = useId();

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (token.trim().length < 8) {
      setStatus({ tone: "bad", text: "Enter an API token (at least 8 characters)." });
      return;
    }
    setBusy(true);
    setStatus(null);
    try {
      const res = await fetchImpl("/api/session", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ apiUrl: apiUrl || undefined, token: token.trim() }),
        credentials: "same-origin",
      });
      if (!res.ok) {
        setStatus({ tone: "bad", text: `Not saved: ${await readDetail(res)}.` });
        return;
      }
      const who = (await res.json()) as { name?: string; role?: string };
      setToken("");
      setStatus({ tone: "good", text: `Signed in as ${who.name ?? "unknown"} (${who.role ?? "?"}).` });
      router.refresh();
    } catch {
      setStatus({ tone: "bad", text: "Not saved: the dashboard server did not respond." });
    } finally {
      setBusy(false);
    }
  }

  async function signOut() {
    setBusy(true);
    try {
      await fetchImpl("/api/session", { method: "DELETE", credentials: "same-origin" });
      setStatus({ tone: "good", text: "Signed out." });
      router.refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={save} noValidate aria-describedby={status ? "settings-status" : undefined}>
      <div className="field">
        <label htmlFor={urlId}>Server URL</label>
        {allowedUrls.length > 1 ? (
          <select id={urlId} value={apiUrl} onChange={(e) => setApiUrl(e.target.value)}>
            {allowedUrls.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
        ) : (
          <input id={urlId} value={apiUrl} readOnly aria-readonly="true" />
        )}
        <p className="hint">Only servers allowlisted by the dashboard operator (SQUADHQ_API_URL_ALLOWLIST) can be used.</p>
      </div>
      <div className="field">
        <label htmlFor={tokenId}>API token</label>
        <input
          id={tokenId}
          type="password"
          autoComplete="off"
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-describedby={tokenHint}
        />
        <p className="hint" id={tokenHint}>
          Create one with <code>squadhq server create-token --role approver</code>.
        </p>
      </div>
      <div className="actions">
        <button type="submit" className="primary" disabled={busy}>
          {signedIn ? "Replace token" : "Sign in"}
        </button>
        {signedIn ? (
          <button type="button" onClick={signOut} disabled={busy}>
            Sign out
          </button>
        ) : null}
      </div>
      <div id="settings-status" aria-live="polite" style={{ marginTop: 12 }}>
        {status ? <div className={`alert ${status.tone}`}>{status.text}</div> : null}
      </div>
    </form>
  );
}
