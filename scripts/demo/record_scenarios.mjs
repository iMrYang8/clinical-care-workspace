#!/usr/bin/env node
/**
 * Per-scenario demo recorder.
 *
 * Two phases share one definition of every scenario, so a green check is
 * evidence about exactly the footage that ships:
 *
 *   --check   run every scenario's steps with video off and holds skipped,
 *             asserting that each declared proof string is on screen.
 *   (default) run the same steps with the demo cursor, chapter card, and
 *             per-scenario video capture, then transcode to mp4.
 *
 * Each scenario gets its own BrowserContext with its own recordVideo dir, so
 * one scenario is one video by construction. Videos are silent by design.
 */

import { execFile, spawn } from "node:child_process"
import { mkdir, mkdtemp, readdir, rm, writeFile } from "node:fs/promises"
import { existsSync } from "node:fs"
import { tmpdir } from "node:os"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { chromium } from "playwright"

import {
  VIEWPORT,
  ensureDemoChrome,
  resetChapter,
  stageChapter,
  setSpeed,
  sleep,
} from "./scenario_chrome.mjs"
import { scenarios } from "./scenario_definitions.mjs"

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, "..", "..")
const BASE = (process.env.BASE_URL || "https://localhost").replace(/\/$/, "")
const OUT_DIR = resolve(
  process.env.NIGHTINGALE_SCENARIO_OUTPUT_DIR ||
    join(ROOT, "output", "demo", "scenarios"),
)
const CHECK_ONLY = process.argv.includes("--check")
const ONLY = (() => {
  const flag = process.argv.find((arg) => arg.startsWith("--only="))
  return flag ? flag.slice("--only=".length).split(",").filter(Boolean) : null
})()

const selected = ONLY
  ? scenarios.filter((item) => ONLY.includes(item.id) || ONLY.includes(String(item.number)))
  : scenarios

if (selected.length === 0) {
  console.error("No scenarios selected.")
  process.exit(1)
}

function run(command, args, options = {}) {
  return new Promise((done, fail) => {
    const child = spawn(command, args, { stdio: "inherit", ...options })
    child.on("error", fail)
    child.on("close", (code) =>
      code === 0 ? done() : fail(new Error(`${command} exited ${code}`)),
    )
  })
}

const DB_CONTAINER =
  process.env.NIGHTINGALE_DEMO_DB_CONTAINER ||
  "nightingale-demo-531ae7752655-db-1"

/** Apply a scenario fixture that no product surface can create. */
function psql(sql) {
  return new Promise((done, fail) => {
    execFile(
      "docker",
      ["exec", DB_CONTAINER, "psql", "-U", "postgres", "-d", "app", "-c", sql],
      (error, stdout, stderr) =>
        error ? fail(new Error(stderr || error.message)) : done(stdout),
    )
  })
}

function capture(command, args) {
  return new Promise((done, fail) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] })
    let out = ""
    let err = ""
    child.stdout.on("data", (chunk) => (out += chunk))
    child.stderr.on("data", (chunk) => (err += chunk))
    child.on("error", fail)
    child.on("close", (code) =>
      code === 0 ? done(out) : fail(new Error(err || `${command} exited ${code}`)),
    )
  })
}

/** Transcode Playwright's webm to a silent, evenly-paced h264 mp4. */
async function toMp4(webm, mp4) {
  await run("ffmpeg", [
    "-hide_banner",
    "-loglevel",
    "warning",
    "-y",
    "-i",
    webm,
    "-an",
    "-vf",
    `fps=30,scale=${VIEWPORT.width}:${VIEWPORT.height}:flags=lanczos`,
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
    mp4,
  ])
  const probe = JSON.parse(
    await capture("ffprobe", [
      "-hide_banner",
      "-loglevel",
      "error",
      "-show_entries",
      "stream=codec_type,codec_name,width,height:format=duration",
      "-of",
      "json",
      mp4,
    ]),
  )
  const video = probe.streams?.find((s) => s.codec_type === "video")
  const audio = probe.streams?.filter((s) => s.codec_type === "audio") ?? []
  const duration = Number(probe.format?.duration ?? 0)
  if (
    video?.codec_name !== "h264" ||
    Number(video?.width) !== VIEWPORT.width ||
    Number(video?.height) !== VIEWPORT.height
  ) {
    throw new Error(`Unexpected video stream in ${mp4}: ${JSON.stringify(video)}`)
  }
  if (audio.length !== 0) {
    throw new Error(`${mp4} must be silent but has ${audio.length} audio stream(s)`)
  }
  if (!(duration > 4)) {
    throw new Error(`${mp4} is implausibly short (${duration}s)`)
  }
  return duration
}

async function writeScenarioScript(scenario, { duration, proofs }) {
  const lines = [
    `# Scenario ${scenario.number} — ${scenario.title}`,
    "",
    `**Video:** \`Nightingale_Scenario_${scenario.id}.mp4\`` +
      (duration ? ` · ${duration.toFixed(1)}s · silent, 1440x900` : ""),
    "",
    `**Who is on screen:** ${scenario.role}`,
    "",
    "## What this shows",
    "",
    scenario.summary,
    "",
    "## On-screen evidence asserted by the automated check",
    "",
    ...proofs.map((proof) => `- \`${proof}\``),
    "",
    "## Notes",
    "",
    "The recording is silent by design. Every string listed above is asserted",
    "by `scripts/demo/record_scenarios.mjs --check` before any video is kept,",
    "so the footage cannot drift from the claim.",
    "",
  ]
  await writeFile(
    join(OUT_DIR, `Nightingale_Scenario_${scenario.id}.md`),
    lines.join("\n"),
    "utf8",
  )
}

async function main() {
  if (!CHECK_ONLY) await mkdir(OUT_DIR, { recursive: true })
  // The check runs fast but not instantly: a zero wait lets a save POST race the
  // next navigation, which would silently drop the very state under test.
  setSpeed(CHECK_ONLY ? 0.2 : 1)

  const browser = await chromium.launch({ headless: true })
  const results = []
  let failed = 0

  for (const [index, scenario] of selected.entries()) {
    const label = `${scenario.number}. ${scenario.title}`
    process.stdout.write(
      `\n[${index + 1}/${selected.length}] ${CHECK_ONLY ? "CHECK" : "RECORD"} ${label}\n`,
    )
    const videoDir = CHECK_ONLY
      ? null
      : await mkdtemp(join(tmpdir(), `ng-scenario-${scenario.id}-`))
    const extraContexts = []
    const serverErrors = []
    const foundProofs = []

    const context = await browser.newContext({
      ignoreHTTPSErrors: true,
      viewport: VIEWPORT,
      colorScheme: "light",
      ...(videoDir ? { recordVideo: { dir: videoDir, size: VIEWPORT } } : {}),
    })
    const page = await context.newPage()
    page.on("response", (response) => {
      if (response.status() >= 500) {
        serverErrors.push(`${response.status()} ${response.url()}`)
      }
    })

    const ctx = {
      page,
      context,
      base: BASE,
      async beat(seconds = 1) {
        await sleep(seconds * 1000)
      },
      async expectText(target, text) {
        const locator =
          typeof target.getByText === "function"
            ? target.getByText(text, { exact: false }).first()
            : target
        await locator.waitFor({ state: "visible", timeout: 20000 })
        foundProofs.push(text)
        if (!CHECK_ONLY) {
          await ensureDemoChrome(page).catch(() => {})
        }
      },
      async openSecondContext() {
        const second = await browser.newContext({
          ignoreHTTPSErrors: true,
          viewport: VIEWPORT,
          colorScheme: "light",
        })
        const secondPage = await second.newPage()
        extraContexts.push(second)
        return { context: second, page: secondPage }
      },
    }

    let error = null
    try {
      if (scenario.setup?.sql) await psql(scenario.setup.sql)
      if (!CHECK_ONLY) {
        stageChapter({ role: scenario.role, title: scenario.title })
      }
      await scenario.steps(ctx)

      const missing = scenario.proofs.filter(
        (proof) => !foundProofs.some((seen) => seen === proof),
      )
      if (missing.length > 0) {
        throw new Error(
          `Declared proof never asserted on screen: ${missing.join(", ")}`,
        )
      }
      if (serverErrors.length > 0) {
        throw new Error(`Server errors during take: ${serverErrors.join("; ")}`)
      }
    } catch (caught) {
      error = caught
      failed += 1
      console.error(`  FAILED: ${caught.message}`)
      if (!CHECK_ONLY) {
        await page
          .screenshot({
            path: join(OUT_DIR, `FAILED_${scenario.id}.png`),
            fullPage: false,
          })
          .catch(() => {})
      }
    } finally {
      if (scenario.setup?.teardown) {
        await psql(scenario.setup.teardown).catch(() => {})
      }
      resetChapter()
      for (const extra of extraContexts) await extra.close().catch(() => {})
      await context.close().catch(() => {})
    }

    if (!CHECK_ONLY && videoDir) {
      if (error) {
        await rm(videoDir, { recursive: true, force: true }).catch(() => {})
      } else {
        const files = (await readdir(videoDir)).filter((f) => f.endsWith(".webm"))
        if (files.length === 0) {
          console.error("  FAILED: playwright produced no video")
          failed += 1
        } else {
          const mp4 = join(OUT_DIR, `Nightingale_Scenario_${scenario.id}.mp4`)
          const duration = await toMp4(join(videoDir, files[0]), mp4)
          await writeScenarioScript(scenario, {
            duration,
            proofs: scenario.proofs,
          })
          console.log(`  OK  ${mp4} (${duration.toFixed(1)}s)`)
          results.push({ id: scenario.id, duration })
        }
        await rm(videoDir, { recursive: true, force: true }).catch(() => {})
      }
    } else if (!error) {
      console.log("  OK  all declared proofs visible")
      results.push({ id: scenario.id })
    }
  }

  await browser.close()

  console.log(
    `\n${CHECK_ONLY ? "Check" : "Recording"} complete: ${results.length} passed, ${failed} failed.`,
  )
  if (failed > 0) process.exit(1)
}

if (!existsSync(join(ROOT, "package.json"))) {
  console.error("Run from the Nightingale repository root.")
  process.exit(1)
}

await main()
