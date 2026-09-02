import { defineConfig, devices } from "@playwright/test"
import "dotenv/config"

const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "https://localhost"

export default defineConfig({
  testDir: "./tests",
  // Scenario A-F intentionally share one deterministic synthetic fixture.
  // Serial execution makes stale-version and decay assertions reproducible.
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  // Safety acceptance failures must remain visible; retries can hide a race
  // that only fails on the first browser attempt.
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? "blob" : "list",
  use: {
    baseURL,
    ignoreHTTPSErrors: true,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(process.env.PLAYWRIGHT_CHANNEL
          ? { channel: process.env.PLAYWRIGHT_CHANNEL }
          : {}),
      },
    },
  ],
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: '"$npm_execpath" run dev',
        url: baseURL,
        reuseExistingServer: !process.env.CI,
      },
})
