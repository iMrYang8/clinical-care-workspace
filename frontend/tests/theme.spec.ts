import { expect, type Page, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

const THEME_STORAGE_KEY = "nightingale-ui-theme"

type Theme = "dark" | "light" | "system"
type Persona = "admin" | "clinician" | "patient" | "staff"

const personaHome: Record<Persona, string> = {
  admin: "/admin",
  clinician: "/patients",
  patient: "/patient/my-care",
  staff: "/patients",
}

async function signInAs(page: Page, persona: Persona): Promise<void> {
  const response = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona },
  })
  expect(response.ok(), await response.text()).toBe(true)
  await page.goto(personaHome[persona])
}

async function chooseTheme(page: Page, theme: Theme): Promise<void> {
  await expect(page.getByRole("menu")).toHaveCount(0)
  await page.getByTestId("theme-button").click()
  await page.locator(`[data-testid="${theme}-mode"]:visible`).click()
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme)
  await expect(page.getByRole("menu")).toHaveCount(0)
}

async function expectResolvedTheme(
  page: Page,
  selected: Theme,
  resolved: Exclude<Theme, "system">,
  options: { expectStoredSelection?: boolean } = {},
): Promise<void> {
  const { expectStoredSelection = true } = options
  const root = page.locator("html")
  await expect(root).toHaveAttribute("data-theme", selected)
  await expect(root).toHaveClass(new RegExp(`(?:^|\\s)${resolved}(?:\\s|$)`))
  await expect(root).toHaveCSS("color-scheme", resolved)
  if (expectStoredSelection) {
    expect(
      await page.evaluate(
        (key) => window.localStorage.getItem(key),
        THEME_STORAGE_KEY,
      ),
    ).toBe(selected)
  }
}

test("the synchronous theme bootstrap runs before the application module", async ({
  page,
}) => {
  const response = await page.request.get("/login", {
    headers: { Accept: "text/html" },
  })
  const html = await response.text()
  expect(response.ok(), html).toBe(true)
  const bootstrapIndex = html.indexOf(THEME_STORAGE_KEY)
  const bootstrapTagStart = html.lastIndexOf("<script", bootstrapIndex)
  const bootstrapTagEnd = html.indexOf(">", bootstrapTagStart)
  const bootstrapOpeningTag = html.slice(bootstrapTagStart, bootstrapTagEnd + 1)
  const applicationModule = Array.from(
    html.matchAll(/<script\b[^>]*\btype=["']module["'][^>]*>/gi),
  ).find((match) => !match[0].includes("/@vite/client"))

  expect(bootstrapIndex).toBeGreaterThan(-1)
  expect(bootstrapTagStart).toBeGreaterThan(-1)
  expect(bootstrapOpeningTag).not.toMatch(/\b(?:async|defer|src)\b/i)
  expect(applicationModule?.index).toBeDefined()
  expect(bootstrapIndex).toBeLessThan(applicationModule?.index ?? -1)

  await page.emulateMedia({ colorScheme: "light" })
  await page.addInitScript((key) => {
    window.localStorage.setItem(key, "dark")
    window.addEventListener(
      "DOMContentLoaded",
      () => {
        ;(
          window as typeof window & {
            __nightingaleThemeAtDOMContentLoaded?: {
              className: string
              colorScheme: string
              selected: string | undefined
            }
          }
        ).__nightingaleThemeAtDOMContentLoaded = {
          className: document.documentElement.className,
          colorScheme: document.documentElement.style.colorScheme,
          selected: document.documentElement.dataset.theme,
        }
      },
      { once: true },
    )
  }, THEME_STORAGE_KEY)
  await page.goto("/login", { waitUntil: "domcontentloaded" })
  const firstTheme = await page.evaluate(
    () =>
      (
        window as typeof window & {
          __nightingaleThemeAtDOMContentLoaded?: {
            className: string
            colorScheme: string
            selected: string | undefined
          }
        }
      ).__nightingaleThemeAtDOMContentLoaded,
  )
  expect(firstTheme).toMatchObject({
    colorScheme: "dark",
    selected: "dark",
  })
  expect(firstTheme?.className.split(/\s+/)).toContain("dark")
})

test("appearance choices persist across refresh", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" })
  await page.goto("/login")
  await expectResolvedTheme(page, "system", "light", {
    expectStoredSelection: false,
  })

  await chooseTheme(page, "dark")
  await expectResolvedTheme(page, "dark", "dark")
  await page.reload({ waitUntil: "domcontentloaded" })
  await expectResolvedTheme(page, "dark", "dark")

  await chooseTheme(page, "light")
  await expectResolvedTheme(page, "light", "light")
  await page.reload({ waitUntil: "domcontentloaded" })
  await expectResolvedTheme(page, "light", "light")
})

test("system appearance follows the browser and rejects an invalid stored value", async ({
  page,
}) => {
  await page.addInitScript((key) => {
    window.localStorage.setItem(key, "sepia")
  }, THEME_STORAGE_KEY)
  await page.emulateMedia({ colorScheme: "dark" })
  await page.goto("/login")

  await expectResolvedTheme(page, "system", "dark", {
    expectStoredSelection: false,
  })
  await page.emulateMedia({ colorScheme: "light" })
  await expectResolvedTheme(page, "system", "light", {
    expectStoredSelection: false,
  })
})

test("appearance changes synchronize across open tabs", async ({ page }) => {
  const second = await page.context().newPage()
  await page.goto("/login")
  await second.goto("/login")

  await chooseTheme(page, "dark")
  await expectResolvedTheme(page, "dark", "dark")
  await expectResolvedTheme(second, "dark", "dark")

  await chooseTheme(second, "light")
  await expectResolvedTheme(second, "light", "light")
  await expectResolvedTheme(page, "light", "light")
})

test("clinical patient-list, care-note, and recording pages share light, dark, and system appearance", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "dark" })
  await signInAs(page, "clinician")

  await expect(page.getByRole("heading", { name: "Patients" })).toBeVisible()
  const careRecordHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(careRecordHref).toBeTruthy()

  for (const selected of ["light", "dark", "system"] as const) {
    const resolved = selected === "light" ? "light" : "dark"
    await chooseTheme(page, selected)
    await expectResolvedTheme(page, selected, resolved)

    await page.goto(careRecordHref!)
    await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
    await expectResolvedTheme(page, selected, resolved)

    await page.goto(`${careRecordHref}/voice/capture`)
    await expect(
      page.getByRole("heading", { name: "Record visit" }),
    ).toBeVisible()
    await expectResolvedTheme(page, selected, resolved)

    await page.goto("/patients")
    await expect(page.getByRole("heading", { name: "Patients" })).toBeVisible()
  }
})

test("administration and the patient portal honor light, dark, and system appearance", async ({
  page,
}) => {
  await page.emulateMedia({ colorScheme: "dark" })
  await signInAs(page, "admin")
  await expect(
    page.getByRole("heading", { name: "Clinic administration" }),
  ).toBeVisible()
  for (const selected of ["light", "dark", "system"] as const) {
    const resolved = selected === "light" ? "light" : "dark"
    await chooseTheme(page, selected)
    await expectResolvedTheme(page, selected, resolved)
  }

  await page.request.post("/api/v1/auth/logout")
  await page.context().clearCookies()
  await signInAs(page, "patient")
  await expect(
    page.getByRole("heading", { name: /My Care · Alex Tan/ }),
  ).toBeVisible()
  for (const selected of ["light", "dark", "system"] as const) {
    const resolved = selected === "light" ? "light" : "dark"
    await chooseTheme(page, selected)
    await expectResolvedTheme(page, selected, resolved)
  }
})
