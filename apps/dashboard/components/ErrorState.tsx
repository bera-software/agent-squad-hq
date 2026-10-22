import Link from "next/link";
import { ApiError } from "@squadhq/shared-api";

export function describeError(error: unknown): { title: string; detail: string; signIn: boolean } {
  if (error instanceof ApiError) {
    if (error.status === 401) return { title: "Not signed in", detail: "Your API token is missing, expired or revoked.", signIn: true };
    if (error.status === 403) return { title: "Not allowed", detail: "Your token's role cannot view this.", signIn: false };
    if (error.code === "network" || error.code === "timeout")
      return { title: "Server unreachable", detail: "The Agent Squad HQ server did not respond.", signIn: false };
    if (error.code === "invalid_response")
      return { title: "Unexpected response", detail: "The server's response did not match API v1.", signIn: false };
    return { title: `Request failed (${error.status})`, detail: error.detail, signIn: false };
  }
  return { title: "Something went wrong", detail: "An unexpected error occurred.", signIn: false };
}

export function ErrorState({ error, what }: { error: unknown; what?: string }) {
  const d = describeError(error);
  return (
    <div className="alert bad" role="alert">
      <strong>
        {d.title}
        {what ? ` while loading ${what}` : ""}.
      </strong>{" "}
      {d.detail}{" "}
      {d.signIn ? <Link href="/settings">Go to Settings</Link> : null}
    </div>
  );
}

export function NotConfigured({ reason }: { reason: "no_api_url" | "no_token" }) {
  return (
    <div className="alert warn" role="status">
      <strong>{reason === "no_api_url" ? "No server configured." : "Not signed in."}</strong>{" "}
      {reason === "no_api_url" ? (
        <>Set SQUADHQ_API_URL on the dashboard server (or MOCK_API=1 to explore with sample data).</>
      ) : (
        <>
          Add an API token on the <Link href="/settings">Settings</Link> page.
        </>
      )}
    </div>
  );
}

export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="page-header">
      <h1>{title}</h1>
      {children}
    </div>
  );
}
