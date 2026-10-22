import * as SecureStore from "expo-secure-store";
import { clearSession, loadSession, saveSession, validateSignIn } from "../src/session";

describe("validateSignIn", () => {
  it("requires both fields", () => {
    expect(validateSignIn({ serverUrl: "", token: "t" })).toEqual({ ok: false, error: "Enter the server URL." });
    expect(validateSignIn({ serverUrl: "https://hq.example.com", token: " " })).toEqual({ ok: false, error: "Enter an API token." });
  });

  it("requires https except for local development hosts", () => {
    expect(validateSignIn({ serverUrl: "http://hq.example.com", token: "t" }).ok).toBe(false);
    expect(validateSignIn({ serverUrl: "ftp://hq.example.com", token: "t" }).ok).toBe(false);
    expect(validateSignIn({ serverUrl: "http://localhost:8080", token: "t" }).ok).toBe(true);
    expect(validateSignIn({ serverUrl: "https://hq.example.com/api/v1/", token: " tok " })).toEqual({
      ok: true,
      baseUrl: "https://hq.example.com",
      token: "tok",
    });
  });
});

describe("secure storage", () => {
  it("round-trips a server session with device-only keychain access", async () => {
    await saveSession({ mode: "server", baseUrl: "https://hq.example.com", token: "secret-value" });
    expect(SecureStore.setItemAsync).toHaveBeenCalledWith("squadhq.token", "secret-value", {
      keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY,
    });
    expect(await loadSession()).toEqual({ mode: "server", baseUrl: "https://hq.example.com", token: "secret-value" });
    await clearSession();
    expect(await loadSession()).toBeNull();
  });

  it("stores demo mode without credentials", async () => {
    await saveSession({ mode: "server", baseUrl: "https://hq.example.com", token: "x" });
    await saveSession({ mode: "demo" });
    expect(await loadSession()).toEqual({ mode: "demo" });
    expect(await SecureStore.getItemAsync("squadhq.token")).toBeNull();
  });
});
