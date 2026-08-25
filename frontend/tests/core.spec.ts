import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

test("staff opens the real synthetic care note", async ({ page }) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()

  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(
    page.getByRole("heading", { name: "Clinical care notes" }),
  ).toBeVisible()
  await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .click()

  await expect(
    page.getByRole("heading", { name: "Alex Synthetic" }),
  ).toBeVisible()
  await expect(page.getByText("Synthetic data").first()).toBeVisible()
  await expect(page.getByRole("heading", { name: "Timeline" })).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "What matters now" }),
  ).toBeVisible()
})

test("patient view exposes only My Care navigation", async ({ page }) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Patient" }).click()

  await expect(page).toHaveURL(/\/my-care$/)
  await expect(
    page.getByRole("heading", { name: /My Care · Alex Synthetic/ }),
  ).toBeVisible()
  await expect(page.getByRole("link", { name: "My care" })).toBeVisible()
  await expect(page.getByRole("link", { name: "Care notes" })).toHaveCount(0)
  await expect(page.getByText("Internal only")).toHaveCount(0)
})
