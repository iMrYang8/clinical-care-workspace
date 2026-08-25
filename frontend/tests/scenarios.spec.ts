import { expect, type Page, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: "serial" })

type ApiResult<T = Record<string, unknown>> = {
  body: T
  etag: string | null
  status: number
}

type Patient = { display_name: string; id: string }
type TimelineEntry = {
  content: string
  entry_type: string
  id: string
  patient_id: string
  section: string
  title: string
  version_id: string
  version_no: number
}

async function login(
  page: Page,
  role: "Care staff" | "Clinician" | "Patient" | "Clinic admin",
) {
  await page.goto("/login")
  await page.getByRole("button", { name: `Continue as ${role}` }).click()
  const destination =
    role === "Patient"
      ? /\/my-care$/
      : role === "Clinic admin"
        ? /\/admin$/
        : /\/patients\/?$/
  await expect(page).toHaveURL(destination)
}

async function openAlex(page: Page) {
  await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .click()
  await expect(
    page.getByRole("heading", { name: "Alex Synthetic" }),
  ).toBeVisible()
}

async function api<T = Record<string, unknown>>(
  page: Page,
  path: string,
  init: {
    body?: unknown
    headers?: Record<string, string>
    method?: string
  } = {},
): Promise<ApiResult<T>> {
  return page.evaluate(
    async ({ init: requestInit, path: requestPath }) => {
      const response = await fetch(requestPath, {
        body:
          requestInit.body === undefined
            ? undefined
            : JSON.stringify(requestInit.body),
        credentials: "same-origin",
        headers: {
          ...(requestInit.body === undefined
            ? {}
            : { "Content-Type": "application/json" }),
          ...requestInit.headers,
        },
        method: requestInit.method ?? "GET",
      })
      const body = await response.json().catch(() => null)
      return {
        body,
        etag: response.headers.get("etag"),
        status: response.status,
      }
    },
    { init, path },
  )
}

async function patientAndTimeline(page: Page) {
  const patients = await api<{ data: Patient[] }>(page, "/api/v1/patients")
  expect(patients.status).toBe(200)
  const patient = patients.body.data.find(
    (candidate) => candidate.display_name === "Alex Synthetic",
  )
  expect(patient).toBeDefined()
  const timeline = await api<{ data: TimelineEntry[] }>(
    page,
    `/api/v1/patients/${patient?.id}/timeline`,
  )
  expect(timeline.status).toBe(200)
  return { patient: patient as Patient, timeline: timeline.body.data }
}

function collectKeys(value: unknown, keys = new Set<string>()): Set<string> {
  if (Array.isArray(value)) {
    for (const item of value) collectKeys(item, keys)
  } else if (value && typeof value === "object") {
    for (const [key, item] of Object.entries(value)) {
      keys.add(key.toLowerCase())
      collectKeys(item, keys)
    }
  }
  return keys
}

test("[Scenario A] Glance opens the exact immutable timeline span", async ({
  page,
}) => {
  await login(page, "Care staff")
  await openAlex(page)

  await expect(
    page.getByText("AI Doctor Consult", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("AI Nurse Consult", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("AI Patient Session", { exact: true }),
  ).toBeVisible()

  const card = page.getByRole("listitem").filter({
    hasText: "Fall risk remains elevated",
  })
  await card.getByRole("button", { name: "View source" }).click()

  const dialog = page.getByRole("dialog", { name: /Immutable source/ })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator("mark[data-source-span]")).toHaveText(
    "Fall risk remains elevated",
  )
  await expect(
    page.locator(
      'article[aria-label="Manual Clinician: Current care review"][data-entry-version-id]',
    ),
  ).toHaveCount(1)
})

test("[Scenario B] collaboration, immutable diff/revert, audit and learning are demonstrable", async ({
  page,
}) => {
  await login(page, "Care staff")
  await openAlex(page)

  const clinicianEntry = page.getByRole("article", {
    name: "Manual Clinician: Current care review",
  })
  await clinicianEntry.getByRole("button", { name: "Comments" }).click()
  await expect(
    page.getByText("@clinician Please review this synthetic fall-risk item."),
  ).toBeVisible()
  await expect(page.getByText(/^Assigned /)).toBeVisible()

  const staffEntry = page
    .locator('article[aria-label="Manual Staff: Medication reconciliation"]')
    .filter({ hasText: "Medication list reviewed" })
  await staffEntry.getByRole("button", { name: "Versions" }).click()
  const drawer = page.getByRole("dialog", { name: /Version history/ })
  const version1 = drawer.getByRole("listitem").filter({
    hasText: "Version 1 · Medication reconciliation",
  })
  const version2 = drawer.getByRole("listitem").filter({
    hasText: "Version 2 · Medication reconciliation",
  })
  await version1.getByRole("button", { name: "Diff from" }).click()
  await version2.getByRole("button", { name: "Diff to" }).click()
  await expect(drawer.getByText("Unified diff")).toBeVisible()
  await expect(drawer.locator("pre")).toContainText("duplicate evening dose")
  await version1.getByRole("button", { name: "Revert as new version" }).click()
  await expect(drawer).toBeHidden()
  await expect(staffEntry).toContainText("Medication list reviewed during")

  await expect(
    page
      .getByRole("listitem")
      .filter({ hasText: "Medication reconciliation completed" })
      .getByText("Clinician accepted"),
  ).toBeVisible()

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "Log out and clear data" }).click()
  await expect(page).toHaveURL(/\/login$/)
  await login(page, "Clinic admin")
  await expect(
    page.getByRole("heading", { name: "Clinic administration" }),
  ).toBeVisible()
  await expect(
    page.getByText("Metadata-only audit trail", { exact: true }),
  ).toBeVisible()
  await expect(page.getByText("entry.reverted").first()).toBeVisible()
  await expect(page.getByText("Medication list reviewed during")).toHaveCount(0)
})

test("[Scenario C] cross-date timeline survives archive and checksum-verified rehydration", async ({
  page,
}) => {
  await login(page, "Clinician")
  await openAlex(page)
  await expect(page.locator('time[datetime^="2025-04-15"]')).toBeVisible()
  await expect(
    page.locator('time[datetime^="2026-02-06"]').first(),
  ).toBeVisible()

  const preview = await api<{
    candidates: Array<{
      eligible_for_cold: boolean
      entry_version_id: string
    }>
  }>(page, "/api/v1/decay/preview")
  expect(preview.status).toBe(200)
  const candidate = preview.body.candidates.find(
    (item) => item.eligible_for_cold,
  )
  expect(candidate).toBeDefined()

  const archived = await api<{ archived_count: number }>(
    page,
    "/api/v1/decay/archive",
    {
      body: {
        dry_run: false,
        entry_version_ids: [candidate?.entry_version_id],
      },
      method: "POST",
    },
  )
  expect(archived).toMatchObject({ body: { archived_count: 1 }, status: 200 })

  const restored = await api<{
    content_sha256: string
    storage_tier: string
  }>(page, `/api/v1/decay/entries/${candidate?.entry_version_id}/rehydrate`, {
    method: "POST",
  })
  expect(restored.status).toBe(200)
  expect(restored.body.storage_tier).toBe("warm")
  expect(restored.body.content_sha256).toMatch(/^[a-f0-9]{64}$/)
})

test("[Scenario D] stale ETag conflicts while independent entries and tenant boundaries hold", async ({
  browser,
}) => {
  const firstContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const secondContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const first = await firstContext.newPage()
  const second = await secondContext.newPage()
  await login(first, "Care staff")
  await login(second, "Care staff")

  const firstData = await patientAndTimeline(first)
  const original = firstData.timeline.find(
    (entry) =>
      entry.entry_type === "manual_staff_note" &&
      entry.title === "Medication reconciliation" &&
      entry.content.includes("Medication list reviewed"),
  )
  expect(original).toBeDefined()
  const firstRead = await api<TimelineEntry>(
    first,
    `/api/v1/entries/${original?.id}`,
  )
  const secondRead = await api<TimelineEntry>(
    second,
    `/api/v1/entries/${original?.id}`,
  )
  expect(firstRead.body.version_id).toBe(secondRead.body.version_id)

  const winner = await api<TimelineEntry>(
    first,
    `/api/v1/entries/${original?.id}`,
    {
      body: { content: `${firstRead.body.content} First browser update.` },
      headers: { "If-Match": `"${firstRead.body.version_id}"` },
      method: "PATCH",
    },
  )
  expect(winner.status).toBe(200)
  const stale = await api<{ code: string }>(
    second,
    `/api/v1/entries/${original?.id}`,
    {
      body: { content: `${secondRead.body.content} Stale browser update.` },
      headers: { "If-Match": `"${secondRead.body.version_id}"` },
      method: "PATCH",
    },
  )
  expect(stale).toMatchObject({
    body: { code: "VERSION_CONFLICT" },
    status: 409,
  })

  const created = await api<TimelineEntry>(first, "/api/v1/entries", {
    body: {
      content: "Independent synthetic entry for concurrent delivery proof.",
      patient_facing: false,
      patient_id: firstData.patient.id,
      section: "staff",
      title: "Independent concurrency proof",
    },
    method: "POST",
  })
  expect(created.status).toBe(201)
  const [updatedOriginal, updatedIndependent] = await Promise.all([
    api(first, `/api/v1/entries/${original?.id}`, {
      body: { title: "Medication reconciliation" },
      headers: { "If-Match": `"${winner.body.version_id}"` },
      method: "PATCH",
    }),
    api(second, `/api/v1/entries/${created.body.id}`, {
      body: { title: "Independent concurrency proof updated" },
      headers: { "If-Match": `"${created.body.version_id}"` },
      method: "PATCH",
    }),
  ])
  expect([updatedOriginal.status, updatedIndependent.status]).toEqual([
    200, 200,
  ])

  const adminContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const admin = await adminContext.newPage()
  await login(admin, "Clinic admin")
  expect((await api(admin, `/api/v1/entries/${original?.id}`)).status).toBe(403)

  const otherContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const other = await otherContext.newPage()
  await other.goto("/login")
  expect(
    (
      await api(other, "/api/v1/auth/demo-login", {
        body: { persona: "other_staff" },
        method: "POST",
      })
    ).status,
  ).toBe(200)
  expect((await api(other, `/api/v1/entries/${original?.id}`)).status).toBe(404)

  await Promise.all([
    firstContext.close(),
    secondContext.close(),
    adminContext.close(),
    otherContext.close(),
  ])
})

test("[Scenario E] patient network is narrow, cookie-only, and provider-off is explicit", async ({
  page,
}) => {
  const payloads: unknown[] = []
  const requestUrls: string[] = []
  const authorizationHeaders: Array<string | undefined> = []
  const pending: Promise<void>[] = []
  page.on("request", (request) => {
    if (!request.url().includes("/api/v1/")) return
    requestUrls.push(request.url())
    authorizationHeaders.push(request.headers().authorization)
  })
  page.on("response", (response) => {
    if (!response.url().includes("/api/v1/")) return
    pending.push(
      response
        .json()
        .then((value) => {
          payloads.push(value)
        })
        .catch(() => undefined),
    )
  })

  await login(page, "Patient")
  await expect(
    page.getByRole("heading", { name: /My Care · Alex Synthetic/ }),
  ).toBeVisible()
  const patientData = await patientAndTimeline(page)
  const created = await api<{ id: string }>(page, "/api/v1/voice/sessions", {
    body: {
      capture_kind: "patient",
      patient_id: patientData.patient.id,
      synthetic_fixture: false,
    },
    method: "POST",
  })
  expect(created.status).toBe(201)
  const live = await api<{ available: boolean; reason_code: string }>(
    page,
    `/api/v1/voice/sessions/${created.body.id}/live`,
  )
  expect(live).toMatchObject({
    body: {
      available: false,
      reason_code: "LIVE_TRANSCRIPT_NOT_CONFIGURED",
    },
    status: 200,
  })
  await Promise.all(pending)

  const keys = collectKeys(payloads)
  for (const forbidden of [
    "author_id",
    "comments",
    "critical",
    "final_score",
    "learned_score",
    "raw_ai",
    "risk_reason",
    "score_components",
  ]) {
    expect(keys, `patient response leaked ${forbidden}`).not.toContain(
      forbidden,
    )
  }
  expect(
    requestUrls.some((url) =>
      /\/(admin|ai|comments|decay|jobs)(\/|\?|$)/.test(url),
    ),
  ).toBe(false)
  expect(authorizationHeaders.every((header) => header === undefined)).toBe(
    true,
  )

  const cookies = await page.context().cookies()
  const authCookie = cookies.find(
    (cookie) => cookie.name === "nightingale_session",
  )
  expect(authCookie).toMatchObject({
    httpOnly: true,
    sameSite: "Lax",
    secure: true,
  })
  const storageKeys = await page.evaluate(() => Object.keys(localStorage))
  expect(
    storageKeys.some((key) => /(access|auth|bearer|token)/i.test(key)),
  ).toBe(false)

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "Log out and clear data" }).click()
  await expect(page).toHaveURL(/\/login$/)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(false)
})
