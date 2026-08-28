import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import {
	mkdir,
	mkdtemp,
	readFile,
	rm,
	stat,
	writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { segments, TARGET_DURATION_SECONDS } from "./english_demo_content.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(SCRIPT_DIR, "../..");
const RAW = process.env.NIGHTINGALE_RAW_VIDEO;
const RECORDING_MANIFEST = resolve(
	process.env.NIGHTINGALE_DEMO_RECORDING_MANIFEST ??
		`${process.env.NIGHTINGALE_RAW_VIDEO ?? ""}.recording.json`,
);
const OUTPUT = resolve(
	process.env.NIGHTINGALE_FINAL_VIDEO ??
		join(ROOT, "output/demo/Nightingale_Final_Demo_EN.mp4"),
);
const SRT = resolve(
	process.env.NIGHTINGALE_DEMO_SRT ??
		join(ROOT, "output/demo/Nightingale_Final_Demo_EN.srt"),
);
const SCRIPT = resolve(
	process.env.NIGHTINGALE_DEMO_SCRIPT ??
		join(ROOT, "output/demo/Nightingale_Final_Demo_Script.en.md"),
);
const METADATA = resolve(
	process.env.NIGHTINGALE_DEMO_METADATA ??
		join(ROOT, "output/demo/Nightingale_Final_Demo_EN_metadata.json"),
);
const CONTACT_SHEET = resolve(
	process.env.NIGHTINGALE_DEMO_CONTACT_SHEET ??
		join(ROOT, "output/demo/Nightingale_Final_Demo_EN_contact_sheet.png"),
);
const SHA_FILE = resolve(
	process.env.NIGHTINGALE_DEMO_SHA256 ??
		join(ROOT, "output/demo/Nightingale_Final_Demo_EN_SHA256.txt"),
);
const PRE_ROLL_SECONDS = Number(process.env.NIGHTINGALE_PRE_ROLL ?? "0");
const IMAGE_DIGEST = process.env.NIGHTINGALE_IMAGE_DIGEST ?? "unknown";
const RECORDING_PROJECT =
	process.env.NIGHTINGALE_RECORDING_PROJECT ?? "unknown";

if (!RAW) throw new Error("NIGHTINGALE_RAW_VIDEO is required");
if (!Number.isFinite(PRE_ROLL_SECONDS) || PRE_ROLL_SECONDS < 0) {
	throw new Error("NIGHTINGALE_PRE_ROLL must be a non-negative number");
}

function run(command, args, options = {}) {
	return new Promise((resolveRun, rejectRun) => {
		const child = spawn(command, args, {
			cwd: options.cwd ?? ROOT,
			stdio: options.capture ? ["ignore", "pipe", "pipe"] : "inherit",
		});
		let stdout = "";
		let stderr = "";
		if (options.capture) {
			child.stdout.on("data", (chunk) => (stdout += String(chunk)));
			child.stderr.on("data", (chunk) => (stderr += String(chunk)));
		}
		child.once("error", rejectRun);
		child.once("exit", (code) => {
			if (code === 0) resolveRun({ stdout, stderr });
			else rejectRun(new Error(`${command} exited ${code}: ${stderr}`));
		});
	});
}

async function sha256(path) {
	const bytes = await readFile(path);
	return createHash("sha256").update(bytes).digest("hex");
}

function relativeForFilter(path) {
	const relative = path.startsWith(`${ROOT}/`)
		? path.slice(ROOT.length + 1)
		: path;
	return relative
		.replaceAll("\\", "\\\\")
		.replaceAll(":", "\\:")
		.replaceAll("'", "\\'");
}

async function validateEnglishText() {
	const cjk = /[\u3400-\u9fff\uf900-\ufaff]/u;
	const bodies = [
		await readFile(SRT, "utf8"),
		await readFile(SCRIPT, "utf8"),
		JSON.stringify(segments),
	];
	if (bodies.some((body) => cjk.test(body))) {
		throw new Error(
			"English demo title, labels, script, or subtitles contain CJK characters",
		);
	}
}

await mkdir(dirname(OUTPUT), { recursive: true });
await validateEnglishText();
await rm(OUTPUT, { force: true });

const revision = (
	await run("git", ["rev-parse", "HEAD"], { capture: true })
).stdout.trim();
const recordingManifest = JSON.parse(
	await readFile(RECORDING_MANIFEST, "utf8"),
);
if (recordingManifest.runtime_git_revision !== revision) {
	throw new Error(
		`Recording revision ${recordingManifest.runtime_git_revision} does not match renderer revision ${revision}`,
	);
}
if (
	recordingManifest.image_digest !== IMAGE_DIGEST ||
	!IMAGE_DIGEST.startsWith("sha256:")
) {
	throw new Error(
		`Recording image ${recordingManifest.image_digest} does not match renderer image ${IMAGE_DIGEST}`,
	);
}
if (
	recordingManifest.language !== "en" ||
	recordingManifest.mutate !== true ||
	recordingManifest.speed !== 1 ||
	recordingManifest.resolution !== "1920x960" ||
	Number(recordingManifest.expected_segment_duration_seconds) !==
		TARGET_DURATION_SECONDS ||
	!Array.isArray(recordingManifest.failures) ||
	recordingManifest.failures.length > 0 ||
	resolve(recordingManifest.raw_video) !== resolve(RAW)
) {
	throw new Error(
		`Recording manifest failed the final capture contract: ${JSON.stringify(recordingManifest)}`,
	);
}
const recordingManifestHash = await sha256(RECORDING_MANIFEST);

const subtitleFile = relativeForFilter(SRT);
const videoFilter = [
	"fps=30",
	"scale=1920:960:flags=lanczos",
	"pad=1920:1080:0:0:color=0x071c1f",
	`subtitles=filename='${subtitleFile}':force_style='FontName=Arial,FontSize=24,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BorderStyle=3,BackColour=&H9A071C1F,Outline=0,Shadow=0,Alignment=2,MarginL=150,MarginR=150,MarginV=20'`,
].join(",");

await run("ffmpeg", [
	"-hide_banner",
	"-loglevel",
	"warning",
	"-y",
	"-ss",
	PRE_ROLL_SECONDS.toFixed(3),
	"-i",
	resolve(RAW),
	"-t",
	String(TARGET_DURATION_SECONDS),
	"-map",
	"0:v:0",
	"-an",
	"-vf",
	videoFilter,
	"-c:v",
	"libx264",
	"-preset",
	process.env.NIGHTINGALE_FFMPEG_PRESET ?? "medium",
	"-crf",
	process.env.NIGHTINGALE_FFMPEG_CRF ?? "22",
	"-pix_fmt",
	"yuv420p",
	"-movflags",
	"+faststart",
	OUTPUT,
]);

const probeResult = await run(
	"ffprobe",
	[
		"-v",
		"error",
		"-show_entries",
		"format=duration,size:stream=index,codec_type,codec_name,width,height,avg_frame_rate",
		"-of",
		"json",
		OUTPUT,
	],
	{ capture: true },
);
const probe = JSON.parse(probeResult.stdout);
const videoStreams = probe.streams.filter(
	(stream) => stream.codec_type === "video",
);
const audioStreams = probe.streams.filter(
	(stream) => stream.codec_type === "audio",
);
const duration = Number(probe.format.duration);
const videoStream = videoStreams[0];
if (
	videoStreams.length !== 1 ||
	audioStreams.length !== 0 ||
	videoStream.codec_name !== "h264" ||
	videoStream.width !== 1920 ||
	videoStream.height !== 1080 ||
	videoStream.avg_frame_rate !== "30/1" ||
	Math.abs(duration - TARGET_DURATION_SECONDS) > 0.12
) {
	throw new Error(`Unexpected final media probe: ${JSON.stringify(probe)}`);
}

const blackdetectResult = await run(
	"ffmpeg",
	[
		"-hide_banner",
		"-nostats",
		"-i",
		OUTPUT,
		"-vf",
		"blackdetect=d=1.5:pic_th=0.98",
		"-an",
		"-f",
		"null",
		"-",
	],
	{ capture: true },
);
const blackFrames = blackdetectResult.stderr
	.split("\n")
	.filter((line) => line.includes("black_duration:"));
if (blackFrames.length > 0) {
	throw new Error(
		`Detected a black frame interval of at least 1.5 seconds: ${blackFrames.join(" | ")}`,
	);
}

const work = await mkdtemp(join(tmpdir(), "nightingale-demo-contact-"));
let cursor = 0;
const frameFiles = [];
const chapterTimeline = [];
for (let index = 0; index < segments.length; index += 1) {
	const segment = segments[index];
	const start = cursor;
	const end = start + segment.seconds;
	const midpoint =
		start + Math.min(segment.seconds - 1, Math.max(3, segment.seconds * 0.52));
	const frame = join(work, `chapter-${String(index + 1).padStart(2, "0")}.png`);
	await run("ffmpeg", [
		"-hide_banner",
		"-loglevel",
		"error",
		"-y",
		"-ss",
		midpoint.toFixed(3),
		"-i",
		OUTPUT,
		"-frames:v",
		"1",
		"-vf",
		"scale=480:270:flags=lanczos",
		frame,
	]);
	frameFiles.push(frame);
	chapterTimeline.push({
		chapter: index + 1,
		role: segment.role,
		title: segment.title,
		start_seconds: start,
		end_seconds: end,
		contact_sheet_frame_seconds: midpoint,
	});
	cursor = end;
}
await run("magick", [
	"montage",
	...frameFiles,
	"-tile",
	"4x",
	"-geometry",
	"480x270+4+4",
	"-background",
	"#071c1f",
	CONTACT_SHEET,
]);

const outputHash = await sha256(OUTPUT);
const srtHash = await sha256(SRT);
const outputStat = await stat(OUTPUT);
const metadata = {
	language: "en",
	narration: false,
	subtitles: "burned-in+sidecar",
	duration_seconds: duration,
	target_duration_seconds: TARGET_DURATION_SECONDS,
	runtime_git_revision: revision,
	oci_image_digest: IMAGE_DIGEST,
	recording_compose_project: RECORDING_PROJECT,
	recording_manifest_sha256: recordingManifestHash,
	recorded_at: new Date().toISOString(),
	browser_canvas: "1920x960",
	output_resolution: "1920x1080",
	subtitle_band_height_px: 120,
	video_codec: videoStream.codec_name,
	frame_rate: videoStream.avg_frame_rate,
	audio_streams: audioStreams.length,
	size_bytes: outputStat.size,
	video_sha256: outputHash,
	srt_sha256: srtHash,
	chapters: chapterTimeline,
	qa: {
		ffprobe: "passed",
		blackdetect_1_5_seconds: "passed",
		srt_validation: "passed by generate_english_demo_assets.mjs",
		cjk_in_titles_labels_or_subtitles: false,
		chapter_contact_frames: frameFiles.length,
		synthetic_data_only: true,
		visible_teal_cursor: true,
		full_page_visible: true,
	},
};
await writeFile(METADATA, `${JSON.stringify(metadata, null, 2)}\n`);

const shaLines = [];
for (const file of [OUTPUT, SRT, SCRIPT, CONTACT_SHEET, METADATA]) {
	shaLines.push(`${await sha256(file)}  ${basename(file)}`);
}
await writeFile(SHA_FILE, `${shaLines.join("\n")}\n`);
await rm(work, { recursive: true, force: true });

console.log(`FINAL_VIDEO=${OUTPUT}`);
console.log(`FINAL_SRT=${SRT}`);
console.log(`FINAL_METADATA=${METADATA}`);
console.log(`FINAL_CONTACT_SHEET=${CONTACT_SHEET}`);
console.log(`FINAL_SHA256=${SHA_FILE}`);
