#!/usr/bin/env node

import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
	SUBTITLE_LINE_LENGTH,
	SUBTITLE_MAX_LINES,
	segments,
	TARGET_DURATION_SECONDS,
} from "./english_demo_content.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(SCRIPT_DIR, "../..");
const OUTPUT_DIR = resolve(
	process.env.NIGHTINGALE_DEMO_OUTPUT_DIR || join(ROOT, "output", "demo"),
);
const SRT_PATH = join(OUTPUT_DIR, "Nightingale_Final_Demo_EN.srt");
const SCRIPT_PATH = join(OUTPUT_DIR, "Nightingale_Final_Demo_Script.en.md");

const CJK = /[\u3400-\u9fff\uf900-\ufaff]/u;

function wordCount(text) {
	return text.trim().split(/\s+/u).filter(Boolean).length;
}

export function wrapCue(text, maxLength = SUBTITLE_LINE_LENGTH) {
	const words = text.trim().split(/\s+/u).filter(Boolean);
	const lines = [];
	let line = "";

	for (const word of words) {
		if (word.length > maxLength) {
			throw new Error(`Subtitle word exceeds ${maxLength} characters: ${word}`);
		}
		const candidate = line ? `${line} ${word}` : word;
		if (candidate.length <= maxLength) {
			line = candidate;
		} else {
			lines.push(line);
			line = word;
		}
	}
	if (line) lines.push(line);

	if (lines.length === 0 || lines.length > SUBTITLE_MAX_LINES) {
		throw new Error(
			`Subtitle must wrap to 1-${SUBTITLE_MAX_LINES} lines: ${JSON.stringify(text)} -> ${JSON.stringify(lines)}`,
		);
	}
	return lines;
}

export function cueDurationSeconds(text) {
	const duration = Math.max(3, wordCount(text) / 2.8);
	if (duration > 7.5) {
		throw new Error(
			`Subtitle must be split because it needs ${duration.toFixed(3)} seconds: ${text}`,
		);
	}
	return duration;
}

function toMilliseconds(seconds) {
	return Math.round(seconds * 1000);
}

export function formatSrtTime(milliseconds) {
	const hours = Math.floor(milliseconds / 3_600_000);
	const minutes = Math.floor((milliseconds % 3_600_000) / 60_000);
	const seconds = Math.floor((milliseconds % 60_000) / 1000);
	const millis = milliseconds % 1000;
	return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}

function formatChapterTime(seconds) {
	const minutes = Math.floor(seconds / 60);
	const remaining = seconds % 60;
	return `${minutes}:${String(remaining).padStart(2, "0")}`;
}

export function buildCueTimeline() {
	const timeline = [];
	let segmentStart = 0;

	segments.forEach((segment, segmentIndex) => {
		if (CJK.test(`${segment.role}\n${segment.title}\n${segment.caption}`)) {
			throw new Error(`Chapter ${segmentIndex + 1} contains CJK characters`);
		}
		let previousRelativeEnd = 0;
		segment.subtitles.forEach((subtitle, cueIndex) => {
			if (!subtitle.text.trim()) {
				throw new Error(
					`Chapter ${segmentIndex + 1}, cue ${cueIndex + 1} is empty`,
				);
			}
			if (CJK.test(subtitle.text)) {
				throw new Error(
					`Chapter ${segmentIndex + 1}, cue ${cueIndex + 1} contains CJK characters`,
				);
			}
			const lines = wrapCue(subtitle.text);
			const duration = cueDurationSeconds(subtitle.text);
			const relativeEnd = subtitle.at + duration;
			if (subtitle.at < previousRelativeEnd - 0.001) {
				throw new Error(
					`Chapter ${segmentIndex + 1}, cue ${cueIndex + 1} overlaps the previous cue`,
				);
			}
			if (relativeEnd > segment.seconds + 0.001) {
				throw new Error(
					`Chapter ${segmentIndex + 1}, cue ${cueIndex + 1} exceeds its chapter`,
				);
			}
			const startMs = toMilliseconds(segmentStart + subtitle.at);
			const endMs = toMilliseconds(segmentStart + relativeEnd);
			timeline.push({
				index: timeline.length + 1,
				segmentIndex,
				cueIndex,
				startMs,
				endMs,
				duration,
				lines,
				text: subtitle.text,
			});
			previousRelativeEnd = relativeEnd;
		});
		segmentStart += segment.seconds;
	});

	if (
		segmentStart !== TARGET_DURATION_SECONDS ||
		TARGET_DURATION_SECONDS !== 720
	) {
		throw new Error(
			`Expected a 720-second demo, received ${segmentStart} seconds`,
		);
	}
	return timeline;
}

export function buildSrt() {
	return `${buildCueTimeline()
		.map(
			(cue) =>
				`${cue.index}\n${formatSrtTime(cue.startMs)} --> ${formatSrtTime(cue.endMs)}\n${cue.lines.join("\n")}`,
		)
		.join("\n\n")}\n`;
}

function parseSrtTime(value) {
	const match = /^(\d{2}):(\d{2}):(\d{2}),(\d{3})$/u.exec(value);
	if (!match) throw new Error(`Invalid SRT timestamp: ${value}`);
	return (
		Number(match[1]) * 3_600_000 +
		Number(match[2]) * 60_000 +
		Number(match[3]) * 1000 +
		Number(match[4])
	);
}

export function validateSrt(srt) {
	if (CJK.test(srt)) throw new Error("SRT contains CJK characters");
	const blocks = srt.trim().split(/\n{2,}/u);
	if (!blocks.length) throw new Error("SRT has no cues");
	let previousEnd = -1;

	blocks.forEach((block, blockIndex) => {
		const lines = block.split("\n");
		if (Number(lines[0]) !== blockIndex + 1) {
			throw new Error(`SRT cue index mismatch at block ${blockIndex + 1}`);
		}
		const timing = /^(\S+) --> (\S+)$/u.exec(lines[1] || "");
		if (!timing)
			throw new Error(`Invalid SRT timing line at cue ${blockIndex + 1}`);
		const start = parseSrtTime(timing[1]);
		const end = parseSrtTime(timing[2]);
		if (start < previousEnd)
			throw new Error(`SRT overlap at cue ${blockIndex + 1}`);
		if (end <= start)
			throw new Error(`Non-positive SRT duration at cue ${blockIndex + 1}`);
		const duration = (end - start) / 1000;
		if (duration < 2.999 || duration > 7.501) {
			throw new Error(
				`SRT duration outside 3-7.5 seconds at cue ${blockIndex + 1}`,
			);
		}
		const textLines = lines.slice(2);
		if (textLines.length < 1 || textLines.length > SUBTITLE_MAX_LINES) {
			throw new Error(
				`SRT cue ${blockIndex + 1} has ${textLines.length} text lines`,
			);
		}
		textLines.forEach((line) => {
			if (!line.trim())
				throw new Error(`SRT cue ${blockIndex + 1} contains an empty line`);
			if (line.length > SUBTITLE_LINE_LENGTH) {
				throw new Error(
					`SRT cue ${blockIndex + 1} exceeds ${SUBTITLE_LINE_LENGTH} characters`,
				);
			}
		});
		previousEnd = end;
	});

	return { cueCount: blocks.length, finalCueEndMs: previousEnd };
}

export function buildEnglishScript() {
	const sections = [];
	let start = 0;

	segments.forEach((segment, index) => {
		const end = start + segment.seconds;
		const subtitleList = segment.subtitles
			.map(({ at, text }) => `- \`${formatChapterTime(start + at)}\` ${text}`)
			.join("\n");
		sections.push(`### ${String(index + 1).padStart(2, "0")}. ${formatChapterTime(start)}-${formatChapterTime(end)} | ${segment.title}

**Role/page:** ${segment.role}

**On-screen action label:** ${segment.caption}

**English subtitle cues:**

${subtitleList}`);
		start = end;
	});

	return `# Nightingale Final Demo - English Caption Script

- Planned duration: **${formatChapterTime(TARGET_DURATION_SECONDS)}** (${TARGET_DURATION_SECONDS} seconds)
- Language: English
- Narration: none
- Subtitles: burned in plus sidecar SRT
- Browser canvas: 1920x960 inside a 1920x1080 frame
- Subtitle safe area: bottom 120 pixels
- Data boundary: synthetic patients, recordings, and evaluation data only
- Runtime revision and image digest: recorded in final capture metadata

## Chapters and subtitle cues

${sections.join("\n\n")}

## Subtitle acceptance rules

- Every cue lasts \`max(3 seconds, word count / 2.8)\`, capped at 7.5 seconds.
- Cues are chronological and non-overlapping.
- Each cue contains one or two lines, with at most ${SUBTITLE_LINE_LENGTH} characters per line.
- Chapter titles, action labels, cursor labels, and subtitles contain no CJK characters.
- The sidecar SRT is the exact source used for burned-in captions.
`;
}

async function main() {
	const checkOnly = process.argv.includes("--check");
	if (!checkOnly) {
		await mkdir(OUTPUT_DIR, { recursive: true });
		await writeFile(SRT_PATH, buildSrt(), "utf8");
		await writeFile(SCRIPT_PATH, buildEnglishScript(), "utf8");
	}

	const [srt, script] = await Promise.all([
		readFile(SRT_PATH, "utf8"),
		readFile(SCRIPT_PATH, "utf8"),
	]);
	const validation = validateSrt(srt);
	if (CJK.test(script))
		throw new Error("English script contains CJK characters");
	if (script.includes("[object Object]"))
		throw new Error("English script contains invalid output");

	process.stdout.write(
		`${JSON.stringify(
			{
				status: "passed",
				duration_seconds: TARGET_DURATION_SECONDS,
				chapter_count: segments.length,
				subtitle_cue_count: validation.cueCount,
				final_cue_end_seconds: validation.finalCueEndMs / 1000,
				max_lines_per_cue: SUBTITLE_MAX_LINES,
				max_characters_per_line: SUBTITLE_LINE_LENGTH,
				srt: SRT_PATH,
				script: SCRIPT_PATH,
			},
			null,
			2,
		)}\n`,
	);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
	await main();
}
