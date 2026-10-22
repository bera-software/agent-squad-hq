import "server-only";
import { createMockFetch } from "@squadhq/shared-api/fixtures";

type MockFetch = ReturnType<typeof createMockFetch>;
const g = globalThis as typeof globalThis & { __squadhqMockFetch?: MockFetch };

/** One in-memory mock per server process, so decisions persist across requests. */
export function mockFetch(): MockFetch {
  g.__squadhqMockFetch ??= createMockFetch();
  return g.__squadhqMockFetch;
}
