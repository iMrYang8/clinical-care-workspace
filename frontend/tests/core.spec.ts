import { randomUUID } from "node:crypto"
import type { Page } from "@playwright/test"
import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

async function seedQueuedVoiceChunk(
  page: Page,
  input: { captureId: string; deviceId: string; patientId: string },
): Promise<void> {
  await page.evaluate(async (fixture) => {
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
      mediaType: "audio/webm",
      key,
      nextChunkIndex: 1,
      createdAt: new Date().toISOString(),
    })
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
    await new Promise<void>((resolve, reject) => {
      transaction.oncomplete = () => resolve()
      transaction.onerror = () => reject(transaction.error)
      transaction.onabort = () => reject(transaction.error)
    })
    db.close()
  }, input)
}

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
  await expect(
    page.getByRole("heading", { name: "Timeline", exact: true }),
  ).toBeVisible()
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

test("failed network and CSRF logout stay masked until a confirmed retry", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()
  await expect(page).toHaveURL(/\/patients\/?$/)

  const second = await page.context().newPage()
  await second.goto("/patients")
  await second
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .click()
  await expect(
    second.getByRole("heading", { name: "Alex Synthetic" }),
  ).toBeVisible()
  await expect(second.getByText("What matters now")).toBeVisible()

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
  await page.getByRole("menuitem", { name: "Log out and clear data" }).click()

  const boundary = page.getByTestId("session-termination-boundary")
  await expect(
    boundary.getByRole("heading", { name: "Session termination incomplete" }),
  ).toBeVisible()
  await expect(boundary).toContainText("server did not confirm logout")
  await expect(boundary).toContainText("You are not logged out yet")
  await expect(page).toHaveURL(/\/patients\/?$/)
  await expect(
    page.getByRole("heading", { name: "Clinical care notes" }),
  ).toHaveCount(0)
  const secondBoundary = second.getByTestId("session-termination-boundary")
  await expect(
    secondBoundary.getByRole("heading", {
      name: "Session termination incomplete",
    }),
  ).toBeVisible()
  await expect(second.getByText("Alex Synthetic")).toHaveCount(0)
  await expect(second.getByText("What matters now")).toHaveCount(0)
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
  await expect(boundary).toContainText("CSRF origin rejected")
  expect(csrfStatus).toBe(403)
  await expect(secondBoundary).toContainText("CSRF origin rejected")
  await expect(second.getByText("Alex Synthetic")).toHaveCount(0)
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
  expect(
    await page.evaluate(() =>
      localStorage.getItem("nightingale_session_termination_pending"),
    ),
  ).toBeNull()
  await second.close()
})

test("confirmed logout masks a second tab and closes its held voice database", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()
  await expect(page).toHaveURL(/\/patients\/?$/)
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()

  const second = await page.context().newPage()
  await second.goto(`${patientHref}/voice/capture`)
  await expect(
    second.getByRole("heading", { name: "Secure voice capture" }),
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
  await page.getByRole("menuitem", { name: "Log out and clear data" }).click()

  await expect(page).toHaveURL(/\/login$/)
  await expect(second).toHaveURL(/\/login$/)
  await expect(
    second.getByRole("heading", { name: "Secure voice capture" }),
  ).toHaveCount(0)
  await expect(second.getByText("Alex Synthetic")).toHaveCount(0)
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

test("a revoked session masks every tab and deletes old encrypted voice before another persona", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = new URL(patientHref!, "https://proxy").pathname.split(
    "/",
  )[2]
  expect(patientId).toBeTruthy()

  const second = await page.context().newPage()
  await second.goto("/patients")
  await second.evaluate(async (fixturePatientId) => {
    const db = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("nightingale-voice-v1", 2)
      request.onupgradeneeded = () => {
        const opened = request.result
        const captures = opened.createObjectStore("captures", { keyPath: "id" })
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
  }, patientId)
  await second.goto(`${patientHref}/voice/capture`)
  await expect(
    second.getByRole("heading", { name: "Encrypted uploads to recover" }),
  ).toBeVisible()
  await expect(second.getByText("expired-session-fixture")).toBeVisible()

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
        status: 401,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Synthetic session revoked" }),
      })
      return
    }
    await route.continue()
  })

  const reload = page.reload().catch(() => undefined)
  await expect(page.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByTestId("session-termination-boundary")).toBeVisible()
  await expect(second.getByText("Alex Synthetic")).toHaveCount(0)
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
  await page.getByRole("button", { name: "Continue as Patient" }).click()
  await page.getByRole("link", { name: "Add a recording" }).click()
  await expect(
    page.getByRole("heading", { name: "Secure voice capture" }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Encrypted uploads to recover" }),
  ).toHaveCount(0)
  await expect(page.getByText("expired-session-fixture")).toHaveCount(0)
})

test("a native voice chunk 401 terminates every tab and purges encrypted recovery", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = new URL(patientHref!, "https://proxy").pathname.split(
    "/",
  )[2]
  expect(patientId).toBeTruthy()

  const second = await page.context().newPage()
  await second.goto(patientHref!)
  await expect(
    second.getByRole("heading", { name: "Alex Synthetic" }),
  ).toBeVisible()

  const captureId = `chunk-401-${randomUUID()}`
  const deviceId = `device-${randomUUID()}`
  await seedQueuedVoiceChunk(page, { captureId, deviceId, patientId })
  await page.goto(`${patientHref}/voice/capture`)
  await expect(page.getByText(captureId)).toBeVisible()
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
  await expect(second.getByText("Alex Synthetic")).toHaveCount(0)
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

test("an authentication-marked native 403 masks all tabs and clears IndexedDB", async ({
  page,
}) => {
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()
  const patientHref = await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .getAttribute("href")
  expect(patientHref).toBeTruthy()
  const patientId = new URL(patientHref!, "https://proxy").pathname.split(
    "/",
  )[2]
  expect(patientId).toBeTruthy()

  const second = await page.context().newPage()
  await second.goto(`${patientHref}/voice/capture`)
  await expect(
    second.getByRole("heading", { name: "Secure voice capture" }),
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
  await expect(second.getByText("Alex Synthetic")).toHaveCount(0)
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
  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Care staff" }).click()
  await page
    .getByRole("link", { name: "Open care note for Alex Synthetic" })
    .click()

  await expect.poll(() => deniedStreams).toBeGreaterThan(0)
  await expect(
    page.getByRole("heading", { name: "Alex Synthetic" }),
  ).toBeVisible()
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
  await expect(
    page.getByRole("button", { name: "Continue as Care staff" }),
  ).toHaveCount(0)
  releaseLogout()
  await navigation
  await expect(page).toHaveURL(/\/login$/)
  await expect(
    page.getByRole("button", { name: "Continue as Care staff" }),
  ).toBeVisible()
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
  const email = `playwright-invite-${randomUUID()}@example.com`
  const password = `recipient-${randomUUID()}`
  const leakedRequestUrls: string[] = []
  page.on("request", (networkRequest) => {
    leakedRequestUrls.push(networkRequest.url())
  })

  await page.goto("/login")
  await page.getByRole("button", { name: "Continue as Clinic admin" }).click()
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
          "http://mailpit:8025/api/v1/messages",
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
          `http://mailpit:8025/api/v1/message/${message.ID}`,
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
  await page.getByRole("menuitem", { name: "Log out and clear data" }).click()
  await expect(page).toHaveURL(/\/login$/)

  await page.goto(`/accept-invitation#${encodeURIComponent(oneTimeCode)}`)
  await expect(page).toHaveURL(/\/accept-invitation$/)
  await expect(page.getByLabel("One-time code")).toHaveValue(oneTimeCode)
  await page.getByLabel("Invited email").fill(email)
  await page.getByLabel("Display name").fill("Recipient Verified Name")
  await page.getByLabel("New password").fill(password)
  await page
    .getByRole("button", { name: "Verify and activate membership" })
    .click()
  await expect(page).toHaveURL(/\/login$/)
  await expect(
    page.getByRole("button", { name: "Continue as Clinic admin" }),
  ).toBeVisible()
  expect(leakedRequestUrls.some((url) => url.includes(oneTimeCode))).toBe(false)

  await page.getByRole("button", { name: "Continue as Clinic admin" }).click()
  const acceptedMember = page.getByRole("row").filter({ hasText: email })
  await expect(acceptedMember).toBeVisible()
  await expect(
    acceptedMember.getByText("Recipient Verified Name"),
  ).toBeVisible()
})
