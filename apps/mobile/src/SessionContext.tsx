import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { createApiClient, type ApiClient } from "@squadhq/shared-api";
import { createMockFetch } from "@squadhq/shared-api/fixtures";
import { confirmWithBiometrics, isDeviceSecured } from "./biometric";
import { clearSession, loadSession, saveSession, validateSignIn, type SignInInput, type StoredSession } from "./session";

export type SessionStatus = "loading" | "signedOut" | "locked" | "ready";

export interface SessionValue {
  status: SessionStatus;
  mode: "server" | "demo" | null;
  api: ApiClient | null;
  /** Validates the credentials against GET /whoami, then stores them. Returns an error message or null. */
  signIn(input: SignInInput): Promise<string | null>;
  startDemo(): Promise<void>;
  unlock(): Promise<string | null>;
  signOut(): Promise<void>;
}

export const SessionContext = createContext<SessionValue | null>(null);

export function useSession(): SessionValue {
  const v = useContext(SessionContext);
  if (!v) throw new Error("useSession must be used inside <SessionProvider>");
  return v;
}

/** The API client for a signed-in screen. */
export function useApi(): ApiClient {
  const { api } = useSession();
  if (!api) throw new Error("not signed in");
  return api;
}

export function clientFor(session: StoredSession): ApiClient {
  if (session.mode === "demo") {
    return createApiClient({ baseUrl: "", fetch: createMockFetch({ latencyMs: 150 }) });
  }
  return createApiClient({ baseUrl: session.baseUrl, token: session.token });
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("loading");
  const [session, setSession] = useState<StoredSession | null>(null);

  useEffect(() => {
    let alive = true;
    loadSession()
      .then((s) => {
        if (!alive) return;
        setSession(s);
        setStatus(s ? "locked" : "signedOut");
      })
      .catch(() => alive && setStatus("signedOut"));
    return () => {
      alive = false;
    };
  }, []);

  const api = useMemo(() => (session ? clientFor(session) : null), [session]);

  const signIn = useCallback(async (input: SignInInput) => {
    const v = validateSignIn(input);
    if (!v.ok) return v.error;
    const next: StoredSession = { mode: "server", baseUrl: v.baseUrl, token: v.token };
    try {
      await clientFor(next).whoami();
    } catch (e) {
      const status = (e as { status?: number }).status;
      if (status === 401 || status === 403) return "The server rejected this token.";
      return "Could not reach the server. Check the URL and your connection.";
    }
    await saveSession(next);
    setSession(next);
    setStatus("ready");
    return null;
  }, []);

  const startDemo = useCallback(async () => {
    const next: StoredSession = { mode: "demo" };
    await saveSession(next);
    setSession(next);
    setStatus("ready");
  }, []);

  const unlock = useCallback(async () => {
    // A device without biometrics or a passcode cannot be locked; the token is
    // still protected at rest by the keystore. Approvals remain gated separately.
    if (!(await isDeviceSecured())) {
      setStatus("ready");
      return null;
    }
    const res = await confirmWithBiometrics("Unlock Agent Squad HQ");
    if (res.ok) {
      setStatus("ready");
      return null;
    }
    return res.message;
  }, []);

  const signOut = useCallback(async () => {
    await clearSession();
    setSession(null);
    setStatus("signedOut");
  }, []);

  const value = useMemo<SessionValue>(
    () => ({ status, mode: session?.mode ?? null, api, signIn, startDemo, unlock, signOut }),
    [status, session, api, signIn, startDemo, unlock, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
