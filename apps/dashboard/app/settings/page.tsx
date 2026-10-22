import type { Metadata } from "next";
import { PageHeader } from "@/components/ErrorState";
import { SettingsForm } from "@/components/SettingsForm";
import { allowedApiUrls, isMockMode } from "@/lib/config";
import { getConnection, settle } from "@/lib/server-api";

export const metadata: Metadata = { title: "Settings" };
export const dynamic = "force-dynamic";

type Props = { searchParams: Promise<{ signin?: string }> };

export default async function SettingsPage({ searchParams }: Props) {
  const { signin } = await searchParams;
  const mock = isMockMode();
  const conn = await getConnection({ redirectToSignIn: false });
  const who = conn.mode === "unconfigured" ? null : await settle(conn.api.whoami());
  const status =
    conn.mode === "mock"
      ? "Mock mode is on (MOCK_API=1). No server or token is used."
      : conn.mode === "unconfigured"
        ? conn.reason === "no_api_url"
          ? "No server configured. Set SQUADHQ_API_URL on the dashboard host."
          : "Not signed in."
        : who?.ok
          ? `Signed in to ${conn.apiUrl} as ${who.value.name} (${who.value.role}).`
          : `A token is stored for ${conn.apiUrl}, but the server did not accept it.`;

  return (
    <>
      <PageHeader title="Settings" />
      {signin === "required" && conn.mode === "unconfigured" && conn.reason === "no_token" ? (
        <div className="alert warn" role="status">
          Sign in with an API token to use the dashboard.
        </div>
      ) : null}
      <section className="card" aria-labelledby="conn-h" style={{ maxWidth: 640 }}>
        <h2 id="conn-h">Server connection</h2>
        <p role="status">{status}</p>
        {mock ? null : (
          <SettingsForm
            allowedUrls={allowedApiUrls()}
            currentUrl={conn.apiUrl ?? ""}
            signedIn={conn.mode === "live"}
          />
        )}
        <p className="hint">
          Your API token is sent once to this dashboard&apos;s server and kept in an httpOnly, SameSite=Strict cookie
          {process.env.SQUADHQ_SESSION_SECRET ? " (encrypted)" : ""}. Browser scripts never see it; requests to the
          Agent Squad HQ server are made server-side.
        </p>
      </section>
    </>
  );
}
