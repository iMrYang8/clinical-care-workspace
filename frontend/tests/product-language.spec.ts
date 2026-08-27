import { expect, type Locator, type Page, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

type Persona = "admin" | "patient" | "staff"

const rawUuidPattern =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/i

const personaHome: Record<Persona, string> = {
  admin: "/admin",
  patient: "/patient/my-care",
  staff: "/patients",
}

const bannedProductTerms: Array<[label: string, pattern: RegExp]> = [
  ["72-hour build language", /\b(?:72h|72-hour)\b/i],
  ["candidate", /\bcandidate\b/i],
  ["brief", /\bbrief\b/i],
  ["demo", /\bdemo\b/i],
  ["fixture", /\bfixture\b/i],
  ["scenario", /\bscenario\b/i],
  ["bonus", /\bbonus\b/i],
  ["micro-test", /\bmicro-test\b/i],
  ["synthetic", /\bsynthetic\b/i],
  ["API", /\bAPI\b/],
  ["If-Match", /\bIf-Match\b/i],
  ["SHA-256", /\bSHA-?256\b/i],
  ["offset", /\boffset\b/i],
  ["RLS", /\bRLS\b/],
  ["provider", /\bprovider\b/i],
  ["model", /\bmodel\b/i],
  ["reason code", /\breason[ _-]code\b/i],
  ["raw UUID", rawUuidPattern],
]

async function signInAs(page: Page, persona: Persona): Promise<void> {
  const response = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona },
  })
  expect(response.ok(), await response.text()).toBe(true)
  await page.goto(personaHome[persona])
}

async function expectProductLanguage(
  surface: Locator,
  surfaceName: string,
  options: { allowAdminAISettings?: boolean } = {},
): Promise<void> {
  await expect(surface).toBeVisible()
  const text = await surface.innerText()
  const permittedLabels = options.allowAdminAISettings
    ? new Set(["API", "model"])
    : new Set<string>()
  const matches = bannedProductTerms
    .filter(([label]) => !permittedLabels.has(label))
    .filter(([, pattern]) => pattern.test(text))
    .map(([label]) => label)
  expect(
    matches,
    `${surfaceName} exposes internal delivery or implementation language:\n${text}`,
  ).toEqual([])
}

test("clinical sign-in is product-only and validates the human clinic code", async ({
  page,
}) => {
  let passwordLoginRequests = 0
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/auth/login"
    ) {
      passwordLoginRequests += 1
    }
  })

  await page.goto("/login")
  await expect(page.getByTestId("clinical-login-form")).toBeVisible()
  await expect(page.getByText("Clinical team access")).toBeVisible()
  await expect(page.getByRole("button", { name: /Continue as/i })).toHaveCount(
    0,
  )
  await expectProductLanguage(page.locator("body"), "clinical sign-in")

  const clinicCode = page.getByLabel("Clinic code")
  await clinicCode.fill("ntuhealth")
  await expect(clinicCode).toHaveValue("NTUHEALTH")

  await page.getByLabel("Email").fill("  USER@EXAMPLE.COM  ")
  await page
    .getByLabel("Password", { exact: true })
    .fill("a long password with spaces")
  await clinicCode.fill("NTU-01")
  await page.getByRole("button", { name: "Sign in to clinic" }).click()
  await expect(
    page.getByText(
      "Use 3–12 English letters. Your clinic code is shown in uppercase.",
    ),
  ).toBeVisible()
  expect(passwordLoginRequests).toBe(0)

  await clinicCode.fill("NtuHealth")
  await expect(clinicCode).toHaveValue("NTUHEALTH")
  await page.getByLabel("Email").blur()
  await expect(page.getByLabel("Email")).toHaveValue("user@example.com")
})

test("patient sign-in is a separate portal with reciprocal navigation", async ({
  page,
}) => {
  await page.goto("/patient/login")
  await expect(page.getByTestId("patient-login-form")).toBeVisible()
  await expect(page.getByText("Patient access")).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Clinical sign in" }),
  ).toHaveAttribute("href", "/login")
  await expect(page.getByRole("link", { name: "Administration" })).toHaveCount(
    0,
  )
  await expect(page.getByRole("link", { name: "Patients" })).toHaveCount(0)
  await expectProductLanguage(page.locator("body"), "patient sign-in")
})

test("invitation activation validates matching passwords before any request", async ({
  page,
}) => {
  let acceptanceRequests = 0
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/auth/invitations/accept"
    ) {
      acceptanceRequests += 1
    }
  })

  await page.goto(`/accept-invitation#${"t".repeat(64)}`)
  await page.getByLabel("Invited email").fill("clinician@example.com")
  await page.getByLabel("Display name").fill("Invited Clinician")
  await page.getByLabel("New password").fill("a long first password")
  await page.getByLabel("Confirm password").fill("a different password")

  await expect(page.getByLabel("New password")).toHaveAttribute(
    "type",
    "password",
  )
  await expect(page.getByLabel("Confirm password")).toHaveAttribute(
    "type",
    "password",
  )
  await page.getByRole("button", { name: "Show passwords" }).click()
  await expect(page.getByLabel("New password")).toHaveAttribute("type", "text")
  await expect(page.getByLabel("Confirm password")).toHaveAttribute(
    "type",
    "text",
  )

  await page.getByRole("button", { name: "Activate account" }).click()
  await expect(page.getByText("Passwords do not match.")).toBeVisible()
  expect(acceptanceRequests).toBe(0)
})

test("care staff remain in the clinical workspace", async ({ page }) => {
  await signInAs(page, "staff")
  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(page.getByRole("link", { name: "Patients" })).toBeVisible()
  await expect(page.getByRole("link", { name: "Administration" })).toHaveCount(
    0,
  )
  await expect(page.getByText("Patient access")).toHaveCount(0)
  await expectProductLanguage(page.locator("#main-content"), "patient list")

  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()
  await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
  expect(page.url()).not.toMatch(rawUuidPattern)
  await expectProductLanguage(
    page.locator("#main-content"),
    "shared care record",
  )

  const aiPriority = page.getByRole("listitem").filter({
    hasText: "AI doctor draft requires clinician review",
  })
  await aiPriority.getByRole("button", { name: "View source" }).click()
  const sourceDialog = page.getByRole("dialog", { name: /Source details/ })
  await expectProductLanguage(sourceDialog, "Source details")
  await sourceDialog.getByRole("button", { name: "Close" }).click()

  const staffEntry = page.locator(
    'article[aria-label="Care staff note: Medication reconciliation"]',
  )
  await staffEntry.getByRole("button", { name: "Change history" }).click()
  const history = page.getByRole("dialog", { name: /Change history/ })
  const version1 = history.getByRole("listitem").filter({
    hasText: "Version 1 · Medication reconciliation",
  })
  const version2 = history.getByRole("listitem").filter({
    hasText: "Version 2 · Medication reconciliation",
  })
  await version1.getByRole("button", { name: "Compare from" }).click()
  await version2.getByRole("button", { name: "Compare to" }).click()
  await expect(
    history.getByRole("heading", { name: "Changes", exact: true }),
  ).toBeVisible()
  await expect(history.locator("pre")).toBeVisible()
  await expectProductLanguage(history, "Change history and comparison")
  await history.getByRole("button", { name: "Close" }).click()

  await page.getByRole("link", { name: "Record visit" }).click()
  await expect(
    page.getByRole("heading", { name: "Record visit" }),
  ).toBeVisible()
  expect(page.url()).not.toMatch(rawUuidPattern)
  await expectProductLanguage(
    page.locator("#main-content"),
    "record-visit page",
  )

  await page.goto("/patient/my-care")
  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(page.getByRole("heading", { name: "Patients" })).toBeVisible()
})

test("clinic administrators see administration but not patient navigation", async ({
  page,
}) => {
  let invitationRequests = 0
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/api/v1/admin/memberships"
    ) {
      invitationRequests += 1
    }
  })
  await signInAs(page, "admin")
  await expect(page.getByRole("link", { name: "Patients" })).toBeVisible()
  await expect(page.getByRole("link", { name: "Administration" })).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Patient access", exact: true }),
  ).toHaveCount(0)
  await expectProductLanguage(page.locator("#main-content"), "administration", {
    // Clinic administrators explicitly configure these technical settings;
    // the same words remain forbidden on patient and care-team surfaces.
    allowAdminAISettings: true,
  })

  await page.getByRole("button", { name: "New invitation" }).click()
  const inviteEmail = page.getByLabel("Email")
  await inviteEmail.fill("not-an-email")
  await page.getByRole("button", { name: "Send verified invitation" }).click()
  expect(
    await inviteEmail.evaluate(
      (element: HTMLInputElement) => element.validity.typeMismatch,
    ),
  ).toBe(true)
  expect(invitationRequests).toBe(0)
})

test("patients see My Care without the clinical workspace", async ({
  page,
}) => {
  await signInAs(page, "patient")
  await expect(page).toHaveURL(/\/patient\/my-care\/?$/)
  await expect(
    page.getByRole("heading", { name: /My Care · Alex Tan/ }),
  ).toBeVisible()
  await expect(page.getByText("Patient access")).toBeVisible()
  await expect(page.getByText("Clinical care workspace")).toHaveCount(0)
  await expect(page.getByRole("link", { name: "Patients" })).toHaveCount(0)
  await expect(page.getByRole("link", { name: "Administration" })).toHaveCount(
    0,
  )
  await expectProductLanguage(page.locator("#main-content"), "My Care")

  await page.goto("/patients")
  await expect(page).toHaveURL(/\/patient\/my-care\/?$/)
})

test("the legacy My Care address redirects to the isolated patient entry", async ({
  page,
}) => {
  await page.goto("/my-care")
  await expect(page).toHaveURL(/\/patient\/login\/?$/)

  await signInAs(page, "patient")
  await page.goto("/my-care")
  await expect(page).toHaveURL(/\/patient\/my-care\/?$/)
})
