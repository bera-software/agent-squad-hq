import * as LocalAuthentication from "expo-local-authentication";
import { confirmWithBiometrics } from "../src/biometric";
import { decideWithBiometrics } from "../src/decide";
import { mockApi } from "./helpers";

const level = LocalAuthentication.getEnrolledLevelAsync as jest.Mock;
const auth = LocalAuthentication.authenticateAsync as jest.Mock;

beforeEach(() => {
  level.mockResolvedValue(3);
  auth.mockResolvedValue({ success: true });
});

describe("confirmWithBiometrics", () => {
  it("passes when the user authenticates", async () => {
    expect(await confirmWithBiometrics("ok?")).toEqual({ ok: true });
    expect(auth).toHaveBeenCalledWith(expect.objectContaining({ promptMessage: "ok?" }));
  });

  it("refuses on unsecured devices unless explicitly allowed", async () => {
    level.mockResolvedValue(0);
    expect(await confirmWithBiometrics("x")).toMatchObject({ ok: false, reason: "unavailable" });
    expect(await confirmWithBiometrics("x", { allowUnavailable: true })).toEqual({ ok: true });
  });

  it("reports cancellation", async () => {
    auth.mockResolvedValue({ success: false, error: "user_cancel" });
    expect(await confirmWithBiometrics("x")).toMatchObject({ ok: false, reason: "cancelled" });
  });
});

describe("decideWithBiometrics", () => {
  it("never calls the API when confirmation fails", async () => {
    auth.mockResolvedValue({ success: false, error: "authentication_failed" });
    const api = mockApi();
    const spy = jest.spyOn(api, "decide");
    const res = await decideWithBiometrics(api, "apv-1", "approve", "");
    expect(res.ok).toBe(false);
    expect(spy).not.toHaveBeenCalled();
  });

  it("posts the decision after confirmation and maps 409", async () => {
    const api = mockApi({ failDecisionsFor: ["apv-2"] });
    const ok = await decideWithBiometrics(api, "apv-1", "approve", " lgtm ");
    expect(ok.ok && ok.result.approval.status).toBe("approved");
    expect(ok.ok && ok.result.approval.decision_comment).toBe("lgtm");
    expect(await decideWithBiometrics(api, "apv-2", "reject", "no")).toEqual({
      ok: false,
      error: "Someone already decided this item.",
    });
  });
});
