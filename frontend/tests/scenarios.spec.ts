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
  occurred_at: string
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
  const persona = {
    "Care staff": "staff",
    Clinician: "clinician",
    Patient: "patient",
    "Clinic admin": "admin",
  }[role]
  const response = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona },
  })
  expect(response.ok(), await response.text()).toBe(true)
  const destination =
    role === "Patient"
      ? "/patient/my-care"
      : role === "Clinic admin"
        ? "/admin"
        : "/patients"
  await page.goto(destination)
  await expect(page).toHaveURL(
    role === "Patient"
      ? /\/patient\/my-care\/?$/
      : role === "Clinic admin"
        ? /\/admin$/
        : /\/patients\/?$/,
  )
}

async function openAlex(page: Page) {
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()
  await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
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

async function patientAndTimeline(page: Page, displayName = "Alex Tan") {
  const patients = await api<{ data: Patient[] }>(
    page,
    `/api/v1/patients?search=${encodeURIComponent(displayName)}&limit=100`,
  )
  expect(patients.status).toBe(200)
  const patient = patients.body.data.find(
    (candidate) => candidate.display_name === displayName,
  )
  expect(patient).toBeDefined()
  const timeline = await api<{ data: TimelineEntry[] }>(
    page,
    `/api/v1/patients/${patient?.id}/timeline`,
  )
  expect(timeline.status).toBe(200)
  return { patient: patient as Patient, timeline: timeline.body.data }
}

async function uploadOrdinaryWav(
  page: Page,
  sessionId: string,
  patientId: string,
) {
  const joined = await api<{ id: string }>(
    page,
    `/api/v1/voice/sessions/${sessionId}/devices`,
    {
      body: {
        capture_role: "patient",
        client_device_id: `scenario-e-${Date.now()}`,
        expected_capture_kind: "patient",
        expected_patient_id: patientId,
      },
      method: "POST",
    },
  )
  expect(joined.status).toBe(201)
  const upload = await page.evaluate(
    async ({ deviceId, session }) => {
      const sampleRate = 16_000
      const sampleCount = sampleRate * 2
      const wav = new Uint8Array(44 + sampleCount * 2)
      const view = new DataView(wav.buffer)
      const write = (offset: number, value: string) => {
        for (const [index, character] of [...value].entries()) {
          view.setUint8(offset + index, character.charCodeAt(0))
        }
      }
      write(0, "RIFF")
      view.setUint32(4, wav.length - 8, true)
      write(8, "WAVEfmt ")
      view.setUint32(16, 16, true)
      view.setUint16(20, 1, true)
      view.setUint16(22, 1, true)
      view.setUint32(24, sampleRate, true)
      view.setUint32(28, sampleRate * 2, true)
      view.setUint16(32, 2, true)
      view.setUint16(34, 16, true)
      write(36, "data")
      view.setUint32(40, sampleCount * 2, true)
      for (let index = 0; index < sampleCount; index += 1) {
        const sample = Math.round(
          Math.sin((2 * Math.PI * 220 * index) / sampleRate) * 1200,
        )
        view.setInt16(44 + index * 2, sample, true)
      }
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", wav)),
      )
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("")
      const response = await fetch(
        `/api/v1/voice/sessions/${session}/devices/${deviceId}/chunks/0`,
        {
          body: wav,
          credentials: "same-origin",
          headers: {
            "Content-Type": "audio/wav",
            "X-Chunk-End-Ms": "2000",
            "X-Chunk-SHA256": digest,
            "X-Chunk-Start-Ms": "0",
          },
          method: "PUT",
        },
      )
      return { body: await response.json(), status: response.status }
    },
    { deviceId: joined.body.id, session: sessionId },
  )
  expect(upload).toMatchObject({
    body: { acknowledged: true, chunk_index: 0 },
    status: 200,
  })
  const sealed = await api(
    page,
    `/api/v1/voice/sessions/${sessionId}/devices/${joined.body.id}/seal`,
    { body: { last_chunk_index: 0 }, method: "POST" },
  )
  expect(sealed.status).toBe(200)
  return joined.body.id
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
    page.getByText("AI-assisted consultation note", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("AI-assisted nursing note", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("AI-assisted patient summary", { exact: true }),
  ).toBeVisible()

  const card = page.getByRole("listitem").filter({
    hasText: "AI doctor draft requires clinician review",
  })
  await card.getByRole("button", { name: "View source" }).click()

  const dialog = page.getByRole("dialog", { name: /Source details/ })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByText(/AI-assisted draft ·/)).toBeVisible()
  await expect(
    dialog.getByText(/Source status: current note version/),
  ).toBeVisible()
  await expect(dialog.locator("mark[data-source-span]")).toHaveText(
    "clinician review is required",
  )
  await expect(
    page.locator(
      'article[aria-label="AI-assisted consultation note: AI doctor consult summary"]' +
        '[data-entry-origin="ai"]' +
        '[data-entry-type="ai_doctor_consult_summary"]' +
        "[data-entry-version-id]",
    ),
  ).toHaveCount(1)
})

test("[Scenario B] collaboration, immutable diff/revert, audit and learning are demonstrable", async ({
  page,
}, testInfo) => {
  await login(page, "Care staff")
  await openAlex(page)

  const initialData = await patientAndTimeline(page)
  const clinicianTimelineEntry = initialData.timeline.find(
    (entry) => entry.entry_type === "manual_clinician_note",
  )
  expect(clinicianTimelineEntry).toBeDefined()
  const seededComments = await api<
    Array<{
      assigned_membership_id: string | null
      mentioned_user_ids: string[]
    }>
  >(page, `/api/v1/entries/${clinicianTimelineEntry?.id}/comments`)
  const clinicianUserId = seededComments.body[0]?.mentioned_user_ids[0]
  const clinicianMembershipId = seededComments.body[0]?.assigned_membership_id
  expect(clinicianUserId).toBeTruthy()
  expect(clinicianMembershipId).toBeTruthy()

  const staffEntry = page
    .locator('article[aria-label="Care staff note: Medication reconciliation"]')
    .filter({ hasText: "Medication list reviewed" })
  await staffEntry.getByRole("button", { name: "Change history" }).click()
  const drawer = page.getByRole("dialog", { name: /Change history/ })
  const version1 = drawer.getByRole("listitem").filter({
    hasText: "Version 1 · Medication reconciliation",
  })
  const version2 = drawer.getByRole("listitem").filter({
    hasText: "Version 2 · Medication reconciliation",
  })
  await expect(version1.getByText(/^Staff ·/)).toBeVisible()
  await version1.getByRole("button", { name: "Compare from" }).click()
  await version2.getByRole("button", { name: "Compare to" }).click()
  await expect(
    drawer.getByRole("heading", { name: "Changes", exact: true }),
  ).toBeVisible()
  await expect(drawer.locator("pre")).toContainText("duplicate evening dose")
  await version1.getByRole("button", { name: "Restore this version" }).click()
  await expect(drawer).toBeHidden()
  await expect(staffEntry).toContainText("Medication list reviewed during")

  const refreshedData = await patientAndTimeline(page)
  const revertedStaffEntry = refreshedData.timeline.find(
    (entry) =>
      entry.entry_type === "manual_staff_note" &&
      entry.title === "Medication reconciliation",
  )
  expect(revertedStaffEntry).toBeDefined()

  // Exercise the real Tiptap selection -> canonical anchor -> comment API path.
  const commentBody = `Scenario B anchored review ${testInfo.repeatEachIndex}-${Date.now()}`
  const exactQuote = "Medication list reviewed"
  await staffEntry.getByRole("button", { name: "Edit" }).click()
  const editDialog = page.getByRole("dialog", { name: "Edit note" })
  await expect(editDialog).toBeVisible()
  const editor = editDialog.getByLabel("Care note content")
  await expect(editor).toBeVisible()
  await editor.evaluate((root, quote) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
    let node = walker.nextNode()
    while (node) {
      const start = node.textContent?.indexOf(quote) ?? -1
      if (start >= 0) {
        const range = document.createRange()
        range.setStart(node, start)
        range.setEnd(node, start + quote.length)
        const selection = window.getSelection()
        selection?.removeAllRanges()
        selection?.addRange(range)
        root.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }))
        document.dispatchEvent(new Event("selectionchange", { bubbles: true }))
        return
      }
      node = walker.nextNode()
    }
    throw new Error(`Could not select ${quote}`)
  }, exactQuote)
  await editDialog.getByRole("button", { name: "Comment on selection" }).click()
  await expect(editDialog.getByText(`“${exactQuote}”`)).toBeVisible()
  await editDialog.getByLabel("Comment", { exact: true }).fill(commentBody)
  await editDialog
    .getByLabel("Mention (optional)")
    .selectOption(clinicianUserId as string)
  await editDialog
    .getByLabel("Assign to (optional)")
    .selectOption(clinicianMembershipId as string)
  await expect(editDialog.getByLabel("Mention (optional)")).toContainText(
    "Clinician — Clinician",
  )
  await expect(editDialog.getByLabel("Assign to (optional)")).toContainText(
    "Clinician — Clinician",
  )
  await editDialog
    .getByRole("button", { name: "Add to team discussion" })
    .click()
  await editDialog.getByRole("button", { name: "Cancel", exact: true }).click()
  await expect(editDialog).toBeHidden()

  await staffEntry.getByRole("button", { name: "Team discussion" }).click()
  const comment = page.getByRole("article").filter({ hasText: commentBody })
  await expect(comment).toBeVisible()
  await expect(comment.getByText(/^Assigned to /)).toHaveText(
    "Assigned to Clinician",
  )
  const commentsAfterCreate = await api<
    Array<{
      assigned_membership_id: string | null
      body: string
      id: string
      mentioned_user_ids: string[]
      resolved_at: string | null
    }>
  >(page, `/api/v1/entries/${revertedStaffEntry?.id}/comments`)
  const createdComment = commentsAfterCreate.body.find(
    (item) => item.body === commentBody,
  )
  expect(createdComment).toMatchObject({
    assigned_membership_id: clinicianMembershipId,
    mentioned_user_ids: [clinicianUserId],
    resolved_at: null,
  })

  const me = await api<{ membership_id: string }>(page, "/api/v1/auth/me")
  await comment.getByRole("button", { name: "Assign to me" }).click()
  await expect(comment.getByText(/^Assigned to /)).toHaveText(
    "Assigned to Staff",
  )
  await comment.getByRole("button", { name: "Resolve" }).click()
  await expect(comment.getByRole("button", { name: "Resolve" })).toHaveCount(0)
  await expect
    .poll(async () => {
      const comments = await api<
        Array<{
          assigned_membership_id: string | null
          id: string
          resolved_at: string | null
        }>
      >(page, `/api/v1/entries/${revertedStaffEntry?.id}/comments`)
      return comments.body.find((item) => item.id === createdComment?.id)
    })
    .toMatchObject({
      assigned_membership_id: me.body.membership_id,
      resolved_at: expect.any(String),
    })

  // Two independently persisted, non-critical priorities share a bounded
  // feature. A finally block removes them after every repeat without weakening
  // protected-priority behavior. Pinning the source must lift the peer score.
  const learningRun = `${testInfo.repeatEachIndex}_${Date.now()}`
  const featureKey = `entry_type:medication_review_${testInfo.repeatEachIndex}_${Date.now()}`
  const sourceLabel = `Medication safety priority ${learningRun}`
  const peerLabel = `Medication review follow-up ${learningRun}`
  const sourceContent = revertedStaffEntry?.content ?? ""
  const anchorFor = (quote: string) => {
    const quoteStart = sourceContent.indexOf(quote)
    expect(quoteStart).toBeGreaterThanOrEqual(0)
    return {
      end_offset: quoteStart + quote.length,
      exact_quote: quote,
      prefix: sourceContent.slice(Math.max(0, quoteStart - 16), quoteStart),
      start_offset: quoteStart,
      suffix: sourceContent.slice(
        quoteStart + quote.length,
        quoteStart + quote.length + 16,
      ),
    }
  }
  const makeHighlight = async (
    label: string,
    highlightAnchor: ReturnType<typeof anchorFor>,
    critical = false,
  ) => {
    const created = await api<{ id: string }>(
      page,
      `/api/v1/entries/${revertedStaffEntry?.id}/highlights`,
      {
        body: {
          ...highlightAnchor,
          clinician_confirmed: false,
          critical,
          feature_keys: [featureKey],
          label,
          patient_facing: false,
          entry_version_id: revertedStaffEntry?.version_id,
          unresolved: false,
        },
        method: "POST",
      },
    )
    expect(created.status).toBe(201)
    const accepted = await api(
      page,
      `/api/v1/highlights/${created.body.id}/accept`,
      {
        headers: { "Idempotency-Key": `scenario-b-accept-${created.body.id}` },
        method: "POST",
      },
    )
    expect(accepted.status).toBe(200)
    return created.body.id
  }
  const createdHighlightIds: string[] = []
  try {
    const sourceHighlightId = await makeHighlight(
      sourceLabel,
      anchorFor(exactQuote),
    )
    createdHighlightIds.push(sourceHighlightId)
    const peerHighlightId = await makeHighlight(
      peerLabel,
      anchorFor("home visit"),
    )
    createdHighlightIds.push(peerHighlightId)

    await page.reload()
    await expect(page.getByRole("heading", { name: "Alex Tan" })).toBeVisible()
    const beforeGlance = await api<{
      cards: Array<{
        highlight_id: string
        label: string
        score_components: { final: number; learned: number }
      }>
    }>(page, `/api/v1/patients/${initialData.patient.id}/glance`)
    const peerBefore = beforeGlance.body.cards.find(
      (card) => card.highlight_id === peerHighlightId,
    )
    expect(peerBefore).toBeDefined()

    const seededCritical = beforeGlance.body.cards.find(
      (card) => card.label === "Fall risk remains elevated",
    )
    expect(seededCritical).toBeDefined()
    const protectedReject = await api<{ detail: { code: string } }>(
      page,
      `/api/v1/highlights/${seededCritical?.highlight_id}/reject`,
      {
        headers: {
          "Idempotency-Key": `scenario-b-protected-${testInfo.repeatEachIndex}`,
        },
        method: "POST",
      },
    )
    expect(protectedReject).toMatchObject({
      body: {
        detail: {
          code: "PROTECTED_PRIORITY_REQUIRES_CLINICIAN_RESOLUTION",
        },
      },
      status: 409,
    })

    await page
      .getByRole("button", { name: `Keep ${sourceLabel} at top` })
      .click()
    await expect
      .poll(async () => {
        const glance = await api<{
          cards: Array<{
            highlight_id: string
            score_components: { final: number; learned: number }
          }>
        }>(page, `/api/v1/patients/${initialData.patient.id}/glance`)
        return (
          glance.body.cards.find(
            (card) => card.highlight_id === peerHighlightId,
          )?.score_components.learned ?? Number.NEGATIVE_INFINITY
        )
      })
      .toBeGreaterThan(
        peerBefore?.score_components.learned ?? Number.POSITIVE_INFINITY,
      )
    const afterGlance = await api<{
      cards: Array<{
        highlight_id: string
        score_components: { final: number; learned: number }
      }>
    }>(page, `/api/v1/patients/${initialData.patient.id}/glance`)
    const peerAfter = afterGlance.body.cards.find(
      (card) => card.highlight_id === peerHighlightId,
    )
    expect(peerAfter?.score_components.learned).toBeGreaterThan(
      peerBefore?.score_components.learned ?? Number.POSITIVE_INFINITY,
    )
    expect(peerAfter?.score_components.final).toBeGreaterThan(
      peerBefore?.score_components.final ?? Number.POSITIVE_INFINITY,
    )
    expect(
      afterGlance.body.cards.findIndex(
        (card) => card.highlight_id === peerHighlightId,
      ),
    ).toBeLessThan(5)
  } finally {
    for (const highlightId of createdHighlightIds) {
      const rejected = await api(
        page,
        `/api/v1/highlights/${highlightId}/reject`,
        {
          headers: { "Idempotency-Key": `scenario-b-cleanup-${highlightId}` },
          method: "POST",
        },
      )
      expect(rejected.status).toBe(200)
    }
  }

  await page.getByTestId("user-menu").click()
  await page.getByRole("menuitem", { name: "Sign out" }).click()
  await expect(page).toHaveURL(/\/login$/)
  await login(page, "Clinic admin")
  await expect(
    page.getByRole("heading", { name: "Clinic administration" }),
  ).toBeVisible()
  await expect(page.getByText("Activity log", { exact: true })).toBeVisible()
  await expect(page.getByText("Earlier note restored").first()).toBeVisible()
  await expect(page.getByText("Medication list reviewed during")).toHaveCount(0)
})

test("[Scenario C] cross-date timeline survives archive and checksum-verified rehydration", async ({
  page,
}) => {
  await login(page, "Clinician")
  const retentionData = await patientAndTimeline(page, "Rachel Lim")
  const historical = retentionData.timeline.find(
    (entry) => entry.title === "Resolved dermatitis follow-up",
  )
  const recent = retentionData.timeline.find(
    (entry) => entry.title === "Annual wellbeing review",
  )
  expect(historical).toBeDefined()
  expect(historical?.occurred_at).toMatch(/^2023-01-10/)
  expect(recent).toBeDefined()
  expect(recent?.occurred_at).toMatch(/^2026-02-06/)

  await page.goto(`/patients/${retentionData.patient.id}`)
  await expect(page.getByRole("heading", { name: "Rachel Lim" })).toBeVisible()
  await expect(page.locator('time[datetime^="2023-01-10"]')).toBeVisible()
  await expect(
    page.locator('time[datetime^="2026-02-06"]').first(),
  ).toBeVisible()

  type EntryVersionRecord = {
    content: string
    content_sha256: string
    id: string
    title: string
  }
  const versionsBefore = await api<{ data: EntryVersionRecord[] }>(
    page,
    `/api/v1/entries/${historical?.id}/versions`,
  )
  expect(versionsBefore.status).toBe(200)
  const versionBefore = versionsBefore.body.data.find(
    (version) => version.id === historical?.version_id,
  )
  expect(versionBefore).toMatchObject({
    content: historical?.content,
    id: historical?.version_id,
    title: historical?.title,
  })

  type DecayCandidate = {
    age_days: number
    eligible_for_cold: boolean
    entry_id: string
    entry_version_id: string
    protected_reasons: string[]
    storage_tier: string
  }
  const preview = await api<{
    candidates: DecayCandidate[]
  }>(page, "/api/v1/decay/preview")
  expect(preview.status).toBe(200)
  const candidate = preview.body.candidates.find(
    (item) => item.entry_version_id === historical?.version_id,
  )
  expect(candidate).toMatchObject({
    eligible_for_cold: true,
    entry_id: historical?.id,
    entry_version_id: historical?.version_id,
    protected_reasons: [],
  })

  let archivedSuccessfully = false
  let restored: ApiResult<{
    content_sha256: string
    storage_tier: string
  }> | null = null
  try {
    const archived = await api<{ archived_count: number }>(
      page,
      "/api/v1/decay/archive",
      {
        body: {
          dry_run: false,
          entry_version_ids: [historical?.version_id],
        },
        method: "POST",
      },
    )
    archivedSuccessfully = archived.status === 200
    expect(archived).toMatchObject({ body: { archived_count: 1 }, status: 200 })

    const previewAfterArchive = await api<{ candidates: DecayCandidate[] }>(
      page,
      "/api/v1/decay/preview",
    )
    expect(
      previewAfterArchive.body.candidates.find(
        (item) => item.entry_version_id === historical?.version_id,
      ),
    ).toMatchObject({ eligible_for_cold: false, storage_tier: "cold" })
  } finally {
    if (archivedSuccessfully) {
      restored = await api<{
        content_sha256: string
        storage_tier: string
      }>(page, `/api/v1/decay/entries/${historical?.version_id}/rehydrate`, {
        method: "POST",
      })
      expect(restored.status).toBe(200)
      expect(restored.body.storage_tier).toBe("warm")
    }
  }
  expect(restored).not.toBeNull()
  expect(restored?.body.content_sha256).toBe(versionBefore?.content_sha256)

  const versionsAfter = await api<{ data: EntryVersionRecord[] }>(
    page,
    `/api/v1/entries/${historical?.id}/versions`,
  )
  const versionAfter = versionsAfter.body.data.find(
    (version) => version.id === historical?.version_id,
  )
  expect(versionAfter).toEqual(versionBefore)

  const timelineAfter = await api<{ data: TimelineEntry[] }>(
    page,
    `/api/v1/patients/${retentionData.patient.id}/timeline`,
  )
  expect(
    timelineAfter.body.data.find(
      (entry) => entry.version_id === historical?.version_id,
    ),
  ).toMatchObject({
    content: historical?.content,
    occurred_at: historical?.occurred_at,
    title: historical?.title,
  })
  await page.reload()
  await expect(page.getByRole("heading", { name: "Rachel Lim" })).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Resolved dermatitis follow-up" }),
  ).toBeVisible()
  await expect(page.getByText(historical?.content ?? "")).toBeVisible()
  await expect(page.locator('time[datetime^="2023-01-10"]')).toBeVisible()
  await expect(
    page.locator('time[datetime^="2026-02-06"]').first(),
  ).toBeVisible()
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
      content:
        "Independent follow-up entry recorded during concurrent chart review.",
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
  const adminRead = await api<TimelineEntry>(
    admin,
    `/api/v1/entries/${original?.id}`,
  )
  expect(adminRead.status).toBe(200)
  expect(
    (
      await api(admin, `/api/v1/entries/${original?.id}`, {
        body: { content: "Admin clinical mutation must be rejected." },
        headers: { "If-Match": `"${adminRead.body.version_id}"` },
        method: "PATCH",
      })
    ).status,
  ).toBe(403)

  const otherContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const other = await otherContext.newPage()
  const otherLogin = await other.request.post("/api/v1/auth/demo-login", {
    data: { persona: "other_staff" },
  })
  expect(otherLogin.ok(), await otherLogin.text()).toBe(true)
  await other.goto("/patients")
  expect((await api(other, `/api/v1/entries/${original?.id}`)).status).toBe(404)

  await Promise.all([
    firstContext.close(),
    secondContext.close(),
    adminContext.close(),
    otherContext.close(),
  ])
})

test("[Scenario E] patient network is narrow, cookie-only, and provider-off is explicit", async ({
  browser,
  page,
}) => {
  test.slow()
  const payloads: unknown[] = []
  const requestUrls: string[] = []
  const authorizationHeaders: Array<string | undefined> = []
  const pending: Promise<void>[] = []
  const responseCaptureErrors: string[] = []
  page.on("request", (request) => {
    if (!request.url().includes("/api/v1/")) return
    requestUrls.push(request.url())
    authorizationHeaders.push(request.headers().authorization)
  })
  page.on("response", (response) => {
    if (!response.url().includes("/api/v1/")) return
    // SSE is intentionally long-lived and audio is binary. Only finite JSON
    // DTOs participate in the recursive patient-leak inspection below.
    if (
      !response
        .headers()
        ["content-type"]?.toLowerCase()
        .includes("application/json")
    )
      return
    const capture = response
      .json()
      .then((value) => {
        payloads.push(value)
      })
      .catch((error: unknown) => {
        responseCaptureErrors.push(
          `${response.url()}: ${error instanceof Error ? error.message : String(error)}`,
        )
      })
    pending.push(
      new Promise<void>((resolve) => {
        const timeout = setTimeout(() => {
          responseCaptureErrors.push(`${response.url()}: capture timed out`)
          resolve()
        }, 5_000)
        void capture.then(() => {
          clearTimeout(timeout)
          resolve()
        })
      }),
    )
  })

  await login(page, "Patient")
  await expect(
    page.getByRole("heading", { name: /My Care · Alex Tan/ }),
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

  const deviceId = await uploadOrdinaryWav(
    page,
    created.body.id,
    patientData.patient.id,
  )
  const finalized = await api<{ state: string }>(
    page,
    `/api/v1/voice/sessions/${created.body.id}/finalize`,
    {
      body: { devices: [{ device_id: deviceId, last_chunk_index: 0 }] },
      headers: { "Idempotency-Key": `scenario-e-${created.body.id}` },
      method: "POST",
    },
  )
  expect(finalized).toMatchObject({
    body: { state: "finalizing" },
    status: 202,
  })

  // The patient-safe status exposes the honest terminal state but not the
  // internal provider error. A clinician context independently verifies the
  // provider-disabled reason and pending-review warning.
  await expect
    .poll(
      async () =>
        (
          await api<{ state: string }>(
            page,
            `/api/v1/voice/sessions/${created.body.id}`,
          )
        ).body.state,
      { timeout: 30_000 },
    )
    .toBe("needs_review")
  const clinicianContext = await browser.newContext({ ignoreHTTPSErrors: true })
  const clinician = await clinicianContext.newPage()
  await login(clinician, "Clinician")
  const internalStatus = await api<{
    error_code: string | null
    state: string
    warning_codes: string[]
  }>(clinician, `/api/v1/voice/sessions/${created.body.id}`)
  expect(internalStatus).toMatchObject({
    body: {
      error_code: "ASR_PROVIDER_DISABLED",
      state: "needs_review",
      warning_codes: ["TRANSCRIPT_PENDING"],
    },
    status: 200,
  })
  const retainedAudio = await clinician.evaluate(async (sessionId) => {
    const response = await fetch(`/api/v1/voice/sessions/${sessionId}/audio`, {
      credentials: "same-origin",
    })
    const payload = await response.arrayBuffer()
    return {
      byteLength: payload.byteLength,
      contentType: response.headers.get("content-type"),
      status: response.status,
    }
  }, created.body.id)
  expect(retainedAudio.status).toBe(200)
  expect(retainedAudio.contentType).toContain("audio/wav")
  expect(retainedAudio.byteLength).toBeGreaterThan(44)
  await clinicianContext.close()

  const chunkStatus = await api<{ uploaded_chunks: number }>(
    page,
    `/api/v1/voice/sessions/${created.body.id}/chunks/status`,
  )
  expect(chunkStatus).toMatchObject({
    body: { uploaded_chunks: 1 },
    status: 200,
  })
  const patientAudioStatus = await page.evaluate(async (sessionId) => {
    const response = await fetch(`/api/v1/voice/sessions/${sessionId}/audio`, {
      credentials: "same-origin",
    })
    // Consume the finite denial DTO in the page as a normal client would.
    // Leaving the body unread can keep Playwright's parallel response capture
    // pending even though the status line has arrived.
    await response.json()
    return response.status
  }, created.body.id)
  expect(patientAudioStatus).toBe(403)

  // Snapshot because the response event handler can append while the page is
  // settling. A bounded capture makes any unfinished finite DTO an explicit
  // test failure instead of hanging the whole Scenario E run.
  await Promise.all([...pending])
  expect(responseCaptureErrors).toEqual([])

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

  await page.getByRole("button", { name: "Sign out" }).click()
  await expect(page).toHaveURL(/\/patient\/login$/)
  expect(
    (await page.context().cookies()).some(
      (cookie) => cookie.name === "nightingale_session",
    ),
  ).toBe(false)
})
