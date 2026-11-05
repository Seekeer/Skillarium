import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'tests',
  // tests/unit runs under node --test.
  testMatch: '*.spec.mjs',
  timeout: 300_000,
  // Every action runs projects.ps1 or projects.sh, which takes seconds on Windows.
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: 'list',
});
