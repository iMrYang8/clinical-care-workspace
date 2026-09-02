import { expect, type Page, test } from "@playwright/test"
import { recordingCodeFromSessionId } from "../src/features/routeReferences"

test.use({ storageState: { cookies: [], origins: [] } })

const rawUuidPattern =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/i

async function signInAsClinician(page: Page): Promise<void> {
  const response = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona: "clinician" },
  })
  expect(response.ok(), await response.text()).toBe(true)
  await page.goto("/patients")
}

async function joinUploadAndSealSecondDevice(
  page: Page,
  sessionId: string,
  patientId: string,
) {
  return page.evaluate(
    async ({ patient, session }) => {
      const joinResponse = await fetch(
        `/api/v1/voice/sessions/${session}/devices`,
        {
          body: JSON.stringify({
            capture_role: "clinician",
            client_device_id: `scenario-f-second-${crypto.randomUUID()}`,
            expected_capture_kind: "clinical",
            expected_patient_id: patient,
          }),
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          method: "POST",
        },
      )
      const joined = (await joinResponse.json()) as { id: string }
      if (joinResponse.status !== 201) {
        throw new Error(`Second device join failed (${joinResponse.status})`)
      }

      const sampleRate = 16_000
      const sampleCount = sampleRate * 11
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
        view.setInt16(
          44 + index * 2,
          Math.round(
            Math.sin((2 * Math.PI * 330 * index) / sampleRate) * 1_000,
          ),
          true,
        )
      }
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", wav)),
      )
        .map((value) => value.toString(16).padStart(2, "0"))
        .join("")
      const uploadResponse = await fetch(
        `/api/v1/voice/sessions/${session}/devices/${joined.id}/chunks/0`,
        {
          body: wav,
          credentials: "same-origin",
          headers: {
            "Content-Type": "audio/wav",
            "X-Chunk-End-Ms": "11000",
            "X-Chunk-SHA256": digest,
            "X-Chunk-Start-Ms": "0",
          },
          method: "PUT",
        },
      )
      if (uploadResponse.status !== 200) {
        throw new Error(
          `Second device upload failed (${uploadResponse.status})`,
        )
      }
      const upload = (await uploadResponse.json()) as {
        acknowledged: boolean
      }
      const sealResponse = await fetch(
        `/api/v1/voice/sessions/${session}/devices/${joined.id}/seal`,
        {
          body: JSON.stringify({ last_chunk_index: 0 }),
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          method: "POST",
        },
      )
      if (sealResponse.status !== 200) {
        throw new Error(`Second device seal failed (${sealResponse.status})`)
      }
      return {
        acknowledged: upload.acknowledged,
        deviceId: joined.id,
        sealed: (await sealResponse.json()) as {
          last_chunk_index: number
          sealed: boolean
        },
      }
    },
    { patient: patientId, session: sessionId },
  )
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/voice/sessions", async (route) => {
    if (route.request().method() !== "POST") {
      await route.continue()
      return
    }
    const body = route.request().postDataJSON() as Record<string, unknown>
    await route.continue({
      postData: JSON.stringify({
        ...body,
        synthetic_fixture: true,
        fixture_id: "code-switch-overlap-v1",
      }),
    })
  })
  await page.addInitScript(() => {
    const wav = new Uint8Array(44 + 16_000 * 2 * 11)
    const view = new DataView(wav.buffer)
    const write = (offset: number, value: string) =>
      [...value].forEach((character, index) => {
        view.setUint8(offset + index, character.charCodeAt(0))
      })
    write(0, "RIFF")
    view.setUint32(4, wav.length - 8, true)
    write(8, "WAVEfmt ")
    view.setUint32(16, 16, true)
    view.setUint16(20, 1, true)
    view.setUint16(22, 1, true)
    view.setUint32(24, 16_000, true)
    view.setUint32(28, 32_000, true)
    view.setUint16(32, 2, true)
    view.setUint16(34, 16, true)
    write(36, "data")
    view.setUint32(40, wav.length - 44, true)

    class MockMediaRecorder extends EventTarget {
      static isTypeSupported(type: string) {
        return type.includes("webm") || type === "audio/mp4"
      }
      mimeType = "audio/wav"
      state = "inactive"
      timer: number | undefined
      constructor() {
        super()
        if (localStorage.getItem("voice-recorder-constructor-fails") === "1") {
          throw new DOMException(
            "Synthetic recorder construction failed",
            "NotSupportedError",
          )
        }
      }
      start() {
        this.state = "recording"
        this.timer = window.setTimeout(() => {
          this.dispatchEvent(
            new BlobEvent("dataavailable", {
              data: new Blob([wav.slice(0, Math.floor(wav.length / 2))], {
                type: "audio/wav",
              }),
            }),
          )
        }, 20)
      }
      stop() {
        if (this.timer) window.clearTimeout(this.timer)
        this.state = "inactive"
        this.dispatchEvent(
          new BlobEvent("dataavailable", {
            data: new Blob([wav.slice(Math.floor(wav.length / 2))], {
              type: "audio/wav",
            }),
          }),
        )
        queueMicrotask(() => this.dispatchEvent(new Event("stop")))
      }
    }
    class MockAudioContext {
      createAnalyser() {
        return {
          fftSize: 512,
          getByteTimeDomainData(values: Uint8Array) {
            values.fill(128)
          },
        }
      }
      createMediaStreamSource() {
        return { connect() {} }
      }
      close() {
        return Promise.resolve()
      }
    }
    const track = { label: "Synthetic browser microphone", stop() {} }
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => ({
          getAudioTracks: () => [track],
          getTracks: () => [track],
        }),
      },
    })
    Object.defineProperty(navigator, "onLine", {
      configurable: true,
      get: () => localStorage.getItem("voice-force-offline") !== "1",
    })
    Object.assign(window, {
      MediaRecorder: MockMediaRecorder,
      AudioContext: MockAudioContext,
    })
  })
})

test("[Scenario F] two-device recovery proves review evidence and clinician publish", async ({
  page,
}) => {
  await signInAsClinician(page)
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()
  await page.getByRole("link", { name: "Record visit" }).click()
  const sessionCreated = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname === "/api/v1/voice/sessions" &&
      response.status() === 201,
  )
  await page.getByRole("button", { name: "Start recording" }).click()
  const session = (await (await sessionCreated).json()) as {
    id: string
    patient_id: string
  }
  await expect(page.getByText("Recording", { exact: true })).toBeVisible()
  await expect(page.getByText("Recording and uploading securely")).toBeVisible()
  const secondDevice = await joinUploadAndSealSecondDevice(
    page,
    session.id,
    session.patient_id,
  )
  expect(secondDevice).toMatchObject({
    acknowledged: true,
    sealed: { last_chunk_index: 0, sealed: true },
  })
  await expect(
    page.getByRole("button", { name: "Stop & finalize" }),
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "Resume upload" })).toHaveCount(
    0,
  )

  await page.evaluate(() => {
    localStorage.setItem("voice-force-offline", "1")
    window.dispatchEvent(new Event("offline"))
  })
  await page.getByRole("button", { name: "Stop & finalize" }).click()
  await expect(page.getByText(/remains securely saved/i)).toBeVisible()
  await page.reload()
  await expect(page.getByText("Recordings waiting to upload")).toBeVisible()

  await page.evaluate(() => localStorage.removeItem("voice-force-offline"))
  await page.reload()
  await page.getByRole("button", { name: "Resume upload" }).click()
  await expect(page).toHaveURL(/\/voice\/.+\/review/)
  await expect(page).toHaveURL(
    new RegExp(`/voice/${recordingCodeFromSessionId(session.id)}/review$`),
  )
  expect(page.url()).not.toMatch(rawUuidPattern)
  await expect(page.getByTestId("voice-review-mode")).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Review visit recording" }),
  ).toBeVisible()
  await expect(
    page.getByText("Clinical findings", { exact: true }).first(),
  ).toBeVisible()

  await expect
    .poll(
      async () =>
        page.evaluate(async (sessionId) => {
          const response = await fetch(
            `/api/v1/voice/sessions/${sessionId}/transcript`,
            { credentials: "same-origin" },
          )
          return response.ok
        }, session.id),
      { timeout: 30_000 },
    )
    .toBe(true)
  const revision = await page.evaluate(async (sessionId) => {
    const response = await fetch(
      `/api/v1/voice/sessions/${sessionId}/transcript`,
      { credentials: "same-origin" },
    )
    return (await response.json()) as {
      facts: Array<{
        audio_start_ms: number
        exact_quote: string
        fact_type: string
        transcript_end: number
        transcript_start: number
      }>
      id: string
      segments: Array<{
        confidence: number | null
        detected_language: string | null
        end_ms: number
        id: string
        language_confidence: number | null
        language_spans: Array<{
          confidence: number | null
          detection_source: string
          end_offset: number
          language_code: "en" | "ms" | "nan" | "zh" | "cmn" | "und"
          review_required: boolean
          start_offset: number
        }>
        overlap_group_id: string | null
        speaker_id: string | null
        source_language: string
        start_ms: number
        text: string
        text_end: number
        text_start: number
      }>
    }
  }, session.id)
  expect(revision.segments).toHaveLength(2)
  expect(revision.segments).toMatchObject([
    {
      // Provider self-reported confidence is intentionally discarded. The
      // trusted band is supplied only by a matching calibration report.
      confidence: null,
      detected_language: "en",
      end_ms: 5200,
      language_confidence: 1,
      language_spans: [
        {
          confidence: 1,
          detection_source: "lexicon_and_provider",
          end_offset: 73,
          language_code: "en",
          review_required: false,
          start_offset: 0,
        },
      ],
      overlap_group_id: null,
      speaker_id: "SPEAKER_00",
      source_language: "en",
      start_ms: 0,
    },
    {
      confidence: null,
      detected_language: "zh",
      end_ms: 10200,
      language_confidence: 1,
      language_spans: [
        {
          confidence: 1,
          detection_source: "lexicon_and_provider",
          end_offset: 42,
          language_code: "zh",
          review_required: false,
          start_offset: 0,
        },
      ],
      overlap_group_id: "overlap-1",
      speaker_id: "SPEAKER_01",
      source_language: "zh",
      start_ms: 4800,
    },
  ])
  const fact = revision.facts[0]
  expect(fact).toMatchObject({
    audio_start_ms: 0,
    exact_quote: "penicillin allergy",
    fact_type: "allergy",
  })
  const factSegment = revision.segments.find(
    (segment) =>
      segment.text_start <= fact.transcript_start &&
      segment.text_end >= fact.transcript_end,
  )
  expect(factSegment?.speaker_id).toBe("SPEAKER_00")

  const desktopTranscript = page.getByTestId("transcript-panel-desktop")
  await expect(desktopTranscript.getByText("Speaker 1")).toBeVisible()
  await expect(desktopTranscript.getByText("Speaker 2")).toBeVisible()
  await expect(
    desktopTranscript.getByText("English (en)", { exact: true }),
  ).toBeVisible()
  await expect(
    desktopTranscript.getByText("Chinese (zh)", { exact: true }),
  ).toBeVisible()
  await expect(
    desktopTranscript.getByText(
      /English \(en\) · characters 0–73 · lexicon and provider · 100% confidence/,
    ),
  ).toBeVisible()
  await expect(
    desktopTranscript.getByText(
      /Chinese \(zh\) · characters 0–42 · lexicon and provider · 100% confidence/,
    ),
  ).toBeVisible()
  await expect(
    desktopTranscript.getByText("Confidence unavailable", { exact: true }),
  ).toHaveCount(2)
  await expect(
    desktopTranscript.getByText("Overlapping speech", { exact: true }),
  ).toBeVisible()
  await expect(
    desktopTranscript.getByText(/Patient reports a penicillin allergy/),
  ).toBeVisible()
  await expect(desktopTranscript.getByText(/医生：好的/)).toBeVisible()
  await expect(
    desktopTranscript.getByRole("button", { name: "Jump to 0:00" }),
  ).toBeVisible()
  await expect(
    desktopTranscript.getByRole("button", { name: "Jump to 0:04" }),
  ).toBeVisible()

  const dualDeviceStatus = await page.evaluate(async (sessionId) => {
    const response = await fetch(
      `/api/v1/voice/sessions/${sessionId}/chunks/status`,
      { credentials: "same-origin" },
    )
    return (await response.json()) as {
      devices: Array<{
        device_id: string
        last_declared_chunk_index: number | null
        received_indices: number[]
      }>
      uploaded_chunks: number
    }
  }, session.id)
  expect(dualDeviceStatus.uploaded_chunks).toBe(3)
  expect(dualDeviceStatus.devices).toHaveLength(2)
  expect(
    dualDeviceStatus.devices.find(
      (device) => device.device_id === secondDevice.deviceId,
    ),
  ).toMatchObject({ last_declared_chunk_index: 0, received_indices: [0] })
  expect(
    dualDeviceStatus.devices.find(
      (device) => device.device_id !== secondDevice.deviceId,
    ),
  ).toMatchObject({ last_declared_chunk_index: 1, received_indices: [0, 1] })

  const audio = page.locator("audio")
  await expect(audio).toBeVisible()
  await expect
    .poll(
      () =>
        audio.evaluate(
          (element) =>
            element instanceof HTMLAudioElement &&
            element.readyState >= HTMLMediaElement.HAVE_METADATA,
        ),
      { timeout: 15_000 },
    )
    .toBe(true)
  await desktopTranscript.getByRole("button", { name: "Jump to 0:04" }).click()
  await expect
    .poll(() =>
      audio.evaluate((element) => (element as HTMLAudioElement).currentTime),
    )
    .toBeGreaterThan(4.7)
  expect(
    await audio.evaluate(
      (element) => (element as HTMLAudioElement).currentTime,
    ),
  ).toBeLessThan(4.95)

  const lowConfidence = page.getByLabel(
    "Show uncertain or overlapping speech only",
  )
  await lowConfidence.check()
  // Uncalibrated segments abstain as Confidence unavailable and therefore
  // remain in the review filter rather than being hidden as if trustworthy.
  await expect(desktopTranscript.getByText("Speaker 1")).toBeVisible()
  await expect(desktopTranscript.getByText("Speaker 2")).toBeVisible()
  const factButton = page
    .locator('[data-testid="facts-panel"]:visible')
    .getByRole("button", { name: /allergy/i })
  await factButton.click()
  await expect(lowConfidence).not.toBeChecked()
  const targetSegment = page.locator(
    `#voice-segment-desktop-${factSegment?.id}`,
  )
  await expect(targetSegment).toBeVisible()
  await expect(targetSegment).toBeInViewport()
  await expect
    .poll(() =>
      audio.evaluate((element) => (element as HTMLAudioElement).currentTime),
    )
    .toBeLessThan(1)

  const publish = page.getByRole("button", {
    name: /Publish reviewed note/i,
  })
  await expect(publish).toBeEnabled()
  await publish.click()
  await expect(
    page.getByText("The reviewed visit note was added to the care record."),
  ).toBeVisible()
  await expect(
    page.getByText("Published", { exact: true }).last(),
  ).toBeVisible()
  const published = await page.evaluate(async (sessionId) => {
    const response = await fetch(`/api/v1/voice/sessions/${sessionId}`, {
      credentials: "same-origin",
    })
    return (await response.json()) as {
      published_entry_id: string | null
      state: string
    }
  }, session.id)
  expect(published.state).toBe("published")
  expect(published.published_entry_id).toBeTruthy()
  const timeline = await page.evaluate(async (patientId) => {
    const response = await fetch(`/api/v1/patients/${patientId}/timeline`, {
      credentials: "same-origin",
    })
    return (await response.json()) as {
      data: Array<{ entry_type: string; id: string }>
    }
  }, session.patient_id)
  expect(
    timeline.data.find((entry) => entry.id === published.published_entry_id),
  ).toMatchObject({ entry_type: "voice_reviewed_result" })
})

test("local storage failure stops capture without poisoning recovery", async ({
  page,
}) => {
  await signInAsClinician(page)
  await page.getByRole("link", { name: "Open care note for Alex Tan" }).click()
  await page.getByRole("link", { name: "Record visit" }).click()
  await page.evaluate(() => {
    const originalAdd = IDBObjectStore.prototype.add
    IDBObjectStore.prototype.add = function (value, key) {
      if (this.name === "chunks") {
        throw new DOMException(
          "Synthetic quota exhausted",
          "QuotaExceededError",
        )
      }
      return key === undefined
        ? originalAdd.call(this, value)
        : originalAdd.call(this, value, key)
    }
  })
  await page.getByRole("button", { name: "Start recording" }).click()

  await expect(page.getByText(/Secure local storage failed/i)).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Stop & finalize" }),
  ).toHaveCount(0)
  await expect(page.getByText("Recordings waiting to upload")).toBeVisible()
  await expect(page.getByText("no audio stored").first()).toBeVisible()
  await expect(page.getByRole("button", { name: "Resume upload" })).toHaveCount(
    0,
  )
  const abandon = page.getByRole("button", { name: "Abandon empty device" })
  await expect(abandon.first()).toBeVisible()
  await abandon.first().click({ force: true })
  await expect(abandon).toHaveCount(0)
  await expect(page.getByText("Recordings waiting to upload")).toHaveCount(0)

  // Reload restores the browser storage implementation. A fresh recording can
  // start, proving the failed write did not leave recovery in a poisoned state.
  await page.reload()
  await page.getByRole("button", { name: "Start recording" }).click()
  await expect(page.getByText("Recording and uploading securely")).toBeVisible()
  await page.getByRole("button", { name: "Stop & finalize" }).click()
  await expect(page).toHaveURL(/\/voice\/.+\/review/)
  expect(page.url()).not.toMatch(rawUuidPattern)
})

for (const failure of ["recorder constructor", "capture IndexedDB"] as const) {
  test(`${failure} failure abandons the joined empty server track`, async ({
    page,
  }) => {
    const deletes: string[] = []
    page.on("request", (request) => {
      if (
        request.method() === "DELETE" &&
        /\/api\/v1\/voice\/sessions\/.+\/devices\/.+$/.test(request.url())
      ) {
        deletes.push(request.url())
      }
    })

    await signInAsClinician(page)
    await page
      .getByRole("link", { name: "Open care note for Alex Tan" })
      .click()
    await page.getByRole("link", { name: "Record visit" }).click()

    if (failure === "recorder constructor") {
      await page.evaluate(() =>
        localStorage.setItem("voice-recorder-constructor-fails", "1"),
      )
    } else {
      await page.evaluate(() => {
        const originalPut = IDBObjectStore.prototype.put
        IDBObjectStore.prototype.put = function (value, key) {
          if (this.name === "captures") {
            throw new DOMException(
              "Synthetic capture write failed",
              "QuotaExceededError",
            )
          }
          return key === undefined
            ? originalPut.call(this, value)
            : originalPut.call(this, value, key)
        }
      })
    }

    await page.getByRole("button", { name: "Start recording" }).click()

    await expect(
      page.getByText(/Recording could not start\. Check the recording code/i),
    ).toBeVisible()
    await expect.poll(() => deletes.length).toBe(1)
    await expect(
      page.getByRole("button", { name: "Abandon empty device" }),
    ).toHaveCount(0)
  })
}
