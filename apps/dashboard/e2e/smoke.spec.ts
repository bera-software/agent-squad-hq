import { expect, test } from "@playwright/test";

// Runs against `next start` with MOCK_API=1 (see playwright.config.ts).

test("overview renders key figures and security headers", async ({ page }) => {
  const res = await page.goto("/");
  expect(res?.status()).toBe(200);
  const headers = res?.headers() ?? {};
  expect(headers["content-security-policy"]).toContain("frame-ancestors 'none'");
  expect(headers["content-security-policy"]).toMatch(/'nonce-[^']+'/);
  expect(headers["referrer-policy"]).toBe("no-referrer");
  expect(headers["x-frame-options"]).toBe("DENY");
  await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
  await expect(page.getByText("Runs today (UTC)")).toBeVisible();
  await expect(page.getByText("Mock mode: showing sample data")).toBeVisible();
});

test("keyboard user can approve an item through the confirmation dialog", async ({ page }) => {
  const errors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });
  await page.goto("/approvals");
  const approve = page.getByRole("button", { name: "Approve Proposal: Dark mode polish (S)" });
  await approve.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await page.keyboard.type("ok from e2e");
  await dialog.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Approved: Proposal: Dark mode polish (S)")).toBeVisible();
  expect(errors.filter((e) => e.includes("Content Security Policy"))).toEqual([]);
});

test("every page loads without errors", async ({ page }) => {
  for (const [path, heading] of [
    ["/pipeline", "Pipeline"],
    ["/runs", "Runs"],
    ["/costs", "Costs"],
    ["/accounts", "Accounts"],
    ["/audit", "Audit log"],
    ["/brief", "Daily brief"],
    ["/settings", "Settings"],
  ] as const) {
    const res = await page.goto(path);
    expect(res?.status(), path).toBe(200);
    await expect(page.getByRole("heading", { level: 1, name: heading })).toBeVisible();
  }
  await page.goto("/brief");
  expect(await page.locator("main script").count()).toBe(0);
});

test("accounts page shows the mock pool read-only", async ({ page, request }) => {
  await page.goto("/accounts");
  await expect(page.getByRole("heading", { level: 1, name: "Accounts" })).toBeVisible();
  await expect(page.getByRole("rowheader", { name: "team-a" })).toBeVisible();
  await expect(page.getByText("Limited", { exact: true })).toBeVisible();
  await expect(page.getByText(/\(in \d+ min\)/)).toBeVisible();
  expect(await page.locator("main button").count()).toBe(0);
  expect((await request.get("/api/proxy/capacity")).status()).toBe(200);
  expect((await request.post("/api/proxy/capacity", { data: {} })).status()).not.toBe(200);
});

test("proxy refuses endpoints outside the allowlist", async ({ request }) => {
  const res = await request.get("/api/proxy/tokens");
  expect(res.status()).toBe(404);
  const cross = await request.post("/api/proxy/approvals/apv-1/decision", {
    headers: { origin: "https://evil.example", "content-type": "application/json" },
    data: { decision: "approve" },
  });
  expect(cross.status()).toBe(403);
});
