// e2e/smoke.spec.ts (in the Next.js app root) — minimal smoke; QA agent extends this per feature.
import { test, expect } from '@playwright/test';

test('landing page loads', async ({ page }) => {
  const resp = await page.goto('/');
  expect(resp?.ok()).toBeTruthy();
  await expect(page).toHaveTitle(/.+/);
});

test('no console errors on load', async ({ page }) => {
  const errors: string[] = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto('/');
  expect(errors, errors.join('\n')).toHaveLength(0);
});
