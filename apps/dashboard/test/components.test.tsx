import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { buildFixtures } from "./helpers";
import { BarChart } from "@/components/BarChart";
import { Markdown } from "@/components/Markdown";
import { RunsTable } from "@/components/RunsTable";
import { SettingsForm } from "@/components/SettingsForm";
import { describeError } from "@/components/ErrorState";
import { ApiError } from "@squadhq/shared-api";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh }), usePathname: () => "/" }));

describe("Markdown", () => {
  it("renders markdown and never executes or injects HTML", () => {
    const { container } = render(
      <Markdown source={"# Brief\n\n**bold** [ok](https://example.com) [bad](javascript:alert(1))\n\n<script>alert(1)</script><img src=x onerror=alert(1)>"} />,
    );
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Brief");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.innerHTML).not.toContain("onerror");
    expect(screen.getByRole("link", { name: "ok" })).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(container.innerHTML).not.toContain("javascript:");
  });
});

describe("BarChart", () => {
  it("is labelled, sorted and has a table alternative", () => {
    render(
      <BarChart
        title="By project"
        bars={[
          { label: "A", value: 1, target: 5 },
          { label: "B", value: 3 },
        ]}
        format={(n) => `$${n}`}
      />,
    );
    const img = screen.getByRole("img", { name: /By project/ });
    expect(img).toBeInTheDocument();
    expect(img.querySelector("desc")?.textContent).toBe("B: $3; A: $1 of $5");
    const rows = screen.getAllByRole("rowheader").map((r) => r.textContent);
    expect(rows).toEqual(["B", "A"]);
  });

  it("handles empty data", () => {
    render(<BarChart title="By model" bars={[]} format={String} />);
    expect(screen.getByText("No spend recorded.")).toBeInTheDocument();
  });
});

describe("RunsTable", () => {
  it("opens a detail drawer and restores focus on close", async () => {
    const user = userEvent.setup();
    const runs = buildFixtures(new Date("2026-10-21T12:00:00Z")).runs;
    render(<RunsTable runs={runs} />);
    expect(screen.getAllByRole("row")).toHaveLength(runs.length + 1);
    const btn = screen.getByRole("button", { name: "Details for implement run on Billing" });
    await user.click(btn);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("run-004");
    expect(dialog).toHaveTextContent("tests failed after 3 fix rounds");
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(btn).toHaveFocus();
  });
});

describe("SettingsForm", () => {
  it("validates the token and posts to the session route", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ ok: true, name: "alice", role: "approver" }), { status: 200 }));
    render(<SettingsForm allowedUrls={["https://hq.example.com"]} currentUrl="" signedIn={false} fetchImpl={fetchImpl} />);
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText(/Enter an API token/)).toBeInTheDocument();
    expect(fetchImpl).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("API token"), "sqhq_test_token_123");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Signed in as alice (approver).")).toBeInTheDocument();
    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/session");
    expect(JSON.parse(String(init.body))).toEqual({ apiUrl: "https://hq.example.com", token: "sqhq_test_token_123" });
    expect(screen.getByLabelText("API token")).toHaveValue("");
    expect(refresh).toHaveBeenCalled();
  });

  it("shows the server's refusal", async () => {
    const user = userEvent.setup();
    const fetchImpl = vi.fn(async () => new Response(JSON.stringify({ detail: "the server rejected this token" }), { status: 401 }));
    render(<SettingsForm allowedUrls={["https://hq.example.com"]} currentUrl="" signedIn={false} fetchImpl={fetchImpl} />);
    await user.type(screen.getByLabelText("API token"), "wrong-token-1");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Not saved: the server rejected this token.")).toBeInTheDocument();
  });
});

describe("describeError", () => {
  it("maps API errors to user-facing messages", () => {
    expect(describeError(new ApiError({ status: 401, code: "http", path: "/x", detail: "" })).signIn).toBe(true);
    expect(describeError(new ApiError({ status: 0, code: "timeout", path: "/x", detail: "" })).title).toBe("Server unreachable");
    expect(describeError(new Error("boom")).title).toBe("Something went wrong");
  });
});
