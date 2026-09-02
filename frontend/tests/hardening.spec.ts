import type { Browser, BrowserContext, Page } from "@playwright/test"
import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })
test.describe.configure({ mode: "serial" })

type ApiResult<T> = {
  body: T
  etag: string | null
  status: number
}

type Patient = { display_name: string; id: string }
type Entry = {
  id: string
  patient_id: string
  title: string
  content: string
  version_id: string
}
type SharingRequest = { id: string }
type Publication = {
  id: string
  entry_id: string
  entry_version_id: string
  replacement_publication_id?: string | null
}

async function api<T>(
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
      return {
        body: (await response.json().catch(() => null)) as T,
        etag: response.headers.get("etag"),
        status: response.status,
      }
    },
    { init, path },
  )
}

async function signIn(page: Page, persona: "staff" | "clinician" | "patient") {
  const response = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona },
  })
  expect(response.ok(), await response.text()).toBe(true)
  await page.goto(persona === "patient" ? "/patient/my-care" : "/patients")
}

async function alex(page: Page): Promise<Patient> {
  const result = await api<{ data: Patient[] }>(
    page,
    "/api/v1/patients/search",
    {
      body: { limit: 100, offset: 0, search: "Alex Tan", visit_scope: "all" },
      method: "POST",
    },
  )
  expect(result.status).toBe(200)
  const patient = result.body.data.find(
    (candidate) => candidate.display_name === "Alex Tan",
  )
  expect(patient).toBeDefined()
  return patient!
}

async function openAlex(page: Page) {
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()
  await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
}

function configuredBaseUrl(page: Page): string {
  return new URL(page.url()).origin
}

async function newContext(
  browser: Browser,
  baseURL: string,
): Promise<BrowserContext> {
  return browser.newContext({ baseURL, ignoreHTTPSErrors: true })
}

test("two editors must load latest and explicitly reconcile before saving", async ({
  browser,
  page,
}) => {
  test.setTimeout(120_000)
  await signIn(page, "staff")
  const patient = await alex(page)
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 10_000)}`
  const title = `Concurrent reconciliation ${suffix}`
  const created = await api<Entry>(page, "/api/v1/entries", {
    body: {
      content: "Original two-editor wording",
      origin: "human",
      patient_facing: false,
      patient_id: patient.id,
      section: "staff",
      title,
    },
    method: "POST",
  })
  expect(created.status).toBe(201)

  const secondContext = await newContext(browser, configuredBaseUrl(page))
  const second = await secondContext.newPage()
  try {
    await signIn(second, "staff")
    await Promise.all([openAlex(page), openAlex(second)])

    const localCard = page.locator(
      `article[aria-label="Care staff note: ${title}"]`,
    )
    const remoteCard = second.locator(
      `article[aria-label="Care staff note: ${title}"]`,
    )
    await localCard.getByRole("button", { name: "Edit" }).click()
    const localEditor = page.getByRole("dialog", { name: "Edit note" })
    await localEditor
      .getByLabel("Care note content")
      .fill("My local draft must survive the conflict")

    await remoteCard.getByRole("button", { name: "Edit" }).click()
    const remoteEditor = second.getByRole("dialog", { name: "Edit note" })
    await remoteEditor
      .getByLabel("Care note content")
      .fill("Latest wording saved by the second editor")
    await remoteEditor.getByRole("button", { name: "Save changes" }).click()
    await expect(remoteEditor).not.toBeVisible()
    await expect(remoteCard).toContainText(
      "Latest wording saved by the second editor",
    )

    const conflict = page.getByRole("dialog", { name: "Version conflict" })
    await expect(conflict)
      .toBeVisible({ timeout: 2_500 })
      .catch(async () => {
        await localEditor.getByRole("button", { name: "Save changes" }).click()
      })
    await expect(conflict).toBeVisible()
    await conflict
      .getByRole("button", { name: "Load latest and reconcile my draft" })
      .click()
    await expect(
      conflict.getByText("Latest saved note", { exact: true }),
    ).toBeVisible()
    await expect(
      conflict.getByText("Latest wording saved by the second editor"),
    ).toBeVisible()
    await expect(
      conflict
        .locator("pre")
        .filter({ hasText: "My local draft must survive the conflict" }),
    ).toBeVisible()
    await conflict
      .getByLabel("Reconciled care note")
      .fill("Explicitly reconciled two-editor wording")
    await conflict
      .getByRole("button", { name: "Continue with reconciled draft" })
      .click()
    const reconciliationBase = await api<Entry>(
      page,
      `/api/v1/entries/${created.body.id}`,
    )
    expect(reconciliationBase.status).toBe(200)

    // Race again after the first "Load latest" but before the reconciled
    // draft is saved. The server must return another conflict and the client
    // must retain the explicit merged wording rather than silently overwriting
    // either editor.
    await remoteCard.getByRole("button", { name: "Edit" }).click()
    await remoteEditor
      .getByLabel("Care note content")
      .fill("Third-race wording saved after reconciliation loaded")
    await remoteEditor.getByRole("button", { name: "Save changes" }).click()
    await expect(remoteEditor).not.toBeVisible()
    await expect(remoteCard).toContainText(
      "Third-race wording saved after reconciliation loaded",
    )

    const secondConflict = await api<{ code: string }>(
      page,
      `/api/v1/entries/${created.body.id}`,
      {
        body: {
          content: "Explicitly reconciled two-editor wording",
          patient_facing: false,
          title,
        },
        headers: {
          "If-Match": `"${reconciliationBase.body.version_id}"`,
        },
        method: "PATCH",
      },
    )
    expect(secondConflict.status).toBe(409)
    expect(secondConflict.body.code).toBe("VERSION_CONFLICT")
    await expect(conflict).toBeVisible()
    await conflict
      .getByRole("button", { name: "Load latest and reconcile my draft" })
      .click()
    await expect(
      conflict.getByText(
        "Third-race wording saved after reconciliation loaded",
      ),
    ).toBeVisible()
    await expect(
      conflict
        .locator("pre")
        .filter({ hasText: "Explicitly reconciled two-editor wording" }),
    ).toBeVisible()
    await conflict
      .getByLabel("Reconciled care note")
      .fill("Final explicit three-race reconciliation")
    await conflict
      .getByRole("button", { name: "Continue with reconciled draft" })
      .click()
    await localEditor.getByRole("button", { name: "Save changes" }).click()
    await expect(localEditor).not.toBeVisible()
    await expect(localCard).toContainText(
      "Final explicit three-race reconciliation",
    )
  } finally {
    await secondContext.close()
  }
})

test("publication correction clears two open patient views through SSE and polling fallback", async ({
  browser,
  page,
}) => {
  test.setTimeout(90_000)
  await signIn(page, "staff")
  const patient = await alex(page)
  const baseURL = configuredBaseUrl(page)
  const suffix = `${Date.now()}-${Math.floor(Math.random() * 10_000)}`
  const title = `Publication recall ${suffix}`
  const oldText = `Withdrawn patient summary ${suffix}`
  const replacementText = `Corrected patient summary ${suffix}`
  let replacementPublicationId: string | undefined

  const clinicianContext = await newContext(browser, baseURL)
  const livePatientContext = await newContext(browser, baseURL)
  const pollingPatientContext = await newContext(browser, baseURL)
  const clinician = await clinicianContext.newPage()
  const livePatient = await livePatientContext.newPage()
  const pollingPatient = await pollingPatientContext.newPage()
  await pollingPatient.route(
    "**/api/v1/patients/*/portal-events/stream",
    (route) => route.abort(),
  )

  try {
    const created = await api<Entry>(page, "/api/v1/entries", {
      body: {
        content: oldText,
        origin: "human",
        patient_facing: false,
        patient_id: patient.id,
        section: "staff",
        title,
      },
      method: "POST",
    })
    expect(created.status).toBe(201)
    const sharing = await api<SharingRequest>(
      page,
      `/api/v1/entries/${created.body.id}/patient-sharing-requests`,
      {
        body: { entry_version_id: created.body.version_id },
        method: "POST",
      },
    )
    expect(sharing.status).toBe(201)

    await signIn(clinician, "clinician")
    const publication = await api<Publication>(
      clinician,
      `/api/v1/patient-sharing-requests/${sharing.body.id}/approve`,
      { body: { medication_reviews: [] }, method: "POST" },
    )
    expect(publication.status).toBe(201)

    await Promise.all([
      signIn(livePatient, "patient"),
      signIn(pollingPatient, "patient"),
    ])
    await Promise.all([
      expect(livePatient.getByText(oldText, { exact: true })).toBeVisible(),
      expect(pollingPatient.getByText(oldText, { exact: true })).toBeVisible(),
    ])

    const replacement = await api<Entry>(
      page,
      `/api/v1/entries/${created.body.id}`,
      {
        body: {
          content: replacementText,
          patient_facing: true,
          title,
        },
        headers: { "If-Match": `"${publication.body.entry_version_id}"` },
        method: "PATCH",
      },
    )
    expect(replacement.status).toBe(200)
    const corrected = await api<Publication>(
      clinician,
      `/api/v1/patient-publications/${publication.body.id}/correct`,
      {
        body: {
          medication_reviews: [],
          outreach_required: true,
          replacement_entry_version_id: replacement.body.version_id,
        },
        headers: { "Idempotency-Key": `correction-${suffix}` },
        method: "POST",
      },
    )
    expect(corrected.status).toBe(201)
    replacementPublicationId = corrected.body.id

    await expect(livePatient.getByText(oldText, { exact: true })).toHaveCount(
      0,
      { timeout: 8_000 },
    )
    await expect(
      livePatient.getByText(replacementText, { exact: true }),
    ).toBeVisible({ timeout: 8_000 })

    // This page deliberately has no SSE connection. The independent 15-second
    // polling path must still recall the old publication from an already-open
    // browser and replace it without a manual refresh.
    await expect(
      pollingPatient.getByText(oldText, { exact: true }),
    ).toHaveCount(0, { timeout: 22_000 })
    await expect(
      pollingPatient.getByText(replacementText, { exact: true }),
    ).toBeVisible({ timeout: 22_000 })
  } finally {
    if (replacementPublicationId) {
      await api(
        clinician,
        `/api/v1/patient-publications/${replacementPublicationId}/withdraw`,
        { method: "POST" },
      ).catch(() => undefined)
    }
    await Promise.all([
      clinicianContext.close(),
      livePatientContext.close(),
      pollingPatientContext.close(),
    ])
  }
})
