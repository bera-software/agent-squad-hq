import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { lastRunByAccount } from "@squadhq/shared-api";
import { buildCapacity, buildFixtures, emptyCapacity } from "./helpers";
import { AccountsPanel, POOL_DOCS_URL } from "@/components/AccountsPanel";
import { NAV_ITEMS } from "@/components/Nav";
import { RunsTable } from "@/components/RunsTable";
import { isProxyAllowed } from "@/lib/proxy-policy";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }), usePathname: () => "/accounts" }));

const NOW = new Date("2026-10-21T12:00:00Z");

describe("AccountsPanel", () => {
  it("lists accounts in strategy order with status badges and last runs", () => {
    const fx = buildFixtures(NOW);
    render(<AccountsPanel capacity={buildCapacity(NOW)} lastRuns={lastRunByAccount(fx.runs)} now={NOW} />);
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((r) => within(r).getByRole("rowheader").textContent)).toEqual(["team-a", "team-b", "org-key"]);
    const [a, b, c] = rows as [HTMLElement, HTMLElement, HTMLElement];
    expect(a).toHaveTextContent("Subscription");
    expect(a).toHaveTextContent("Limited");
    expect(a).toHaveTextContent("(in 40 min)");
    expect(within(a).getByText((_, el) => el?.tagName === "TIME" && el.getAttribute("dateTime") === "2026-10-21T12:40:00.000Z")).toBeInTheDocument();
    expect(a).toHaveTextContent("implement on Billing");
    expect(b).toHaveTextContent("Available");
    expect(c).toHaveTextContent("API key");
    expect(c).toHaveTextContent("ANTHROPIC_API_KEY_3");
    expect(screen.getByText("2 of 3")).toBeInTheDocument();
    expect(screen.getByText("Next job: team-b")).toBeInTheDocument();
    expect(screen.getByText("Ordered")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Last run" })).toBeInTheDocument();
    // display only
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("omits the last-run column without run attribution and warns when every account is limited", () => {
    const cap = buildCapacity(NOW);
    const until = "2026-10-21T12:40:00.000Z";
    const all = {
      ...cap,
      available: 0,
      all_tripped_until: until,
      accounts: cap.accounts.map((a) => ({ ...a, state: "tripped", tripped_until: until })),
    };
    render(<AccountsPanel capacity={all} now={NOW} />);
    expect(screen.queryByRole("columnheader", { name: "Last run" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Every account is limited. Jobs are paused until");
    expect(screen.getAllByText("Limited")).toHaveLength(3);
  });

  it("explains how to configure a pool when none is configured", () => {
    render(<AccountsPanel capacity={emptyCapacity()} now={NOW} />);
    expect(screen.getByRole("heading", { name: "No account pool configured" })).toBeInTheDocument();
    expect(screen.getByText(/pool_strategy: ordered/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "Account pool" });
    expect(link).toHaveAttribute("href", POOL_DOCS_URL);
    expect(POOL_DOCS_URL).toMatch(/#account-pool$/);
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("accounts wiring", () => {
  it("has a nav entry and a GET-only proxy route", () => {
    expect(NAV_ITEMS.some((i) => i.href === "/accounts")).toBe(true);
    expect(isProxyAllowed("GET", "capacity")).toBe(true);
    expect(isProxyAllowed("POST", "capacity")).toBe(false);
    expect(isProxyAllowed("GET", "capacity/x")).toBe(false);
  });
});

describe("RunsTable account column", () => {
  it("shows the account only when runs carry one", () => {
    const runs = buildFixtures(NOW).runs;
    const { unmount } = render(<RunsTable runs={runs} />);
    expect(screen.getByRole("columnheader", { name: "Account" })).toBeInTheDocument();
    expect(screen.getAllByText("team-a").length).toBeGreaterThan(0);
    unmount();
    render(<RunsTable runs={runs.map((r) => ({ ...r, data: {} }))} />);
    expect(screen.queryByRole("columnheader", { name: "Account" })).toBeNull();
  });
});
