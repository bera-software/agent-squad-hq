import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError, buildFixtures } from "./helpers";
import { ApprovalsInbox } from "@/components/ApprovalsInbox";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: vi.fn() }), usePathname: () => "/approvals" }));

const approvals = buildFixtures(new Date("2026-10-21T12:00:00Z")).approvals;

describe("ApprovalsInbox", () => {
  it("asks for confirmation, then removes the item optimistically", async () => {
    let resolve: () => void = () => {};
    const decide = vi.fn(
      () =>
        new Promise<never>((r) => {
          resolve = () => r(undefined as never);
        }),
    );
    const user = userEvent.setup();
    render(<ApprovalsInbox initial={approvals} api={{ decide }} />);

    await user.click(screen.getByRole("button", { name: /^Approve Merge: Add guest checkout/ }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByRole("heading")).toHaveTextContent("Approve “Merge: Add guest checkout”?");
    await user.type(within(dialog).getByLabelText(/Comment/), "looks good");
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));

    expect(decide).toHaveBeenCalledWith("apv-1", { decision: "approve", comment: "looks good" });
    expect(screen.queryByRole("heading", { name: "Merge: Add guest checkout" })).not.toBeInTheDocument();
    resolve();
    expect(await screen.findByText(/Approved: Merge: Add guest checkout/)).toBeInTheDocument();
  });

  it("rolls back and explains when the server refuses", async () => {
    const decide = vi.fn(async () => {
      throw new ApiError({ status: 409, code: "http", path: "/approvals/apv-3/decision", detail: "already decided" });
    });
    const user = userEvent.setup();
    render(<ApprovalsInbox initial={approvals} api={{ decide }} />);
    await user.click(screen.getByRole("button", { name: /^Approve Deploy Billing/ }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve" }));
    expect(await screen.findByText(/Could not approve "Deploy Billing v2.4.0 to production"/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("heading", { name: "Deploy Billing v2.4.0 to production" })).toBeInTheDocument());
    // Order is preserved on rollback.
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings.indexOf("Deploy Billing v2.4.0 to production")).toBe(2);
  });

  it("requires a comment to reject", async () => {
    const decide = vi.fn();
    const user = userEvent.setup();
    render(<ApprovalsInbox initial={approvals} api={{ decide }} />);
    await user.click(screen.getByRole("button", { name: /^Reject Proposal: Dark mode/ }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Reject" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Please say why");
    expect(decide).not.toHaveBeenCalled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByRole("heading", { name: "Proposal: Dark mode polish (S)" })).toBeInTheDocument();
  });

  it("labels escalations as acknowledge and filters by kind", async () => {
    const user = userEvent.setup();
    render(<ApprovalsInbox initial={approvals} api={{ decide: vi.fn() }} />);
    await user.click(screen.getByRole("button", { name: "Escalation (1)" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /^Acknowledge Escalation/ })).toBeInTheDocument();
  });

  it("hides decision buttons for viewers", () => {
    render(<ApprovalsInbox initial={approvals} canDecide={false} api={{ decide: vi.fn() }} />);
    expect(screen.queryByRole("button", { name: /^Approve/ })).not.toBeInTheDocument();
  });
});
