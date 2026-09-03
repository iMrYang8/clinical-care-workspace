#!/usr/bin/env node
/**
 * Build one SRT per scenario recording from `scenario_narration.mjs`.
 *
 * The seven scenario clips were recorded silent and without captions. This
 * turns the narration cues into a sidecar SRT beside each clip, using the same
 * cue rules as the twelve-minute demo (`generate_english_demo_assets.mjs`), so
 * `render_samantha_voiceover.py` can speak them without a second script.
 *
 *   --check   validate against the real recordings, write nothing.
 *   (default) validate and write output/demo/scenarios/voiced/*.srt
 *
 * Validation is against the file on disk, not the declared length: a clip that
 * was re-recorded to a different duration fails here instead of producing
 * narration that runs off the end of the picture.
 */

import { execFile } from "node:child_process"
import { existsSync } from "node:fs"
import { mkdir, writeFile } from "node:fs/promises"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"
import { promisify } from "node:util"

import {
  cueDurationSeconds,
  formatSrtTime,
  validateSrt,
  wrapCue,
} from "./generate_english_demo_assets.mjs"
import { narrations } from "./scenario_narration.mjs"

const execFileAsync = promisify(execFile)

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = resolve(HERE, "..", "..")
const VIDEO_DIR = resolve(
  process.env.NIGHTINGALE_SCENARIO_OUTPUT_DIR ||
    join(ROOT, "output", "demo", "scenarios"),
)
const OUT_DIR = resolve(
  process.env.NIGHTINGALE_SCENARIO_VOICE_DIR || join(VIDEO_DIR, "voiced"),
)
const CHECK_ONLY = process.argv.includes("--check")

/** Narration must end before the picture does, with room for the tail. */
const TAIL_MARGIN_SECONDS = 0.1
/** A re-recorded clip of a different length needs its cues re-timed. */
const DURATION_TOLERANCE_SECONDS = 0.25

const CJK = /[\u3400-\u9fff\uf900-\ufaff]/u

async function videoDuration(path) {
  const { stdout } = await execFileAsync("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "csv=p=0",
    path,
  ])
  const duration = Number(stdout.trim())
  if (!Number.isFinite(duration) || duration <= 0) {
    throw new Error(`ffprobe reported no usable duration for ${path}`)
  }
  return duration
}

function buildScenarioSrt(narration, duration) {
  const blocks = []
  let previousEnd = 0

  narration.cues.forEach((cue, index) => {
    const where = `${narration.id} cue ${index + 1}`
    if (!cue.text.trim()) throw new Error(`${where} is empty`)
    if (CJK.test(cue.text)) throw new Error(`${where} contains CJK characters`)

    const lines = wrapCue(cue.text)
    const end = cue.at + cueDurationSeconds(cue.text)
    if (cue.at < previousEnd - 0.001) {
      throw new Error(
        `${where} starts at ${cue.at}s but the previous cue runs to ${previousEnd.toFixed(3)}s`,
      )
    }
    if (end > duration - TAIL_MARGIN_SECONDS) {
      throw new Error(
        `${where} ends at ${end.toFixed(3)}s, past the ${duration.toFixed(3)}s recording`,
      )
    }

    blocks.push(
      `${index + 1}\n${formatSrtTime(Math.round(cue.at * 1000))} --> ${formatSrtTime(
        Math.round(end * 1000),
      )}\n${lines.join("\n")}`,
    )
    previousEnd = end
  })

  if (blocks.length === 0) throw new Error(`${narration.id} has no cues`)
  return `${blocks.join("\n\n")}\n`
}

async function main() {
  if (!CHECK_ONLY) await mkdir(OUT_DIR, { recursive: true })
  const report = []

  for (const narration of narrations) {
    const video = join(VIDEO_DIR, `Nightingale_Scenario_${narration.id}.mp4`)
    if (!existsSync(video)) {
      throw new Error(`Missing scenario recording: ${video}`)
    }
    const duration = await videoDuration(video)
    if (
      Math.abs(duration - narration.videoSeconds) > DURATION_TOLERANCE_SECONDS
    ) {
      throw new Error(
        `${narration.id} was written against ${narration.videoSeconds}s but the recording is ${duration.toFixed(3)}s; re-time its cues`,
      )
    }

    const srt = buildScenarioSrt(narration, duration)
    const validation = validateSrt(srt)
    const srtPath = join(OUT_DIR, `Nightingale_Scenario_${narration.id}.srt`)
    if (!CHECK_ONLY) await writeFile(srtPath, srt, "utf8")

    report.push({
      id: narration.id,
      video_seconds: Number(duration.toFixed(3)),
      cue_count: validation.cueCount,
      final_cue_end_seconds: validation.finalCueEndMs / 1000,
      spoken_words: narration.cues.reduce(
        (total, cue) => total + cue.text.trim().split(/\s+/u).length,
        0,
      ),
      srt: srtPath,
    })
  }

  process.stdout.write(
    `${JSON.stringify(
      { status: "passed", mode: CHECK_ONLY ? "check" : "write", scenarios: report },
      null,
      2,
    )}\n`,
  )
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main()
}
