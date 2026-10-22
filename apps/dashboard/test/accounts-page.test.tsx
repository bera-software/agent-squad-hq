import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({ cookies: async () => ({ get: () => undefined }) }));
vi.mock("next/navigation", () => ({
  redirect: (url: string) => {
    throw new Error(`NEXT_REDIRECT ${url}`);
  },
}));

const { default: AccountsPage } = await import("@/app/accounts/page");

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("Accounts page", () => {
  it("renders the mock pool (MOCK_API=1) with one limited account", async () => {
    vi.stubEnv("MOCK_API", "1");
    render(await AccountsPage());
    expect(screen.getByRole("heading", { level: 1, name: "Accounts" })).toBeInTheDocument();
    expect(screen.getByRole("rowheader", { name: "team-a" })).toBeInTheDocument();
    expect(screen.getByText("Limited")).toBeInTheDocument();
    expect(screen.getAllByText("Available")).toHaveLength(2);
    expect(screen.getByText(/\(in (39|40) min\)/)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Last run" })).toBeInTheDocument();
  });

  it("shows the not-configured notice without a server", async () => {
    vi.stubEnv("MOCK_API", "0");
    vi.stubEnv("SQUADHQ_API_URL", "");
    vi.stubEnv("SQUADHQ_API_URL_ALLOWLIST", "");
    render(await AccountsPage());
    expect(screen.getByRole("status")).toHaveTextContent("No server configured.");
  });
});
