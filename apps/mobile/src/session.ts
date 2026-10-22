/**
 * Persisted sign-in state. The bearer token only ever lives in the platform
 * keychain/keystore via expo-secure-store and is never logged or rendered.
 */
import * as SecureStore from "expo-secure-store";
import { normalizeBaseUrl } from "@squadhq/shared-api";

export type StoredSession =
  | { mode: "server"; baseUrl: string; token: string }
  | { mode: "demo" };

const KEY_MODE = "squadhq.mode";
const KEY_URL = "squadhq.serverUrl";
const KEY_TOKEN = "squadhq.token";

const OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
};

export interface SignInInput {
  serverUrl: string;
  token: string;
}

export type ValidationResult = { ok: true; baseUrl: string; token: string } | { ok: false; error: string };

/** Validate the sign-in form. Plain http is allowed only for local development hosts. */
export function validateSignIn(input: SignInInput): ValidationResult {
  const token = input.token.trim();
  const raw = input.serverUrl.trim();
  if (!raw) return { ok: false, error: "Enter the server URL." };
  if (!token) return { ok: false, error: "Enter an API token." };
  if (/\s/.test(token)) return { ok: false, error: "The token must not contain spaces." };
  let baseUrl: string;
  try {
    baseUrl = normalizeBaseUrl(raw);
  } catch {
    return { ok: false, error: "The server URL must start with https://" };
  }
  const isLocal = /^http:\/\/(localhost|127\.0\.0\.1|10\.0\.2\.2|192\.168\.\d+\.\d+)(:\d+)?$/.test(baseUrl);
  if (!baseUrl.startsWith("https://") && !isLocal) {
    return { ok: false, error: "Use https:// (plain http is only allowed for local development)." };
  }
  return { ok: true, baseUrl, token };
}

export async function saveSession(session: StoredSession): Promise<void> {
  await SecureStore.setItemAsync(KEY_MODE, session.mode, OPTIONS);
  if (session.mode === "server") {
    await SecureStore.setItemAsync(KEY_URL, session.baseUrl, OPTIONS);
    await SecureStore.setItemAsync(KEY_TOKEN, session.token, OPTIONS);
  } else {
    await SecureStore.deleteItemAsync(KEY_URL, OPTIONS);
    await SecureStore.deleteItemAsync(KEY_TOKEN, OPTIONS);
  }
}

export async function loadSession(): Promise<StoredSession | null> {
  const mode = await SecureStore.getItemAsync(KEY_MODE, OPTIONS);
  if (mode === "demo") return { mode: "demo" };
  if (mode !== "server") return null;
  const baseUrl = await SecureStore.getItemAsync(KEY_URL, OPTIONS);
  const token = await SecureStore.getItemAsync(KEY_TOKEN, OPTIONS);
  if (!baseUrl || !token) return null;
  return { mode: "server", baseUrl, token };
}

export async function clearSession(): Promise<void> {
  await Promise.all([KEY_MODE, KEY_URL, KEY_TOKEN].map((k) => SecureStore.deleteItemAsync(k, OPTIONS)));
}
