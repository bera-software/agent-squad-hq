import type { ReactNode } from "react";
import { render } from "@testing-library/react-native";
import { createApiClient, type ApiClient } from "@squadhq/shared-api";
import { createMockFetch } from "@squadhq/shared-api/fixtures";
import { SessionContext, type SessionValue } from "../src/SessionContext";

export function mockApi(opts: Parameters<typeof createMockFetch>[0] = {}): ApiClient {
  return createApiClient({ baseUrl: "https://hq.example.com", token: "test-token", fetch: createMockFetch(opts) });
}

export async function renderWithSession(ui: ReactNode, api: ApiClient, overrides: Partial<SessionValue> = {}) {
  const value: SessionValue = {
    status: "ready",
    mode: "server",
    api,
    signIn: jest.fn(async () => null),
    startDemo: jest.fn(async () => undefined),
    unlock: jest.fn(async () => null),
    signOut: jest.fn(async () => undefined),
    ...overrides,
  };
  return await render(<SessionContext.Provider value={value}>{ui}</SessionContext.Provider>);
}
