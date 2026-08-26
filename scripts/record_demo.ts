import { spawn } from "node:child_process"
import { mkdir, mkdtemp, rm, stat } from "node:fs/promises"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"

import { chromium, type Locator, type Page } from "@playwright/test"

const root = resolve(import.meta.dir, "..")
const output = resolve(
  process.env.NIGHTINGALE_DEMO_VIDEO ??
    join(root, "output", "demo", "Nightingale_Demo.mp4"),
)
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "https://localhost"
const trustedOriginOverride =
  process.env.NIGHTINGALE_DEMO_TRUSTED_ORIGIN?.trim() || null
const temp = await mkdtemp(join(tmpdir(), "nightingale-demo-recording-"))
const failures: string[] = []

function run(command: string, args: string[]): Promise<void> {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(command, args, { stdio: "inherit" })
    child.once("error", rejectRun)
    child.once("exit", (code) => {
      if (code === 0) resolveRun()
      else rejectRun(new Error(`${command} exited with status ${code}`))
    })
  })
}

function capture(command: string, args: string[]): Promise<string> {
  return new Promise((resolveCapture, rejectCapture) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk)
    })
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk)
    })
    child.once("error", rejectCapture)
    child.once("exit", (code) => {
      if (code === 0) resolveCapture(stdout)
      else
        rejectCapture(
          new Error(`${command} exited with status ${code}: ${stderr.trim()}`),
        )
    })
  })
}

async function pause(page: Page, milliseconds = 1_000) {
  await page.waitForTimeout(milliseconds)
}

async function scene(page: Page, eyebrow: string, title: string) {
  await page.evaluate(
    ({ sceneEyebrow, sceneTitle }) => {
      document.querySelector("[data-demo-scene]")?.remove()
      const label = document.createElement("div")
      label.dataset.demoScene = "true"
      label.setAttribute("aria-hidden", "true")
      Object.assign(label.style, {
        alignItems: "center",
        background:
          "linear-gradient(110deg, rgba(8,47,73,.96), rgba(15,118,110,.94))",
        border: "1px solid rgba(255,255,255,.24)",
        borderRadius: "18px",
        bottom: "30px",
        boxShadow: "0 20px 60px rgba(2,6,23,.32)",
        color: "white",
        display: "flex",
        fontFamily:
          "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
        gap: "16px",
        left: "32px",
        maxWidth: "760px",
        opacity: "0",
        padding: "14px 20px",
        pointerEvents: "none",
        position: "fixed",
        transform: "translateY(12px)",
        transition: "opacity 220ms ease, transform 220ms ease",
        zIndex: "2147483647",
      })
      const number = document.createElement("div")
      number.textContent = sceneEyebrow
      Object.assign(number.style, {
        color: "#99f6e4",
        fontSize: "11px",
        fontWeight: "800",
        letterSpacing: ".14em",
        textTransform: "uppercase",
        whiteSpace: "nowrap",
      })
      const heading = document.createElement("div")
      heading.textContent = sceneTitle
      Object.assign(heading.style, {
        fontFamily: "Georgia, ui-serif, serif",
        fontSize: "22px",
        fontWeight: "700",
        letterSpacing: "-.01em",
        lineHeight: "1.2",
      })
      label.append(number, heading)
      document.body.append(label)
      requestAnimationFrame(() => {
        label.style.opacity = "1"
        label.style.transform = "translateY(0)"
      })
    },
    { sceneEyebrow: eyebrow, sceneTitle: title },
  )
  await pause(page, 1_700)
  await page.evaluate(() => {
    const label = document.querySelector<HTMLElement>("[data-demo-scene]")
    if (!label) return
    label.style.opacity = "0"
    label.style.transform = "translateY(8px)"
    window.setTimeout(() => label.remove(), 240)
  })
  await pause(page, 350)
}

async function focusTarget(page: Page, target: Locator) {
  await target.scrollIntoViewIfNeeded()
  await target.evaluate((element) => {
    const node = element as HTMLElement
    node.dataset.demoFocus = "true"
    node.style.transition = "box-shadow 180ms ease"
    node.style.boxShadow =
      "0 0 0 4px rgba(20,184,166,.28), 0 10px 32px rgba(15,23,42,.18)"
  })
  await pause(page, 650)
  await target.click()
  await pause(page, 300)
  await page.evaluate(() => {
    const node = document.querySelector<HTMLElement>("[data-demo-focus]")
    if (!node) return
    node.style.boxShadow = ""
    delete node.dataset.demoFocus
  })
}

async function login(
  page: Page,
  role: "Care staff" | "Clinician" | "Patient" | "Clinic admin",
) {
  await page.goto(`${baseURL}/login`, { waitUntil: "domcontentloaded" })
  const button = page.getByRole("button", { name: `Continue as ${role}` })
  await button.waitFor({ state: "visible" })
  await focusTarget(page, button)
  const destination =
    role === "Patient"
      ? /\/my-care$/
      : role === "Clinic admin"
        ? /\/admin$/
        : /\/patients\/?$/
  await page.waitForURL(destination)
}

async function logout(page: Page) {
  await focusTarget(page, page.getByTestId("user-menu"))
  const item = page.getByRole("menuitem", { name: "Log out and clear data" })
  await item.waitFor({ state: "visible" })
  await focusTarget(page, item)
  await page.waitForURL(/\/login$/)
}

async function addSyntheticRecorder(page: Page) {
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

    class SyntheticMediaRecorder extends EventTarget {
      static isTypeSupported(type: string) {
        return type.includes("webm") || type === "audio/mp4"
      }
      mimeType = "audio/wav"
      state = "inactive"
      timer: number | undefined
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
        }, 50)
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
    class SyntheticAudioContext {
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
    Object.assign(window, {
      AudioContext: SyntheticAudioContext,
      MediaRecorder: SyntheticMediaRecorder,
    })
  })
}

await mkdir(dirname(output), { recursive: true })
await rm(output, { force: true })
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({
  ignoreHTTPSErrors: true,
  recordVideo: { dir: temp, size: { height: 720, width: 1280 } },
  viewport: { height: 720, width: 1280 },
})
const page = await context.newPage()
await addSyntheticRecorder(page)
if (trustedOriginOverride) {
  await page.route("**/api/v1/**", async (route) => {
    const method = route.request().method()
    if (!["DELETE", "PATCH", "POST", "PUT"].includes(method)) {
      await route.continue()
      return
    }
    const response = await route.fetch({
      headers: {
        ...route.request().headers(),
        origin: trustedOriginOverride,
      },
    })
    await route.fulfill({ response })
  })
}
page.on("response", (response) => {
  if (response.status() >= 500)
    failures.push(
      `${response.status()} ${response.request().method()} ${response.url()}`,
    )
})

const video = page.video()
if (!video) throw new Error("Playwright did not create a video recorder")

try {
  await page.goto(`${baseURL}/login`, { waitUntil: "domcontentloaded" })
  await page.getByRole("button", { name: "Continue as Care staff" }).waitFor()
  await scene(page, "Synthetic demo", "Nightingale — evidence before summary")

  await login(page, "Care staff")
  await page.getByRole("heading", { name: "Clinical care notes" }).waitFor()
  await scene(
    page,
    "Staff view",
    "Clinic-scoped care notes, one patient at a time",
  )
  await focusTarget(
    page,
    page.getByRole("link", { name: "Open care note for Alex Synthetic" }),
  )
  await page.getByRole("heading", { name: "Alex Synthetic" }).waitFor()
  await page.getByRole("heading", { name: "What matters now" }).waitFor()

  await scene(page, "Scenario A", "Glance resolves to an immutable source span")
  const riskCard = page
    .getByRole("listitem")
    .filter({ hasText: "Fall risk remains elevated" })
  await focusTarget(page, riskCard.getByRole("button", { name: "View source" }))
  const sourceDialog = page.getByRole("dialog", { name: /Immutable source/ })
  await sourceDialog.waitFor({ state: "visible" })
  await sourceDialog.locator("mark[data-source-span]").waitFor()
  await pause(page, 3_100)
  await page.keyboard.press("Escape")
  await sourceDialog.waitFor({ state: "hidden" })

  await scene(
    page,
    "Immutable history",
    "Compare versions without deleting the record",
  )
  const medication = page
    .locator('article[aria-label="Manual Staff: Medication reconciliation"]')
    .filter({ hasText: "Medication list reviewed" })
  await focusTarget(page, medication.getByRole("button", { name: "Versions" }))
  const history = page.getByRole("dialog", { name: /Version history/ })
  await history.waitFor({ state: "visible" })
  const version1 = history.getByRole("listitem").filter({
    hasText: "Version 1 · Medication reconciliation",
  })
  const version2 = history.getByRole("listitem").filter({
    hasText: "Version 2 · Medication reconciliation",
  })
  await focusTarget(page, version1.getByRole("button", { name: "Diff from" }))
  await focusTarget(page, version2.getByRole("button", { name: "Diff to" }))
  await history.getByText("Unified diff").waitFor()
  await pause(page, 3_000)
  await page.keyboard.press("Escape")
  await history.waitFor({ state: "hidden" })

  await logout(page)
  await login(page, "Clinic admin")
  await page.getByRole("heading", { name: "Clinic administration" }).waitFor()
  await focusTarget(
    page,
    page.getByRole("link", { name: /Care notes · read-only/ }),
  )
  await focusTarget(
    page,
    page.getByRole("link", { name: "Open care note for Alex Synthetic" }),
  )
  await page.getByRole("heading", { name: "Alex Synthetic" }).waitFor()
  await page.getByText("admin · read-only oversight").waitFor()
  await scene(
    page,
    "Admin oversight",
    "Clinic-scoped visibility with clinical writes disabled",
  )
  if (
    (await page.getByRole("button", { name: "Edit" }).count()) !== 0 ||
    (await page.getByRole("link", { name: "Record visit" }).count()) !== 0
  ) {
    throw new Error("Admin recording exposed a clinical mutation control")
  }

  await logout(page)
  await login(page, "Patient")
  await page
    .getByRole("heading", { name: /My Care · Alex Synthetic/ })
    .waitFor()
  await scene(
    page,
    "Scenario E",
    "Patient-safe My Care exposes approved content only",
  )
  const approved = page
    .getByRole("button", { name: "View approved source" })
    .first()
  await focusTarget(page, approved)
  await page.getByText("Approved source", { exact: true }).waitFor()
  await pause(page, 3_000)

  await logout(page)
  await login(page, "Clinician")
  await focusTarget(
    page,
    page.getByRole("link", { name: "Open care note for Alex Synthetic" }),
  )
  await page.getByRole("heading", { name: "Alex Synthetic" }).waitFor()
  await focusTarget(page, page.getByRole("link", { name: "Record visit" }))
  await page.getByTestId("voice-capture").waitFor()
  await scene(
    page,
    "Scenario F",
    "Encrypted capture becomes clinician Review Mode",
  )
  await focusTarget(page, page.getByLabel("Synthetic fixture transcript"))
  await focusTarget(page, page.getByRole("button", { name: "Start recording" }))
  await page.getByText("1/1 chunks acknowledged").waitFor({ timeout: 15_000 })
  await pause(page, 2_100)
  await focusTarget(page, page.getByRole("button", { name: "Stop & finalize" }))
  await page.waitForURL(/\/voice\/.+\/review/, { timeout: 45_000 })
  await page.getByTestId("voice-review-mode").waitFor({ timeout: 30_000 })
  await page
    .getByRole("heading", { name: "Transcript, summary & evidence" })
    .waitFor()
  await page.getByText("Structured facts", { exact: true }).waitFor()
  await page
    .getByTestId("transcript-panel-desktop")
    .getByText("SPEAKER_00")
    .waitFor()
  await scene(
    page,
    "Review Mode",
    "Speaker, timestamp, confidence, overlap and provenance",
  )
  await pause(page, 2_000)
  await focusTarget(page, page.getByLabel("Low confidence / overlap only"))
  await page
    .getByTestId("transcript-panel-desktop")
    .getByText("SPEAKER_01")
    .waitFor()
  await pause(page, 2_600)
  const allergy = page
    .locator('[data-testid="facts-panel"]:visible')
    .getByRole("button", { name: /allergy/i })
  await focusTarget(page, allergy)
  await pause(page, 2_400)
  await scene(
    page,
    "Nightingale",
    "Traceable decisions. Patient-safe delivery.",
  )
  await pause(page, 1_200)
} finally {
  await page.close()
  await context.close()
  await browser.close()
}

const raw = await video.path()
await run("ffmpeg", [
  "-hide_banner",
  "-loglevel",
  "warning",
  "-y",
  "-i",
  raw,
  "-an",
  "-vf",
  "fps=30,scale=1280:720:flags=lanczos",
  "-c:v",
  "libx264",
  "-preset",
  "medium",
  "-crf",
  "23",
  "-pix_fmt",
  "yuv420p",
  "-movflags",
  "+faststart",
  output,
])

const media = await stat(output)
if (media.size > 25 * 1024 * 1024) {
  await rm(output, { force: true })
  throw new Error(
    `Final MP4 is ${(media.size / 1024 / 1024).toFixed(1)} MiB; limit is 25 MiB`,
  )
}
if (failures.length > 0) {
  await rm(output, { force: true })
  throw new Error(
    `Recording observed application errors:\n${failures.join("\n")}`,
  )
}

const probe = JSON.parse(
  await capture("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "stream=codec_type,codec_name,width,height:format=duration",
    "-of",
    "json",
    output,
  ]),
) as {
  format?: { duration?: string }
  streams?: Array<{
    codec_name?: string
    codec_type?: string
    height?: number
    width?: number
  }>
}
const duration = Number(probe.format?.duration)
const videoStream = probe.streams?.find(
  (stream) => stream.codec_type === "video",
)
const audioStreams =
  probe.streams?.filter((stream) => stream.codec_type === "audio") ?? []
if (
  videoStream?.codec_name !== "h264" ||
  videoStream?.width !== 1280 ||
  videoStream?.height !== 720 ||
  audioStreams.length !== 0 ||
  !Number.isFinite(duration) ||
  duration < 30 ||
  duration > 90
) {
  await rm(output, { force: true })
  throw new Error(
    `Unexpected media: ${JSON.stringify({ audioStreams: audioStreams.length, duration, videoStream })}`,
  )
}

await rm(temp, { recursive: true, force: true })
console.log(`Recorded silent synthetic demo: ${output}`)
console.log(`Size: ${(media.size / 1024 / 1024).toFixed(2)} MiB`)
console.log(`Duration: ${duration.toFixed(3)} seconds`)
