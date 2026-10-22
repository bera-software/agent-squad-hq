import "server-only";
import { createCipheriv, createDecipheriv, createHash, randomBytes } from "node:crypto";

/**
 * Session cookies. The API token lives only in an httpOnly, SameSite=Strict
 * cookie. When SQUADHQ_SESSION_SECRET is set (recommended, required in
 * production deployments) the value is sealed with AES-256-GCM.
 */
export const TOKEN_COOKIE = "squadhq_token";
export const API_URL_COOKIE = "squadhq_api_url";
export const SESSION_MAX_AGE_S = 60 * 60 * 24 * 7;

const PREFIX_SEALED = "v1.";
const PREFIX_PLAIN = "p1.";

function key(): Buffer | null {
  const secret = process.env.SQUADHQ_SESSION_SECRET;
  if (!secret || secret.length < 16) return null;
  return createHash("sha256").update(secret, "utf8").digest();
}

export function sealToken(token: string): string {
  const k = key();
  if (!k) return PREFIX_PLAIN + Buffer.from(token, "utf8").toString("base64url");
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", k, iv);
  const enc = Buffer.concat([cipher.update(token, "utf8"), cipher.final()]);
  const tag = cipher.getAuthTag();
  return PREFIX_SEALED + Buffer.concat([iv, tag, enc]).toString("base64url");
}

export function unsealToken(value: string | undefined | null): string | null {
  if (!value) return null;
  try {
    if (value.startsWith(PREFIX_PLAIN)) {
      // Refuse plaintext cookies once a secret is configured.
      if (key()) return null;
      return Buffer.from(value.slice(PREFIX_PLAIN.length), "base64url").toString("utf8") || null;
    }
    if (value.startsWith(PREFIX_SEALED)) {
      const k = key();
      if (!k) return null;
      const raw = Buffer.from(value.slice(PREFIX_SEALED.length), "base64url");
      if (raw.length < 29) return null;
      const decipher = createDecipheriv("aes-256-gcm", k, raw.subarray(0, 12));
      decipher.setAuthTag(raw.subarray(12, 28));
      return Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString("utf8") || null;
    }
  } catch {
    return null;
  }
  return null;
}

export function cookieOptions(secure: boolean) {
  return {
    httpOnly: true,
    secure,
    sameSite: "strict" as const,
    path: "/",
    maxAge: SESSION_MAX_AGE_S,
  };
}

/** Same-origin check for state-changing requests (CSRF defence on top of SameSite=Strict). */
export function isSameOrigin(req: Request): boolean {
  const origin = req.headers.get("origin");
  const site = req.headers.get("sec-fetch-site");
  if (site && site !== "same-origin") return false;
  if (!origin) return site === "same-origin";
  const host = req.headers.get("x-forwarded-host") ?? req.headers.get("host");
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}
