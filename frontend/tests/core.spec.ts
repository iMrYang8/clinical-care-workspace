import { randomUUID } from "node:crypto"
import type { Page } from "@playwright/test"
import { expect, test } from "@playwright/test"
import { resolvePatientRouteReference } from "../src/features/routeReferences"

test.use({ storageState: { cookies: [], origins: [] } })

type TestPersona = "patient" | "staff" | "clinician" | "admin"

const personaHome: Record<TestPersona, string> = {
  patient: "/patient/my-care",
  staff: "/patients",
  clinician: "/patients",
  admin: "/admin",
}

const rawUuidPattern =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i

function patientIdFromHref(href: string): string {
  const routeReference = new URL(href, "https://proxy").pathname.split("/")[2]
  const patient = resolvePatientRouteReference(routeReference)
  expect(
    patient,
    `Expected a public patient reference in ${href}`,
  ).not.toBeNull()
  return patient!.id
}

async function signInAs(page: Page, persona: TestPersona): Promise<void> {
  const response = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona },
  })
  expect(response.ok(), await response.text()).toBe(true)
  await page.goto(personaHome[persona])
}

async function seedQueuedVoiceChunk(
  page: Page,
  input: {
    captureId: string
    deviceId: string
    patientId: string
    includeChunk?: boolean
  },
): Promise<void> {
  const owner = await page.evaluate(async () => {
    const response = await fetch("/api/v1/auth/me")
    if (!response.ok)
      throw new Error("Authenticated capture owner is unavailable")
    return (await response.json()) as {
      user_id: string
      membership_id: string
      clinic_id: string
    }
  })
  await page.evaluate(
    async (fixture) => {
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open("nightingale-voice-v1", 2)
        request.onupgradeneeded = () => {
          const opened = request.result
          if (!opened.objectStoreNames.contains("captures")) {
            opened.createObjectStore("captures", { keyPath: "id" })
          }
          if (!opened.objectStoreNames.contains("chunks")) {
            const chunks = opened.createObjectStore("chunks", { keyPath: "id" })
            chunks.createIndex("by-capture", "captureId")
            chunks.createIndex("by-capture-index", ["captureId", "chunkIndex"])
          }
        }
        request.onerror = () => reject(request.error)
        request.onsuccess = () => resolve(request.result)
      })
      const key = await crypto.subtle.generateKey(
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"],
      )
      const iv = crypto.getRandomValues(new Uint8Array(12))
      const plaintext = new TextEncoder().encode("auth-fetch-voice-fixture")
      const ciphertext = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        key,
        plaintext,
      )
      const transaction = db.transaction(["captures", "chunks"], "readwrite")
      transaction.objectStore("captures").put({
        id: fixture.captureId,
        serverSessionId: fixture.captureId,
        serverDeviceId: fixture.deviceId,
        patientId: fixture.patientId,
        userId: fixture.owner.user_id,
        membershipId: fixture.owner.membership_id,
        clinicId: fixture.owner.clinic_id,
        mediaType: "audio/webm",
        key,
        nextChunkIndex: 1,
        createdAt: new Date().toISOString(),
      })
      if (fixture.includeChunk !== false) {
        transaction.objectStore("chunks").put({
          id: `${fixture.captureId}:0`,
          captureId: fixture.captureId,
          chunkIndex: 0,
          iv,
          ciphertext,
          sha256: "fixture",
          byteLength: plaintext.byteLength,
          mediaType: "audio/webm",
          startMs: 0,
          endMs: 1_000,
          createdAt: new Date().toISOString(),
        })
      }
      await new Promise<void>((resolve, reject) => {
        transaction.oncomplete = () => resolve()
        transaction.onerror = () => reject(transaction.error)
        transaction.onabort = () => reject(transaction.error)
      })
      db.close()
    },
    { ...input, owner },
  )
}

test("care staff open the shared patient record", async ({ page }) => {
  await signInAs(page, "staff")

  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(page.getByRole("heading", { name: "Patients" })).toBeVisible()
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()

  await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
  expect(page.url()).not.toMatch(rawUuidPattern)
  await expect(
    page.getByRole("heading", {
      name: "Longitudinal timeline",
      exact: true,
    }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Current priorities" }),
  ).toBeVisible({ timeout: 10_000 })
})

test("admin has clinic-scoped read-only care-note oversight", async ({
  page,
}) => {
  await signInAs(page, "admin")

  await expect(page).toHaveURL(/\/admin$/)
  await page.getByRole("link", { name: "Patients" }).click()
  await expect(page).toHaveURL(/\/patients\/?$/)
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()

  await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
  await expect(
    page.getByText("Clinic administrator · read-only oversight"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: /Add admin entry/ }),
  ).toHaveCount(0)
  await expect(page.getByRole("link", { name: "Record visit" })).toHaveCount(0)
  await expect(page.getByRole("button", { name: "Edit" })).toHaveCount(0)

  const denied = await page.evaluate(async () => {
    const patients = await fetch("/api/v1/patients").then((response) =>
      response.json(),
    )
    const response = await fetch("/api/v1/entries", {
      body: JSON.stringify({
        content: "Admin write must be rejected",
        patient_id: patients.data[0].id,
        section: "staff",
        title: "Rejected admin write",
      }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    })
    return response.status
  })
  expect(denied).toBe(403)
})

test("production-capable password form signs in with the secure cookie path", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByLabel("Clinic code").fill("nightingale")
  await expect(page.getByLabel("Clinic code")).toHaveValue("NIGHTINGALE")
  await page.getByLabel("Email").fill("  StAfF@NiGhTiNgAlE.ExAmPlE  ")
  await page.getByLabel("Password", { exact: true }).fill("synthetic-demo-only")
  await expect(page.getByLabel("Email")).toHaveValue(
    "staff@nightingale.example",
  )
  await page.getByRole("button", { name: "Sign in to clinic" }).click()

  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(page.getByRole("heading", { name: "Patients" })).toBeVisible()
  const sessionCookie = (await page.context().cookies()).find(
    (cookie) => cookie.name === "nightingale_session",
  )
  expect(sessionCookie).toMatchObject({
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
  })
  expect(
    await page.evaluate(() =>
      Object.entries(localStorage).some(([key, value]) =>
        /token|bearer|session/i.test(`${key}:${value}`),
      ),
    ),
  ).toBe(false)
})

test("patient view exposes only My Care navigation", async ({ page }) => {
  await signInAs(page, "patient")

  await expect(page).toHaveURL(/\/patient\/my-care\/?$/)
  await expect(
    page.getByRole("heading", { name: /My Care · Alex Tan/ }),
  ).toBeVisible()
  await expect(page.getByText("Patient access")).toBeVisible()
  await expect(page.getByRole("link", { name: "Patients" })).toHaveCount(0)
  await expect(page.getByText("Internal only")).toHaveCount(0)
})

test("failed network and CSRF logout stay masked until a confirmed retry", async ({
  page,
}) => {
  await signInAs(page, "staff")
  await expect(page).toHaveURL(/\/patients\/?$/)

  const second = await page.context().newPage()
  await second.goto("/patients")
  await second
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .click()
  await expect(second.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
  await expect(second.getByText("Current priorities")).toBeVisible()

  let attempts = 0
  let csrfStatus: number | undefined
  await page.route("**/api/v1/auth/logout", async (route) => {
    attempts += 1
    if (attempts === 1) {
      await route.abort("connectionfailed")
      return
    }
    if (attempts === 2) {
      // route.fetch sends a real request whose Origin Playwright may override;
      // Chromium itself forbids page JavaScript from forging this header.
      const response = await route.fetch({
        headers: {
          ...route.request().headers(),
          origin: "https://csrf.invalid",
        },
      })
      csrfStatus = response.status()
      await route.fulfill({ response })
      return
    }
    await route.continue()
  })

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "Sign out" }).click()

  const boundary = page.getByTestId("session-termination-boundary")
  await expect(
    boundary.getByRole("heading", { name: "Session termination incomplete" }),
  ).toBeVisible()
  await expect(boundary).toContainText("Sign-out could not be confirmed")
  await expect(boundary).toContainText("You are not logged out yet")
  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(page.getByRole("heading", { name: "Patients" })).toHaveCount(0)
  const secondBoundary = second.getByTestId("session-termination-boundary")
  await expect(
    secondBoundary.getByRole("heading", {
      name: "Session termination incomplete",
    }),
  ).toBeVisible()
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  await expect(second.getByText("Current priorities")).toHaveCount(0)
  await expect(second).toHaveURL(/\/patients\/.+/)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(true)
  expect(
    await page.evaluate(() =>
      localStorage.getItem("nightingale_session_termination_pending"),
    ),
  ).toBe("unconfirmed")

  await boundary.getByRole("button", { name: "Retry secure logout" }).click()
  await expect(
    boundary.getByRole("heading", { name: "Session termination incomplete" }),
  ).toBeVisible()
  await expect(boundary).toContainText(
    "Your account does not have access to this action.",
  )
  await expect(boundary).not.toContainText("CSRF origin rejected")
  expect(csrfStatus).toBe(403)
  await expect(secondBoundary).toContainText(
    "Your account does not have access to this action.",
  )
  await expect(secondBoundary).not.toContainText("CSRF origin rejected")
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(true)

  await boundary.getByRole("button", { name: "Retry secure logout" }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  expect(attempts).toBe(3)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(false)
  await expect
    .poll(() =>
      page.evaluate(() =>
        localStorage.getItem("nightingale_session_termination_pending"),
      ),
    )
    .toBeNull()
  await second.close()
})

test("confirmed logout masks a second tab and closes its held voice database", async ({
  page,
}) => {
  await signInAs(page, "staff")
  await expect(page).toHaveURL(/\/patients\/?$/)
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()

  const second = await page.context().newPage()
  await second.goto(`${patientHref}/voice/capture`)
  await expect(
    second.getByRole("heading", { name: "Record visit" }),
  ).toBeVisible()
  await expect
    .poll(() =>
      second.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(true)

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "Sign out" }).click()

  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  await expect(
    second.getByRole("heading", { name: "Record visit" }),
  ).toHaveCount(0)
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(false)
  await expect
    .poll(() =>
      second.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(false)
  await second.close()
})

test("a removed membership marker masks every tab and deletes its owned voice before another persona", async ({
  page,
}) => {
  await signInAs(page, "staff")
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = patientIdFromHref(patientHref!)

  const second = await page.context().newPage()
  await second.goto("/patients")
  const owner = await second.evaluate(async () => {
    const response = await fetch("/api/v1/auth/me")
    if (!response.ok)
      throw new Error("Authenticated capture owner is unavailable")
    return (await response.json()) as {
      user_id: string
      membership_id: string
      clinic_id: string
    }
  })
  await second.evaluate(
    async ({ fixturePatientId, owner }) => {
      const db = await new Promise<IDBDatabase>((resolve, reject) => {
        const request = indexedDB.open("nightingale-voice-v1", 2)
        request.onupgradeneeded = () => {
          const opened = request.result
          const captures = opened.createObjectStore("captures", {
            keyPath: "id",
          })
          void captures
          const chunks = opened.createObjectStore("chunks", { keyPath: "id" })
          chunks.createIndex("by-capture", "captureId")
          chunks.createIndex("by-capture-index", ["captureId", "chunkIndex"])
        }
        request.onerror = () => reject(request.error)
        request.onsuccess = () => resolve(request.result)
      })
      const key = await crypto.subtle.generateKey(
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"],
      )
      const iv = crypto.getRandomValues(new Uint8Array(12))
      const plaintext = new TextEncoder().encode("old-persona-voice-fixture")
      const ciphertext = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        key,
        plaintext,
      )
      const transaction = db.transaction(["captures", "chunks"], "readwrite")
      transaction.objectStore("captures").add({
        id: "expired-session-fixture",
        serverSessionId: "expired-session-fixture",
        serverDeviceId: "expired-device-fixture",
        patientId: fixturePatientId,
        userId: owner.user_id,
        membershipId: owner.membership_id,
        clinicId: owner.clinic_id,
        mediaType: "audio/webm",
        key,
        nextChunkIndex: 1,
        createdAt: new Date().toISOString(),
      })
      transaction.objectStore("chunks").add({
        id: "expired-session-fixture:0",
        captureId: "expired-session-fixture",
        chunkIndex: 0,
        iv,
        ciphertext,
        sha256: "fixture",
        byteLength: plaintext.byteLength,
        mediaType: "audio/webm",
        startMs: 0,
        endMs: 1_000,
        createdAt: new Date().toISOString(),
      })
      await new Promise<void>((resolve, reject) => {
        transaction.oncomplete = () => resolve()
        transaction.onerror = () => reject(transaction.error)
        transaction.onabort = () => reject(transaction.error)
      })
      db.close()
    },
    { fixturePatientId: patientId, owner },
  )
  await second.goto(`${patientHref}/voice/capture`)
  await expect(
    second.getByRole("heading", { name: "Recordings waiting to upload" }),
  ).toBeVisible()
  await expect(second.getByText("Interrupted recording")).toBeVisible()

  let releaseLogout!: () => void
  const logoutGate = new Promise<void>((resolve) => {
    releaseLogout = resolve
  })
  await page.route("**/api/v1/auth/logout", async (route) => {
    await logoutGate
    await route.continue()
  })
  let revokeOnce = true
  await page.route("**/api/v1/auth/me", async (route) => {
    if (revokeOnce) {
      revokeOnce = false
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        headers: { "X-Nightingale-Session-Invalid": "1" },
        body: JSON.stringify({ detail: "Invalid membership context" }),
      })
      return
    }
    await route.continue()
  })

  const reload = page.reload().catch(() => undefined)
  await expect(page.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  releaseLogout()
  await reload
  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  await expect
    .poll(() =>
      second.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(false)

  await second.close()
  await signInAs(page, "patient")
  await page.getByRole("link", { name: "Add a recording" }).click()
  await expect(
    page.getByRole("heading", { name: "Record visit" }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Recordings waiting to upload" }),
  ).toHaveCount(0)
})

test("a native voice chunk 401 terminates every tab and purges encrypted recovery", async ({
  page,
}) => {
  await signInAs(page, "staff")
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = patientIdFromHref(patientHref!)

  const second = await page.context().newPage()
  await second.goto(patientHref!)
  await expect(second.getByRole("heading", { name: "Alex Tan" })).toBeVisible()

  const captureId = `chunk-401-${randomUUID()}`
  const deviceId = `device-${randomUUID()}`
  await seedQueuedVoiceChunk(page, { captureId, deviceId, patientId })
  await page.goto(`${patientHref}/voice/capture`)
  await expect(page.getByText("Interrupted recording")).toBeVisible()
  await expect
    .poll(() =>
      page.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(true)

  let releaseLogout!: () => void
  const logoutGate = new Promise<void>((resolve) => {
    releaseLogout = resolve
  })
  await page.route("**/api/v1/auth/logout", async (route) => {
    await logoutGate
    await route.continue()
  })
  let rejectedChunkUploads = 0
  await page.route(
    `**/api/v1/voice/sessions/${captureId}/devices/${deviceId}/chunks/0`,
    async (route) => {
      rejectedChunkUploads += 1
      await route.fulfill({
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Synthetic expired session" }),
      })
    },
  )

  await page.getByRole("button", { name: "Resume upload" }).click()

  await expect(page.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  expect(rejectedChunkUploads).toBe(1)

  releaseLogout()
  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  await expect
    .poll(() =>
      second.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(false)
  await second.close()
})

test("an authentication-marked seal response terminates direct VoiceService calls", async ({
  page,
}) => {
  await signInAs(page, "staff")
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = patientIdFromHref(patientHref!)

  const second = await page.context().newPage()
  await second.goto(patientHref!)
  await expect(second.getByRole("heading", { name: "Alex Tan" })).toBeVisible()

  const captureId = `sealed-403-${randomUUID()}`
  const deviceId = `device-${randomUUID()}`
  await seedQueuedVoiceChunk(page, {
    captureId,
    deviceId,
    patientId,
    includeChunk: false,
  })
  await page.goto(`${patientHref}/voice/capture`)
  await expect(page.getByText("Interrupted recording")).toBeVisible()
  let pendingChunkPuts = 0
  page.on("request", (request) => {
    if (
      request.method() === "PUT" &&
      request
        .url()
        .includes(`/voice/sessions/${captureId}/devices/${deviceId}/chunks/`)
    ) {
      pendingChunkPuts += 1
    }
  })

  let releaseLogout!: () => void
  const logoutGate = new Promise<void>((resolve) => {
    releaseLogout = resolve
  })
  await page.route("**/api/v1/auth/logout", async (route) => {
    await logoutGate
    await route.continue()
  })
  let rejectedSeals = 0
  await page.route(
    `**/api/v1/voice/sessions/${captureId}/devices/${deviceId}/seal`,
    async (route) => {
      rejectedSeals += 1
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        headers: { "X-Nightingale-Session-Invalid": "1" },
        body: JSON.stringify({ detail: "Inactive membership" }),
      })
    },
  )

  await page.getByRole("button", { name: "Resume upload" }).click()

  await expect(page.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  expect(rejectedSeals).toBe(1)
  expect(pendingChunkPuts).toBe(0)

  releaseLogout()
  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  await expect
    .poll(() =>
      second.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(false)
  await second.close()
})

test("a markerless seal permission 403 keeps the current session and recovery", async ({
  page,
}) => {
  await signInAs(page, "staff")
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = patientIdFromHref(patientHref!)

  const captureId = `seal-rbac-${randomUUID()}`
  const deviceId = `device-${randomUUID()}`
  await seedQueuedVoiceChunk(page, {
    captureId,
    deviceId,
    patientId,
    includeChunk: false,
  })
  await page.goto(`${patientHref}/voice/capture`)
  await expect(page.getByText("Interrupted recording")).toBeVisible()

  let rejectedSeals = 0
  await page.route(
    `**/api/v1/voice/sessions/${captureId}/devices/${deviceId}/seal`,
    async (route) => {
      rejectedSeals += 1
      await route.fulfill({
        status: 403,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Device belongs to another member" }),
      })
    },
  )

  await page.getByRole("button", { name: "Resume upload" }).click()

  await expect.poll(() => rejectedSeals).toBe(1)
  await expect(page.getByTestId("session-termination-boundary")).toHaveCount(0)
  await expect(page.getByText("Interrupted recording")).toBeVisible()
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(true)
  expect(
    await page.evaluate(async () =>
      (await indexedDB.databases()).some(
        (database) => database.name === "nightingale-voice-v1",
      ),
    ),
  ).toBe(true)
})

test("an authentication-marked native 403 masks all tabs and clears IndexedDB", async ({
  page,
}) => {
  await signInAs(page, "staff")
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Tan" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = patientIdFromHref(patientHref!)

  const second = await page.context().newPage()
  await second.goto(`${patientHref}/voice/capture`)
  await expect(
    second.getByRole("heading", { name: "Record visit" }),
  ).toBeVisible()
  await seedQueuedVoiceChunk(second, {
    captureId: `inactive-403-${randomUUID()}`,
    deviceId: `device-${randomUUID()}`,
    patientId,
  })

  let releaseLogout!: () => void
  const logoutGate = new Promise<void>((resolve) => {
    releaseLogout = resolve
  })
  await page.route("**/api/v1/auth/logout", async (route) => {
    await logoutGate
    await route.continue()
  })
  let rejectedStreams = 0
  await page.route("**/api/v1/events/stream**", async (route) => {
    rejectedStreams += 1
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      headers: { "X-Nightingale-Session-Invalid": "1" },
      body: JSON.stringify({ detail: "Inactive membership" }),
    })
  })

  await page.goto(patientHref!)

  await expect(page.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByText("Alex Tan")).toHaveCount(0)
  expect(rejectedStreams).toBe(1)

  releaseLogout()
  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  await expect
    .poll(() =>
      second.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(false)
  await second.close()
})

test("an ordinary native permission 403 does not terminate the session", async ({
  page,
}) => {
  let deniedStreams = 0
  await page.route("**/api/v1/events/stream**", async (route) => {
    deniedStreams += 1
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "Clinical event role required" }),
    })
  })
  await signInAs(page, "staff")
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()

  await expect.poll(() => deniedStreams).toBeGreaterThan(0)
  await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
  await expect(page.getByTestId("session-termination-boundary")).toHaveCount(0)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(true)
})

test("expired cookie on direct login purges old voice before sign-in controls appear", async ({
  page,
}) => {
  await page.goto("/accept-invitation")
  await page.evaluate(async () => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("nightingale-voice-v1", 2)
      request.onupgradeneeded = () => {
        const opened = request.result
        opened.createObjectStore("captures", { keyPath: "id" })
        const chunks = opened.createObjectStore("chunks", { keyPath: "id" })
        chunks.createIndex("by-capture", "captureId")
        chunks.createIndex("by-capture-index", ["captureId", "chunkIndex"])
      }
      request.onerror = () => reject(request.error)
      request.onsuccess = () => resolve(request.result)
    })
    const transaction = db.transaction("captures", "readwrite")
    transaction.objectStore("captures").add({
      id: "stale-login-voice",
      patientId: "prior-persona",
      createdAt: new Date().toISOString(),
    })
    await new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve()
      transaction.onerror = () => reject(transaction.error)
    })
    db.close()
  })
  await page.context().addCookies([
    {
      name: "nightingale_session",
      value: "expired-cookie-fixture",
      url: new URL(page.url()).origin,
      httpOnly: true,
      secure: true,
      sameSite: "Lax",
    },
  ])

  let releaseLogout!: () => void
  const logoutGate = new Promise<void>((resolve) => {
    releaseLogout = resolve
  })
  await page.route("**/api/v1/auth/logout", async (route) => {
    await logoutGate
    await route.continue()
  })
  const navigation = page.goto("/login").catch(() => undefined)
  await expect(page.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(page.getByTestId("clinical-login-form")).toHaveCount(0)
  releaseLogout()
  await navigation
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByTestId("clinical-login-form")).toBeVisible()
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(false)
  await expect
    .poll(() =>
      page.evaluate(async () =>
        (await indexedDB.databases()).some(
          (database) => database.name === "nightingale-voice-v1",
        ),
      ),
    )
    .toBe(false)
})

test("[Scenario B] recipient accepts a one-time clinic invitation in the public form", async ({
  page,
  request,
}) => {
  const mailpitBaseUrl =
    process.env.MAILPIT_BASE_URL ??
    process.env.MAILPIT_HOST ??
    "http://localhost:8025"
  const email = `playwright-invite-${randomUUID().replaceAll("-", "")}@example.com`
  const password = `recipient-${randomUUID()}`
  const leakedRequestUrls: string[] = []
  page.on("request", (networkRequest) => {
    leakedRequestUrls.push(networkRequest.url())
  })

  await signInAs(page, "admin")
  await expect(page).toHaveURL(/\/admin$/)
  await page.getByLabel("Email").fill(email)
  await page.getByLabel("Display name").fill("Verified Browser Invite")
  await page.getByLabel("Role").selectOption("clinician")
  await page.getByRole("button", { name: "Send verified invitation" }).click()
  await expect(page.getByText(`Invitation sent to ${email}`)).toBeVisible()

  let oneTimeCode = ""
  await expect
    .poll(
      async () => {
        const listResponse = await request.get(
          `${mailpitBaseUrl}/api/v1/messages`,
        )
        if (!listResponse.ok()) return false
        const listing = (await listResponse.json()) as {
          messages?: Array<{ ID?: string; To?: Array<{ Address?: string }> }>
        }
        const message = listing.messages?.find((candidate) =>
          candidate.To?.some(
            (recipient) =>
              recipient.Address?.toLowerCase() === email.toLowerCase(),
          ),
        )
        if (!message?.ID) return false
        const detailResponse = await request.get(
          `${mailpitBaseUrl}/api/v1/message/${message.ID}`,
        )
        if (!detailResponse.ok()) return false
        const match = JSON.stringify(await detailResponse.json()).match(
          /[0-9a-f]{8}-[0-9a-f-]{27}\.[A-Za-z0-9_-]{40,}/i,
        )
        oneTimeCode = match?.[0] ?? ""
        return Boolean(oneTimeCode)
      },
      { timeout: 15_000 },
    )
    .toBe(true)

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "Sign out" }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect
    .poll(() =>
      page.evaluate(() =>
        localStorage.getItem("nightingale_session_termination_pending"),
      ),
    )
    .toBeNull()

  await page.goto(`/accept-invitation#${encodeURIComponent(oneTimeCode)}`)
  await expect(page).toHaveURL(/\/accept-invitation(?:#|$)/)
  await expect(page.getByLabel("One-time code")).toHaveValue(oneTimeCode)
  await page.getByLabel("Invited email").fill(email)
  await page.getByLabel("Display name").fill("Recipient Verified Name")
  await page.getByLabel("New password").fill(password)
  await page.getByLabel("Confirm password").fill(password)
  await page.getByRole("button", { name: "Activate account" }).click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByTestId("clinical-login-form")).toBeVisible()
  expect(leakedRequestUrls.some((url) => url.includes(oneTimeCode))).toBe(false)

  await signInAs(page, "admin")
  const acceptedMember = page.getByRole("row").filter({ hasText: email })
  await expect(acceptedMember).toBeVisible()
  await expect(
    acceptedMember.getByText("Recipient Verified Name"),
  ).toBeVisible()
})
