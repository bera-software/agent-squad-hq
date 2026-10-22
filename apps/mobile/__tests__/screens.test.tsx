import * as LocalAuthentication from "expo-local-authentication";
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApprovalListScreen } from "../src/screens/ApprovalListScreen";
import { ApprovalDetailScreen } from "../src/screens/ApprovalDetailScreen";
import { SignInScreen } from "../src/screens/SignInScreen";
import { BriefScreen } from "../src/screens/BriefScreen";
import { SessionProvider } from "../src/SessionContext";
import { mockApi, renderWithSession } from "./helpers";

const auth = LocalAuthentication.authenticateAsync as jest.Mock;

beforeEach(() => {
  auth.mockResolvedValue({ success: true });
});

describe("ApprovalListScreen", () => {
  it("lists non-escalation approvals and opens details", async () => {
    const onOpen = jest.fn();
    await renderWithSession(<ApprovalListScreen kind="approvals" onOpen={onOpen} />, mockApi());
    expect(await screen.findByText("Merge: Add guest checkout")).toBeTruthy();
    expect(screen.queryByText(/Stripe webhook retries failing/)).toBeNull();
    await fireEvent.press(screen.getByTestId("open-apv-1"));
    expect(onOpen).toHaveBeenCalledWith("apv-1");
  });

  it("acknowledges an escalation only after biometric confirmation", async () => {
    const api = mockApi();
    const decide = jest.spyOn(api, "decide");
    await renderWithSession(<ApprovalListScreen kind="escalations" />, api);
    await fireEvent.press(await screen.findByTestId("ack-apv-4"));
    await waitFor(() => expect(screen.queryByTestId("ack-apv-4")).toBeNull());
    expect(auth).toHaveBeenCalled();
    expect(decide).toHaveBeenCalledWith("apv-4", { decision: "approve", comment: "acknowledged" });
  });
});

describe("ApprovalDetailScreen", () => {
  it("approves after biometric success", async () => {
    const api = mockApi();
    const decide = jest.spyOn(api, "decide");
    await renderWithSession(<ApprovalDetailScreen id="apv-1" />, api);
    await fireEvent.changeText(await screen.findByTestId("comment"), "ship it");
    await fireEvent.press(screen.getByTestId("approve"));
    expect(await screen.findByTestId("decided")).toHaveTextContent("Recorded: approved");
    expect(decide).toHaveBeenCalledWith("apv-1", { decision: "approve", comment: "ship it" });
  });

  it("does not send when biometric confirmation is cancelled", async () => {
    auth.mockResolvedValue({ success: false, error: "user_cancel" });
    const api = mockApi();
    const decide = jest.spyOn(api, "decide");
    await renderWithSession(<ApprovalDetailScreen id="apv-1" />, api);
    await fireEvent.press(await screen.findByTestId("approve"));
    expect(await screen.findByText("Confirmation cancelled.")).toBeTruthy();
    expect(decide).not.toHaveBeenCalled();
  });

  it("requires a comment to reject", async () => {
    const api = mockApi();
    const decide = jest.spyOn(api, "decide");
    await renderWithSession(<ApprovalDetailScreen id="apv-2" />, api);
    await fireEvent.press(await screen.findByTestId("reject"));
    expect(await screen.findByText("Add a comment explaining the rejection.")).toBeTruthy();
    expect(decide).not.toHaveBeenCalled();
  });
});

describe("SignInScreen", () => {
  it("validates input before contacting a server", async () => {
    const fetchSpy = jest.spyOn(globalThis, "fetch");
    await render(
      <SessionProvider>
        <SignInScreen />
      </SessionProvider>,
    );
    await fireEvent.changeText(await screen.findByTestId("server-url"), "http://hq.example.com");
    await fireEvent.changeText(screen.getByTestId("token"), "abc");
    await fireEvent.press(screen.getByTestId("sign-in"));
    expect(await screen.findByText(/Use https:\/\//)).toBeTruthy();
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});

describe("BriefScreen", () => {
  it("renders markdown as text and never as markup", async () => {
    await renderWithSession(<BriefScreen />, mockApi());
    expect(await screen.findByText("Daily brief")).toBeTruthy();
    expect(screen.getByText("<script>alert('this must never execute')</script>")).toBeTruthy();
    expect(screen.getByRole("link", { name: "Open the pipeline" })).toBeTruthy();
  });
});
