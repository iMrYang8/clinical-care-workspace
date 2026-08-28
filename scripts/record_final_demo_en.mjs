/**
 * Record the English Nightingale release demo as a silent 1920x960 WebM.
 *
 * Required for the final isolated run:
 *   BASE_URL=https://localhost:PORT DEMO_MUTATE=1 STRICT_DEMO=1
 *   NIGHTINGALE_RUNTIME_REVISION=GIT_SHA NIGHTINGALE_IMAGE_DIGEST=OCI_DIGEST
 *   node scripts/record_final_demo_en.mjs
 *
 * The raw recording has no bottom caption overlay. The renderer places it
 * above a dedicated 120px subtitle band and burns the generated English SRT.
 */
import { createHash } from "node:crypto";
import {
	copyFile,
	mkdir,
	mkdtemp,
	rm,
	stat,
	writeFile,
} from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
	segments,
	TARGET_DURATION_SECONDS,
} from "./demo/english_demo_content.mjs";

const require = createRequire(import.meta.url);
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const { chromium } = require(join(ROOT, "node_modules", "playwright"));

const BASE = (
	process.env.BASE_URL ||
	process.env.PLAYWRIGHT_BASE_URL ||
	"https://localhost"
).replace(/\/+$/, "");
const OUTPUT = resolve(
	process.env.NIGHTINGALE_DEMO_RAW_VIDEO ||
		process.env.NIGHTINGALE_FINAL_VIDEO ||
		join(ROOT, "output", "demo", "Nightingale_Final_Demo_EN_raw.webm"),
);
const RECORDING_MANIFEST = resolve(
	process.env.NIGHTINGALE_DEMO_RECORDING_MANIFEST || `${OUTPUT}.recording.json`,
);
const SPEED = Number(process.env.DEMO_SPEED || "1");
const MUTATE = process.env.DEMO_MUTATE === "1";
const TRUSTED_ORIGIN_OVERRIDE =
	process.env.NIGHTINGALE_DEMO_TRUSTED_ORIGIN?.trim() || null;
const SEGMENT_OVERRUN_TOLERANCE_MS = Number(
	process.env.NIGHTINGALE_DEMO_SEGMENT_OVERRUN_TOLERANCE_MS || "2000",
);
const RUNTIME_REVISION =
	process.env.NIGHTINGALE_RUNTIME_REVISION || "recorded in final metadata";
const IMAGE_DIGEST =
	process.env.NIGHTINGALE_IMAGE_DIGEST || "recorded in final metadata";
const TRUSTED_ORIGIN = TRUSTED_ORIGIN_OVERRIDE || new URL(BASE).origin;
const VIEWPORT = { width: 1920, height: 960 };
if (!Number.isFinite(SPEED) || SPEED <= 0) {
	throw new Error("DEMO_SPEED must be a positive number");
}
if (
	!Number.isFinite(SEGMENT_OVERRUN_TOLERANCE_MS) ||
	SEGMENT_OVERRUN_TOLERANCE_MS < 0
) {
	throw new Error(
		"NIGHTINGALE_DEMO_SEGMENT_OVERRUN_TOLERANCE_MS must be a non-negative number",
	);
}
if (!OUTPUT.toLowerCase().endsWith(".webm")) {
	throw new Error("NIGHTINGALE_DEMO_RAW_VIDEO must use a .webm suffix");
}
const temp = await mkdtemp(join(tmpdir(), "nightingale-final-demo-en-"));
const rawDir = join(temp, "raw");
const failures = [];
const mutationResponseFailures = [];
const segmentTimings = [];
let activeSegment = null;
let activeSegmentIndex = -1;
const MUTATION_METHODS = new Set(["DELETE", "PATCH", "POST", "PUT"]);

const sleep = (ms) =>
	new Promise((resolveSleep) =>
		setTimeout(resolveSleep, Math.max(1, ms * SPEED)),
	);

async function ensureDemoChrome(page) {
	await page.evaluate(() => {
		if (!document.querySelector("#nightingale-demo-cursor")) {
			const cursor = document.createElement("div");
			cursor.id = "nightingale-demo-cursor";
			cursor.innerHTML = `<svg width="34" height="42" viewBox="0 0 34 42" aria-hidden="true"><path d="M3 2 L3 31 L11 24 L17 39 L23 36 L17 22 L29 22 Z" fill="#0f766e" stroke="white" stroke-width="2.5" stroke-linejoin="round"/></svg><span></span>`;
			Object.assign(cursor.style, {
				left: "80px",
				top: "80px",
				position: "fixed",
				zIndex: "2147483647",
				pointerEvents: "none",
				filter: "drop-shadow(0 5px 7px rgba(0,0,0,.35))",
				transition:
					"left 620ms cubic-bezier(.2,.8,.2,1), top 620ms cubic-bezier(.2,.8,.2,1)",
			});
			const label = cursor.querySelector("span");
			const style = document.createElement("style");
			style.id = "nightingale-demo-cursor-style";
			style.textContent =
				"#nightingale-demo-cursor span::after{content:attr(data-label)}";
			document.head.append(style);
			Object.assign(label.style, {
				position: "absolute",
				left: "28px",
				top: "24px",
				whiteSpace: "nowrap",
				background: "rgba(8,47,73,.94)",
				color: "white",
				borderRadius: "8px",
				padding: "5px 9px",
				font: "600 13px Inter,system-ui,sans-serif",
				opacity: "0",
				transition: "opacity 180ms ease",
				boxShadow: "0 8px 20px rgba(2,6,23,.2)",
			});
			document.body.append(cursor);
		}
		if (!document.querySelector("#nightingale-demo-chapter")) {
			const chapter = document.createElement("div");
			chapter.id = "nightingale-demo-chapter";
			chapter.innerHTML = `<div data-role></div><strong data-title></strong>`;
			Object.assign(chapter.style, {
				position: "fixed",
				right: "28px",
				top: "26px",
				zIndex: "2147483646",
				pointerEvents: "none",
				color: "white",
				background:
					"linear-gradient(120deg,rgba(8,47,73,.97),rgba(15,118,110,.94))",
				border: "1px solid rgba(255,255,255,.25)",
				borderRadius: "14px",
				padding: "11px 16px",
				maxWidth: "520px",
				boxShadow: "0 14px 42px rgba(2,6,23,.25)",
				fontFamily: "Inter,system-ui,sans-serif",
			});
			Object.assign(chapter.querySelector("[data-role]").style, {
				color: "#99f6e4",
				fontSize: "11px",
				fontWeight: "800",
				letterSpacing: ".14em",
				textTransform: "uppercase",
			});
			Object.assign(chapter.querySelector("strong").style, {
				display: "block",
				marginTop: "3px",
				fontSize: "17px",
				lineHeight: "1.25",
			});
			document.body.append(chapter);
		}
	});
	if (activeSegment && activeSegmentIndex >= 0) {
		await page.evaluate(
			({ segment, index, total }) => {
				const chapter = document.querySelector("#nightingale-demo-chapter");
				chapter.querySelector("[data-role]").textContent =
					`${String(index + 1).padStart(2, "0")}/${String(total).padStart(2, "0")} · ${segment.role}`;
				chapter.querySelector("[data-title]").textContent = segment.title;
			},
			{
				segment: activeSegment,
				index: activeSegmentIndex,
				total: segments.length,
			},
		);
	}
}

async function setScene(page, segment, index) {
	activeSegment = segment;
	activeSegmentIndex = index;
	await ensureDemoChrome(page);
	await page.evaluate(
		({ segment, index, total }) => {
			const chapter = document.querySelector("#nightingale-demo-chapter");
			chapter.querySelector("[data-role]").textContent =
				`${String(index + 1).padStart(2, "0")}/${String(total).padStart(2, "0")} · ${segment.role}`;
			chapter.querySelector("[data-title]").textContent = segment.title;
		},
		{ segment, index, total: segments.length },
	);
}

async function moveTo(page, locator, label = "", options = {}) {
	const timeout = options.timeout ?? 8000;
	await locator.waitFor({ state: "visible", timeout });
	await locator.scrollIntoViewIfNeeded();
	await sleep(250);
	const box = await locator.boundingBox();
	if (!box) throw new Error(`No box for ${label}`);
	await ensureDemoChrome(page);
	await page.evaluate(
		({ x, y, label }) => {
			const cursor = document.querySelector("#nightingale-demo-cursor");
			cursor.style.left = `${x}px`;
			cursor.style.top = `${y}px`;
			const bubble = cursor.querySelector("span");
			bubble.textContent = "";
			bubble.dataset.label = label;
			bubble.style.opacity = label ? "1" : "0";
		},
		{
			x: Math.min(
				VIEWPORT.width - 48,
				Math.max(8, box.x + Math.min(box.width * 0.55, box.width - 8)),
			),
			y: Math.min(
				VIEWPORT.height - 50,
				Math.max(8, box.y + Math.min(box.height * 0.55, box.height - 8)),
			),
			label,
		},
	);
	await locator.evaluate((node) => {
		node.dataset.demoPointed = "true";
		node.style.transition = "box-shadow 180ms ease, outline 180ms ease";
		node.style.outline = "3px solid rgba(13,148,136,.42)";
		node.style.outlineOffset = "4px";
	});
	await sleep(options.hold ?? 850);
	if (options.click) {
		await page.evaluate(() => {
			const cursor = document.querySelector("#nightingale-demo-cursor");
			cursor.animate(
				[
					{ transform: "scale(1)" },
					{ transform: "scale(.72)" },
					{ transform: "scale(1)" },
				],
				{ duration: 360 },
			);
		});
		await locator.click();
		await sleep(options.after ?? 650);
	}
	await page
		.evaluate(() => {
			document.querySelectorAll("[data-demo-pointed]").forEach((node) => {
				node.style.outline = "";
				node.style.outlineOffset = "";
				delete node.dataset.demoPointed;
			});
		})
		.catch(() => {});
}

async function showCard(page, title, lines, seconds = 7) {
	await ensureDemoChrome(page);
	await page.evaluate(
		({ title, lines }) => {
			document.querySelector("#nightingale-demo-evidence")?.remove();
			const card = document.createElement("section");
			card.id = "nightingale-demo-evidence";
			card.innerHTML = `<div class="eyebrow">VERIFIED EVIDENCE</div><h2></h2><div class="rows"></div>`;
			card.querySelector("h2").textContent = title;
			const rows = card.querySelector(".rows");
			for (const line of lines) {
				const row = document.createElement("div");
				row.className = "row";
				row.innerHTML = `<span class="dot">✓</span><span></span>`;
				row.lastElementChild.textContent = line;
				rows.append(row);
			}
			Object.assign(card.style, {
				position: "fixed",
				inset: "120px 260px",
				zIndex: "2147483645",
				pointerEvents: "none",
				background: "rgba(255,255,255,.97)",
				color: "#0f172a",
				border: "1px solid rgba(15,118,110,.3)",
				borderRadius: "24px",
				padding: "38px 46px",
				boxShadow: "0 30px 90px rgba(2,6,23,.3)",
				fontFamily: "Inter,system-ui,sans-serif",
			});
			Object.assign(card.querySelector(".eyebrow").style, {
				color: "#0f766e",
				fontWeight: "800",
				fontSize: "12px",
				letterSpacing: ".16em",
			});
			Object.assign(card.querySelector("h2").style, {
				margin: "10px 0 24px",
				fontFamily: "Georgia,serif",
				fontSize: "34px",
			});
			Object.assign(rows.style, { display: "grid", gap: "13px" });
			card.querySelectorAll(".row").forEach((row) => {
				Object.assign(row.style, {
					display: "flex",
					alignItems: "center",
					gap: "14px",
					padding: "12px 16px",
					background: "#f0fdfa",
					borderRadius: "12px",
					fontSize: "18px",
					fontWeight: "650",
				});
			});
			card.querySelectorAll(".dot").forEach((dot) => {
				Object.assign(dot.style, {
					display: "grid",
					placeItems: "center",
					width: "26px",
					height: "26px",
					borderRadius: "50%",
					background: "#0f766e",
					color: "white",
				});
			});
			document.body.append(card);
		},
		{ title, lines },
	);
	const rows = page.locator("#nightingale-demo-evidence .row");
	for (let i = 0; i < (await rows.count()); i += 1)
		await moveTo(page, rows.nth(i), "evidence", { hold: 320 });
	await sleep(seconds * 1000);
	await page.evaluate(() =>
		document.querySelector("#nightingale-demo-evidence")?.remove(),
	);
}

async function login(page, context, persona, destination) {
	// Leave the previous role's React tree before clearing cookies. Otherwise an
	// in-flight auth/SSE request from that page can observe the empty-cookie
	// window and clear the newly issued persona cookie after demo-login returns.
	await page.goto("about:blank");
	await context.clearCookies();
	const response = await page.request.post(`${BASE}/api/v1/auth/demo-login`, {
		data: { persona },
	});
	if (!response.ok())
		throw new Error(`demo-login ${persona}: ${response.status()}`);
	await page.goto(`${BASE}${destination}`, { waitUntil: "domcontentloaded" });
	await page.waitForLoadState("networkidle").catch(() => {});
}

function createSyntheticWav(durationSeconds = 11, sampleRate = 16_000) {
	const frameCount = Math.round(durationSeconds * sampleRate);
	const bytesPerSample = 2;
	const dataSize = frameCount * bytesPerSample;
	const wav = Buffer.alloc(44 + dataSize);
	wav.write("RIFF", 0, "ascii");
	wav.writeUInt32LE(36 + dataSize, 4);
	wav.write("WAVE", 8, "ascii");
	wav.write("fmt ", 12, "ascii");
	wav.writeUInt32LE(16, 16);
	wav.writeUInt16LE(1, 20);
	wav.writeUInt16LE(1, 22);
	wav.writeUInt32LE(sampleRate, 24);
	wav.writeUInt32LE(sampleRate * bytesPerSample, 28);
	wav.writeUInt16LE(bytesPerSample, 32);
	wav.writeUInt16LE(16, 34);
	wav.write("data", 36, "ascii");
	wav.writeUInt32LE(dataSize, 40);
	for (let index = 0; index < frameCount; index += 1) {
		const sample = Math.round(
			4_000 * Math.sin((2 * Math.PI * 220 * index) / sampleRate),
		);
		wav.writeInt16LE(sample, 44 + index * bytesPerSample);
	}
	return wav;
}

async function expectApiJson(response, label, expectedStatus) {
	if (response.status() !== expectedStatus) {
		throw new Error(
			`${label}: expected ${expectedStatus}, received ${response.status()} ${await response.text()}`,
		);
	}
	return response.json();
}

async function createSyntheticVoiceReview(page) {
	const patientsResponse = await page.request.get(`${BASE}/api/v1/patients`);
	const patients = await expectApiJson(patientsResponse, "list patients", 200);
	const patient =
		patients.data?.find((item) => item.display_name === "Jordan Wong") ||
		patients.data?.[0];
	if (!patient?.id) throw new Error("No patient is available for voice review");

	const mutationHeaders = { Origin: TRUSTED_ORIGIN };
	const created = await expectApiJson(
		await page.request.post(`${BASE}/api/v1/voice/sessions`, {
			headers: mutationHeaders,
			data: {
				patient_id: patient.id,
				capture_kind: "clinical",
				synthetic_fixture: true,
				fixture_id: "code-switch-overlap-v1",
			},
		}),
		"create synthetic voice session",
		201,
	);
	const joined = await expectApiJson(
		await page.request.post(
			`${BASE}/api/v1/voice/sessions/${created.id}/devices`,
			{
				headers: mutationHeaders,
				data: {
					client_device_id: `english-demo-${created.id}`,
					capture_role: "clinician",
					expected_patient_id: patient.id,
					expected_capture_kind: "clinical",
				},
			},
		),
		"join synthetic voice session",
		201,
	);

	const wav = createSyntheticWav();
	await expectApiJson(
		await page.request.put(
			`${BASE}/api/v1/voice/sessions/${created.id}/devices/${joined.id}/chunks/0`,
			{
				headers: {
					...mutationHeaders,
					"Content-Type": "audio/wav",
					"X-Chunk-SHA256": createHash("sha256").update(wav).digest("hex"),
					"X-Chunk-Start-Ms": "0",
					"X-Chunk-End-Ms": "11000",
				},
				data: wav,
			},
		),
		"upload synthetic voice chunk",
		200,
	);
	await expectApiJson(
		await page.request.post(
			`${BASE}/api/v1/voice/sessions/${created.id}/devices/${joined.id}/seal`,
			{
				headers: mutationHeaders,
				data: { last_chunk_index: 0 },
			},
		),
		"seal synthetic voice session",
		200,
	);
	await expectApiJson(
		await page.request.post(
			`${BASE}/api/v1/voice/sessions/${created.id}/finalize`,
			{
				headers: {
					...mutationHeaders,
					"Idempotency-Key": `english-demo-voice-${created.id}`,
				},
				data: {
					devices: [{ device_id: joined.id, last_chunk_index: 0 }],
				},
			},
		),
		"finalize synthetic voice session",
		202,
	);

	for (let attempt = 0; attempt < 300; attempt += 1) {
		const statusResponse = await page.request.get(
			`${BASE}/api/v1/voice/sessions/${created.id}`,
		);
		const status = await expectApiJson(
			statusResponse,
			"poll synthetic voice session",
			200,
		);
		if (status.state === "needs_review" || status.state === "published") {
			return created.id;
		}
		if (status.state === "failed") {
			throw new Error(
				`Synthetic voice processing failed: ${status.error_code || "unknown"}`,
			);
		}
		await new Promise((resolveWait) => setTimeout(resolveWait, 200));
	}
	throw new Error(
		"Synthetic voice review did not become ready within 60 seconds",
	);
}

async function openPatient(page, name) {
	if (!/\/patients\/?$/.test(new URL(page.url()).pathname))
		await page.goto(`${BASE}/patients`, { waitUntil: "domcontentloaded" });
	const link = page.getByRole("link", { name: `Open care note for ${name}` });
	await link.waitFor({ state: "visible", timeout: 15000 });
	await moveTo(page, link, name, { click: true, hold: 650 });
	await page
		.getByRole("heading", { name })
		.waitFor({ state: "visible", timeout: 15000 });
}

async function closeVisibleDialog(page) {
	const dialogs = page.getByRole("dialog");
	for (let index = (await dialogs.count()) - 1; index >= 0; index -= 1) {
		const dialog = dialogs.nth(index);
		if (!(await dialog.isVisible().catch(() => false))) continue;
		const chromeClose = dialog.locator('[data-slot="dialog-close"]').last();
		const semanticClose = dialog
			.getByRole("button", { name: /Close|Cancel|Keep shared/ })
			.first();
		const close = (await chromeClose.isVisible().catch(() => false))
			? chromeClose
			: semanticClose;
		if (await close.isVisible().catch(() => false)) {
			await moveTo(page, close, "Close", { click: true, hold: 350 });
		} else {
			await page.keyboard.press("Escape");
		}
		await dialog.waitFor({ state: "hidden", timeout: 5000 });
		return;
	}
}

async function runSegment(page, index, action) {
	const segment = segments[index];
	const started = Date.now();
	await setScene(page, segment, index);
	console.log(`[${index + 1}/${segments.length}] ${segment.title}`);
	await action().catch((error) => {
		failures.push(`${segment.title}: ${error.stack || error}`);
		console.error(`Scene error: ${error.message}`);
	});
	const expectedMilliseconds = segment.seconds * 1000 * SPEED;
	const actionMilliseconds = Date.now() - started;
	const overrunMilliseconds = Math.max(
		0,
		actionMilliseconds - expectedMilliseconds,
	);
	segmentTimings.push({
		action_seconds: actionMilliseconds / 1000,
		expected_seconds: expectedMilliseconds / 1000,
		index: index + 1,
		overrun_seconds: overrunMilliseconds / 1000,
		title: segment.title,
	});
	if (SPEED === 1 && overrunMilliseconds > SEGMENT_OVERRUN_TOLERANCE_MS) {
		failures.push(
			`Segment ${index + 1} overran its subtitle window by ${(overrunMilliseconds / 1000).toFixed(3)} seconds: ${segment.title}`,
		);
	}
	const remaining = expectedMilliseconds - actionMilliseconds;
	if (remaining > 0)
		await new Promise((resolveWait) => setTimeout(resolveWait, remaining));
}

await mkdir(dirname(OUTPUT), { recursive: true });
await mkdir(dirname(RECORDING_MANIFEST), { recursive: true });
await mkdir(rawDir, { recursive: true });
await rm(OUTPUT, { force: true });
await rm(RECORDING_MANIFEST, { force: true });

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
	ignoreHTTPSErrors: true,
	viewport: VIEWPORT,
	recordVideo: { dir: rawDir, size: VIEWPORT },
	colorScheme: "light",
});
const page = await context.newPage();
if (TRUSTED_ORIGIN_OVERRIDE) {
	await page.route("**/api/v1/**", async (route) => {
		const request = route.request();
		if (!MUTATION_METHODS.has(request.method())) {
			await route.continue();
			return;
		}
		const response = await route.fetch({
			headers: {
				...request.headers(),
				origin: TRUSTED_ORIGIN_OVERRIDE,
			},
		});
		await route.fulfill({ response });
	});
}
page.on("response", (response) => {
	const method = response.request().method();
	const status = response.status();
	const detail = `${status} ${method} ${response.url()}`;
	if (MUTATION_METHODS.has(method) && status >= 400) {
		mutationResponseFailures.push(detail);
		failures.push(`Mutation response failed: ${detail}`);
	} else if (status >= 500) {
		failures.push(detail);
	}
});
const video = page.video();
if (!video) throw new Error("Playwright video unavailable");
const recordingStarted = Date.now();

await login(page, context, "staff", "/patients");
await page
	.getByRole("heading", { name: "Patients" })
	.waitFor({ state: "visible" });
const preRoll = (Date.now() - recordingStarted) / 1000;

await runSegment(page, 0, async () => {
	await moveTo(
		page,
		page.getByRole("heading", { name: "Today's visits" }),
		"Today's visits",
	);
	await moveTo(
		page,
		page.getByPlaceholder(/Search by patient name/),
		"Search 303 records",
	);
	await moveTo(
		page,
		page.getByRole("heading", { name: "Previous patient records" }),
		"Previous records",
	);
	await moveTo(
		page,
		page.getByRole("link", { name: "Open care note for Jordan Wong" }),
		"Jordan Wong",
	);
});

await runSegment(page, 1, async () => {
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByText(/22-year history/).first(),
		"22-year history",
	);
	await moveTo(
		page,
		page.getByText("3 AI-assisted notes", { exact: true }),
		"3 AI notes",
	);
	await moveTo(
		page,
		page.getByText("1 unresolved conflicts", { exact: true }),
		"1 conflict",
	);
	await moveTo(
		page,
		page.getByRole("heading", { name: "Current priorities" }),
		"Current priorities",
	);
	const aiCard = page
		.getByRole("listitem")
		.filter({ hasText: "AI-scribed handover: hydration-plan discrepancy" });
	await moveTo(
		page,
		aiCard.getByRole("button", { name: "View source" }),
		"View exact source",
		{ click: true },
	);
	const dialog = page.getByRole("dialog", { name: /Source details/ });
	await dialog.waitFor({ state: "visible" });
	await moveTo(
		page,
		dialog.getByText(/AI-assisted nursing handover/).first(),
		"Source title",
	);
	await moveTo(page, dialog.locator("mark[data-source-span]"), "Exact quote");
	const sourceStatus = dialog.getByText(/Source status:/).first();
	if (await sourceStatus.isVisible().catch(() => false))
		await moveTo(page, sourceStatus, "Immutable version");
	await closeVisibleDialog(page);
	await moveTo(
		page,
		page.getByRole("heading", { name: "Needs clinical review" }),
		"Needs clinical review",
	);
});

const createdNoteTitle = "Follow-up plan confirmed";
await runSegment(page, 2, async () => {
	await login(page, context, "staff", "/patients");
	await openPatient(page, "Alex Tan");
	if (MUTATE) {
		await moveTo(
			page,
			page.getByRole("button", { name: "Add care note" }),
			"Add care note",
			{ click: true },
		);
		const dialog = page.getByRole("dialog", { name: "New care note" });
		await dialog.getByLabel("Title").fill(createdNoteTitle);
		await dialog
			.getByLabel("Care note")
			.fill(
				"Patient confirmed the nurse follow-up call for tomorrow morning and asked to keep the written care instructions available in My Care.",
			);
		await moveTo(
			page,
			dialog.getByRole("button", { name: "Create note" }),
			"Create note",
			{ click: true },
		);
		const article = page
			.locator(`article[aria-label="Care staff note: ${createdNoteTitle}"]`)
			.first();
		await article.waitFor({ state: "visible", timeout: 15000 });
		await moveTo(
			page,
			article.getByRole("button", { name: "Edit" }),
			"Edit note",
			{ click: true },
		);
		const editDialog = page.getByRole("dialog", { name: "Edit note" });
		await editDialog.waitFor({ state: "visible" });
		const editor = editDialog.getByLabel("Care note content");
		const quote = "nurse follow-up call for tomorrow morning";
		await editor.evaluate((root, quote) => {
			const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
			let node = walker.nextNode();
			while (node) {
				const start = node.textContent?.indexOf(quote) ?? -1;
				if (start >= 0) {
					const range = document.createRange();
					range.setStart(node, start);
					range.setEnd(node, start + quote.length);
					const selection = window.getSelection();
					selection.removeAllRanges();
					selection.addRange(range);
					root.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
					document.dispatchEvent(
						new Event("selectionchange", { bubbles: true }),
					);
					return;
				}
				node = walker.nextNode();
			}
			throw new Error("selection missing");
		}, quote);
		await moveTo(
			page,
			editDialog.getByRole("button", { name: "Comment on selection" }),
			"Comment on selection",
			{ click: true },
		);
		await editDialog
			.getByLabel("Comment", { exact: true })
			.fill("Please confirm the timing before patient sharing.");
		await editDialog
			.getByLabel("Mention (optional)")
			.selectOption({ label: "Clinician — Clinician" });
		await editDialog
			.getByLabel("Assign to (optional)")
			.selectOption({ label: "Clinician — Clinician" });
		await moveTo(
			page,
			editDialog.getByRole("button", { name: "Add to team discussion" }),
			"@Clinician · Assign",
			{ click: true },
		);
		const cancel = editDialog.getByRole("button", {
			name: "Cancel",
			exact: true,
		});
		if (await cancel.isVisible().catch(() => false)) await cancel.click();
		await moveTo(
			page,
			article.getByRole("button", { name: "Team discussion" }),
			"Team discussion",
			{ click: true },
		);
		await moveTo(
			page,
			page.getByText("Please confirm the timing before patient sharing.", {
				exact: true,
			}),
			"Anchored comment",
		);
	} else {
		await moveTo(
			page,
			page.getByRole("button", { name: "Add care note" }),
			"Add care note",
			{ click: true },
		);
		await moveTo(
			page,
			page
				.getByRole("dialog", { name: "New care note" })
				.getByRole("button", { name: "Cancel" }),
			"Cancel smoke preview",
			{ click: true },
		);
		const seeded = page.locator(
			'article[aria-label="Care staff note: Medication reconciliation"]',
		);
		await moveTo(
			page,
			seeded.getByRole("button", { name: "Team discussion" }),
			"Team discussion",
			{ click: true },
		);
	}
	await moveTo(
		page,
		page.getByRole("heading", { name: "Patient sharing" }),
		"Patient sharing",
	);
	if (MUTATE) {
		const select = page.locator("#sharing-entry");
		await select.selectOption({ label: createdNoteTitle });
		await moveTo(
			page,
			page.getByTestId("request-patient-sharing"),
			"Request clinician review",
			{ click: true },
		);
		await moveTo(
			page,
			page.getByText(createdNoteTitle, { exact: true }).last(),
			"Awaiting review",
		);
	} else {
		const pending = page.getByText("Awaiting review", { exact: true }).first();
		if (await pending.isVisible().catch(() => false))
			await moveTo(page, pending, "Awaiting review");
	}
});

await runSegment(page, 3, async () => {
	await login(page, context, "clinician", "/patients");
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByRole("link", { name: "Timeline", exact: true }),
		"Timeline",
		{ click: true },
	);
	for (const year of ["2026", "2025", "2021", "2004"]) {
		const heading = page.getByRole("heading", { name: year, exact: true });
		if (await heading.isVisible().catch(() => false))
			await moveTo(page, heading, year, { hold: 350 });
		else {
			await heading.scrollIntoViewIfNeeded();
			await moveTo(page, heading, year, { hold: 350 });
		}
	}
	const aiArticle = page.locator(
		'article[aria-label="AI-assisted nursing note: AI-assisted nursing handover"]',
	);
	await aiArticle.scrollIntoViewIfNeeded();
	await moveTo(
		page,
		aiArticle.getByText("AI-assisted nursing note", { exact: true }),
		"AI-assisted nursing note",
	);
	const quote = "Vomiting continues, oral intake remains restricted";
	const source = aiArticle.getByRole("group", {
		name: "Select exact wording from this AI-assisted note",
	});
	await source.evaluate((root, quote) => {
		const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
		const nodes = [];
		let node = walker.nextNode();
		let text = "";
		while (node) {
			nodes.push({
				node,
				start: text.length,
				end: text.length + (node.textContent?.length ?? 0),
			});
			text += node.textContent ?? "";
			node = walker.nextNode();
		}
		const start = text.toLowerCase().indexOf(quote.toLowerCase());
		if (start < 0) throw new Error(`AI quote not found in: ${text}`);
		const end = start + quote.length;
		const first = nodes.find(
			(item) => start >= item.start && start <= item.end,
		);
		const last = nodes.find((item) => end >= item.start && end <= item.end);
		if (!first || !last) throw new Error("AI quote range could not be mapped");
		const range = document.createRange();
		range.setStart(first.node, start - first.start);
		range.setEnd(last.node, end - last.start);
		const selection = window.getSelection();
		selection.removeAllRanges();
		selection.addRange(range);
		root.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));
		document.dispatchEvent(new Event("selectionchange", { bubbles: true }));
	}, quote);
	await moveTo(
		page,
		aiArticle.getByRole("button", { name: "Add to priorities" }),
		"Add to priorities",
		{ click: true },
	);
	const addDialog = page.getByRole("dialog", {
		name: "Add source-linked priority",
	});
	await moveTo(page, addDialog.locator("blockquote"), "Exact AI wording");
	if (MUTATE)
		await moveTo(
			page,
			addDialog.getByRole("button", { name: "Add to Current priorities" }),
			"Confirm source link",
			{ click: true },
		);
	else
		await moveTo(
			page,
			addDialog.getByRole("button", { name: "Cancel" }),
			"Cancel smoke preview",
			{ click: true },
		);
	await moveTo(
		page,
		page.getByRole("link", { name: "Current priorities", exact: true }),
		"Current priorities",
		{ click: true },
	);
	if (MUTATE) {
		const newCard = page.getByRole("listitem").filter({ hasText: quote });
		if (await newCard.isVisible().catch(() => false)) {
			await moveTo(page, newCard, "Clinician-confirmed");
			await moveTo(
				page,
				newCard.getByRole("button", { name: "View source" }),
				"Same exact source",
				{ click: true },
			);
			const dialog = page.getByRole("dialog", { name: /Source details/ });
			await moveTo(
				page,
				dialog.locator("mark[data-source-span]"),
				"Exact quote",
			);
			await closeVisibleDialog(page);
		}
	}
	await moveTo(
		page,
		page.getByRole("link", { name: "Source-linked facts", exact: true }),
		"Source-linked facts",
		{ click: true },
	);
	const facts = page.getByRole("heading", {
		name: "Structured clinical context",
	});
	await moveTo(page, facts, "Normalized facts");
	const exactSource = page
		.getByRole("button", { name: "View exact source" })
		.first();
	if (await exactSource.isVisible().catch(() => false)) {
		await moveTo(page, exactSource, "View exact source", { click: true });
		await closeVisibleDialog(page);
	}
	await showCard(
		page,
		"Timeline trust contract",
		[
			"author_role: system",
			"provenance.status: resolved",
			"exact source: immutable version",
		],
		4,
	);
});

await runSegment(page, 4, async () => {
	await login(page, context, "clinician", "/patients");
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByRole("link", { name: "Timeline", exact: true }),
		"Timeline",
		{ click: true },
	);
	const article = page.locator(
		'article[aria-label="Clinical note: Current pancreatitis admission plan"]',
	);
	await article.scrollIntoViewIfNeeded();
	await moveTo(
		page,
		article.getByRole("button", { name: "Change history" }),
		"Change history",
		{ click: true },
	);
	const dialog = page.getByRole("dialog", { name: /Change history/ });
	const v1 = dialog.getByRole("listitem").filter({ hasText: "Version 1" });
	const v3 = dialog.getByRole("listitem").filter({ hasText: "Version 3" });
	await moveTo(page, v1, "Version 1");
	await moveTo(page, v3, "Version 3 · Current");
	const from = v1.getByRole("button", { name: /Compare from/ });
	const to = v3.getByRole("button", { name: /Compare to/ });
	if (await from.isVisible().catch(() => false))
		await moveTo(page, from, "Compare from", { click: true });
	if (await to.isVisible().catch(() => false))
		await moveTo(page, to, "Compare to", { click: true });
	const changes = dialog.getByRole("heading", { name: "Changes", exact: true });
	if (await changes.isVisible().catch(() => false))
		await moveTo(page, changes, "Immutable diff");
	const restore = v1.getByRole("button", { name: "Restore this version" });
	if (await restore.isVisible().catch(() => false)) {
		if (MUTATE) {
			await moveTo(page, restore, "Restore as a new version", { click: true });
			await dialog.waitFor({ state: "hidden", timeout: 15000 });
			const updatedArticle = page.locator(
				'article[aria-label="Clinical note: Current pancreatitis admission plan"]',
			);
			await moveTo(
				page,
				updatedArticle.getByRole("button", { name: "Change history" }),
				"Verify appended version",
				{ click: true },
			);
			const updatedDialog = page.getByRole("dialog", {
				name: /Change history/,
			});
			const restoredVersion = updatedDialog
				.getByRole("listitem")
				.filter({ hasText: "Current" })
				.first();
			await moveTo(page, restoredVersion, "Restored snapshot · Current");
			await closeVisibleDialog(page);
		} else {
			await moveTo(page, restore, "Restore creates a new version");
			await closeVisibleDialog(page);
		}
	} else {
		await closeVisibleDialog(page);
	}
	await showCard(
		page,
		"Version rule",
		[
			"Saved versions are immutable",
			"Diff compares exact snapshots",
			"Restore appends a new version; history remains",
		],
		4,
	);
});

await runSegment(page, 5, async () => {
	await login(page, context, "clinician", "/patients");
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByRole("link", { name: "Clinical review", exact: true }),
		"Clinical review",
		{ click: true },
	);
	const conflict = page.getByRole("heading", { name: "Clinical conflicts" });
	await moveTo(page, conflict, "High conflict");
	for (const name of ["View first source", "View conflicting source"]) {
		const button = page.getByRole("button", { name }).first();
		await moveTo(page, button, name, { click: true });
		const dialog = page.getByRole("dialog", { name: /Source details/ });
		if (await dialog.isVisible().catch(() => false)) {
			const mark = dialog.locator("mark[data-source-span]");
			if (await mark.isVisible().catch(() => false))
				await moveTo(page, mark, "Exact conflicting wording");
			await closeVisibleDialog(page);
		}
	}
	const why = page.getByText("Why this decision?", { exact: true }).first();
	if (await why.isVisible().catch(() => false)) {
		await moveTo(page, why, "Why this decision?", { click: true });
		for (const text of [
			"What is it?",
			"How could it be wrong?",
			"What happens when it is wrong?",
		]) {
			const item = page.getByText(text, { exact: true }).first();
			if (await item.isVisible().catch(() => false))
				await moveTo(page, item, text, { hold: 300 });
		}
	}
	const resolve = page.getByRole("button", { name: "Resolve conflict" });
	if (await resolve.isVisible().catch(() => false)) {
		await moveTo(page, resolve, "Clinician correction", { click: true });
		const dialog = page.getByRole("dialog", {
			name: "Resolve clinical conflict",
		});
		const correction = dialog.getByLabel("Correction entry");
		const reason = dialog.getByLabel("Resolution reason");
		if (MUTATE) {
			await correction.selectOption({
				label: "Current pancreatitis admission plan",
			});
			await moveTo(page, correction, "Clinician-authored correction");
			await reason.fill(
				"The acute pancreatitis plan applies during active vomiting. Continue bedside glucose monitoring and reassess oral intake after acute-care review.",
			);
			await moveTo(page, reason, "Required resolution reason");
			await moveTo(
				page,
				dialog.getByRole("button", { name: "Resolve with correction" }),
				"Resolve with correction",
				{ click: true },
			);
			await dialog.waitFor({ state: "hidden", timeout: 15000 });
			const resolved = page
				.getByText("Status: resolved", { exact: true })
				.first();
			if (await resolved.isVisible().catch(() => false)) {
				await moveTo(page, resolved, "Resolved · sources preserved");
			}
		} else {
			await reason.fill(
				"The acute pancreatitis plan applies during active vomiting. Continue bedside glucose monitoring and reassess oral intake after acute-care review.",
			);
			await moveTo(
				page,
				dialog.getByRole("button", { name: "Cancel" }),
				"Cancel smoke preview",
				{ click: true },
			);
		}
	}
});

await runSegment(page, 6, async () => {
	await login(page, context, "clinician", "/patients");
	await openPatient(page, "Alex Tan");
	await moveTo(
		page,
		page.getByRole("heading", { name: "Patient sharing" }),
		"Patient sharing",
	);
	let requestBox = page
		.getByTestId(/sharing-request-/)
		.filter({ hasText: createdNoteTitle })
		.last();
	if (!(await requestBox.isVisible().catch(() => false)))
		requestBox = page
			.getByTestId(/sharing-request-/)
			.filter({ hasText: "Medication reconciliation" })
			.last();
	await moveTo(page, requestBox, "Pending exact version");
	const review = requestBox.getByRole("button", {
		name: "Review exact version",
	});
	await moveTo(page, review, "Review exact version", { click: true });
	const dialog = page.getByRole("dialog", { name: "Review patient sharing" });
	await moveTo(
		page,
		dialog.getByText("Saved version", { exact: true }),
		"Saved version",
	);
	await moveTo(
		page,
		dialog.getByText(/Publishing also verifies/),
		"Safety gates",
	);
	if (MUTATE)
		await moveTo(
			page,
			dialog.getByTestId("approve-patient-sharing"),
			"Approve and publish",
			{ click: true },
		);
	else
		await moveTo(
			page,
			dialog.getByRole("button", { name: "Cancel" }),
			"Cancel smoke preview",
			{ click: true },
		);
	if (MUTATE) {
		await login(page, context, "patient", "/patient/my-care");
		await page
			.getByRole("heading", { name: /My Care · Alex Tan/ })
			.waitFor({ state: "visible" });
		await moveTo(
			page,
			page.getByText("Patient access", { exact: true }),
			"Patient-only portal",
		);
		const publishedTitle = page
			.getByText(createdNoteTitle, { exact: true })
			.first();
		if (await publishedTitle.isVisible().catch(() => false))
			await moveTo(page, publishedTitle, "Newly shared note");
		const receipt = page
			.getByText("Reviewed for sharing", { exact: true })
			.last();
		if (await receipt.isVisible().catch(() => false))
			await moveTo(page, receipt, "Approval receipt");
		const approvedSource = page
			.getByRole("button", { name: "View approved source" })
			.last();
		if (await approvedSource.isVisible().catch(() => false)) {
			await moveTo(page, approvedSource, "Approved source", { click: true });
			await closeVisibleDialog(page);
		}
		await login(page, context, "clinician", "/patients");
		await openPatient(page, "Alex Tan");
		await moveTo(
			page,
			page.getByRole("heading", { name: "Patient sharing" }),
			"Currently shared",
		);
		const sharedSection = page
			.getByText("Currently shared", { exact: true })
			.locator("..");
		const sharedCard = sharedSection
			.locator("div.rounded-xl")
			.filter({ hasText: createdNoteTitle })
			.first();
		const shared = sharedCard.getByText(createdNoteTitle, { exact: true });
		await moveTo(page, shared, "Currently shared");
		const withdraw = sharedCard.getByRole("button", {
			name: "Withdraw from patient portal",
		});
		await moveTo(page, withdraw, "Withdraw", { click: true });
		const withdrawDialog = page.getByRole("dialog", {
			name: "Withdraw patient sharing?",
		});
		await moveTo(
			page,
			withdrawDialog.getByText(/receipt, and audit history remain/),
			"Receipt and audit remain",
		);
		await moveTo(
			page,
			withdrawDialog.getByTestId("withdraw-patient-sharing"),
			"Confirm withdraw",
			{ click: true },
		);
		await login(page, context, "patient", "/patient/my-care");
		await page
			.getByRole("heading", { name: /My Care · Alex Tan/ })
			.waitFor({ state: "visible" });
		const withdrawn = page.getByText("Withdrawn", { exact: true }).last();
		if (await withdrawn.isVisible().catch(() => false))
			await moveTo(page, withdrawn, "Withdrawn receipt");
	}
});

await runSegment(page, 7, async () => {
	await login(page, context, "admin", "/admin");
	await page
		.getByRole("heading", { name: "Clinic administration" })
		.waitFor({ state: "visible" });
	const readonly = page
		.getByText(/Clinical documentation remains read-only/)
		.first();
	if (await readonly.isVisible().catch(() => false))
		await moveTo(page, readonly, "Read-only clinical oversight");
	await moveTo(
		page,
		page.getByText("AI processing", { exact: true }).first(),
		"AI processing",
	);
	const configured = page
		.getByText(/Configured|Environment credential|Not configured/)
		.first();
	if (await configured.isVisible().catch(() => false))
		await moveTo(page, configured, "Secret is never returned");
	await moveTo(
		page,
		page.locator("#main-content").getByText("Activity log", { exact: true }),
		"Activity log",
	);
	const sgt = page.getByText(/Singapore Time|SGT/).first();
	if (await sgt.isVisible().catch(() => false))
		await moveTo(page, sgt, "Singapore time");
	await moveTo(
		page,
		page.getByRole("link", { name: "Patients" }),
		"Patients · read-only",
		{ click: true },
	);
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByText("Clinic administrator · read-only oversight"),
		"No clinical write controls",
	);
});

await runSegment(page, 8, async () => {
	await login(page, context, "clinician", "/patients");
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByRole("link", { name: "Current priorities", exact: true }),
		"Current priorities",
		{ click: true },
	);
	const why = page.getByText("Why this decision?", { exact: true }).first();
	await moveTo(page, why, "Why this decision?", { click: true });
	for (const pattern of [
		/Rule-based score/i,
		/Clinic feedback adjustment/i,
		/Final importance/i,
		/Protected/i,
	]) {
		const node = page.getByText(pattern).first();
		if (await node.isVisible().catch(() => false))
			await moveTo(page, node, String(pattern).replaceAll("/", ""), {
				hold: 300,
			});
	}
	const dismiss = page.getByText("Dismiss…", { exact: true }).first();
	if (await dismiss.isVisible().catch(() => false))
		await moveTo(page, dismiss, "Dismiss requires a reason", { click: true });
	const tooBusy = page.getByText("Too busy to review", { exact: true }).last();
	if (await tooBusy.isVisible().catch(() => false))
		await moveTo(page, tooBusy, "No negative learning", { click: true });
	await page.keyboard.press("Escape").catch(() => {});
	await showCard(
		page,
		"Auditable clinic-level learning",
		[
			"importance_feedback_events · explicit reason",
			"importance_feature_stats · bounded feature weight",
			"importance_impressions · exposure audit only",
			"Critical / unresolved / clinician-confirmed · protected",
		],
		5,
	);
});

await runSegment(page, 9, async () => {
	const heading = page.getByRole("heading", { name: "Historical retention" });
	await moveTo(page, heading, "Historical retention");
	for (const text of ["Archived", "Protected", "Eligible"]) {
		const node = page.getByText(text, { exact: true }).first();
		if (await node.isVisible().catch(() => false))
			await moveTo(page, node, text, { hold: 380 });
	}
	await showCard(
		page,
		"Recoverable data lifecycle",
		[
			"hot record body",
			"zstd compression + AES-GCM encryption",
			"immutable metadata + provenance + audit remain",
			"rehydrate + checksum verification",
		],
		5,
	);
});

await runSegment(page, 10, async () => {
	await login(page, context, "clinician", "/patients");
	if (!MUTATE) {
		throw new Error(
			"Voice review requires DEMO_MUTATE=1 so the isolated synthetic fixture can be processed",
		);
	}
	const voiceSessionId = await createSyntheticVoiceReview(page);
	await page.goto(`${BASE}/voice/${voiceSessionId}/review`, {
		waitUntil: "domcontentloaded",
	});
	await page.waitForLoadState("networkidle").catch(() => {});
	await page
		.getByRole("heading", { name: "Review visit recording" })
		.waitFor({ state: "visible", timeout: 15000 });
	await moveTo(
		page,
		page.getByRole("heading", { name: "Review visit recording" }),
		"Synthetic voice review",
	);
	const transcript = page.getByTestId("transcript-panel-desktop");
	await moveTo(
		page,
		transcript.getByText("Speaker 1", { exact: true }),
		"Speaker 1",
	);
	await moveTo(
		page,
		transcript.getByText("0:00–0:05", { exact: true }),
		"Timestamp",
	);
	await moveTo(
		page,
		transcript.getByText("Confidence unavailable", { exact: true }).first(),
		"Confidence unavailable",
	);
	await moveTo(
		page,
		transcript.getByText("Overlapping speech", { exact: true }),
		"Overlap retained",
	);
	const finding = page
		.getByTestId("facts-panel")
		.filter({ visible: true })
		.getByText("penicillin allergy", { exact: true })
		.last();
	await moveTo(page, finding, "Fact → transcript/audio", { click: true });
	await moveTo(
		page,
		page.getByRole("button", { name: "Publish reviewed note" }),
		"Clinician review required",
	);
});

await runSegment(page, 11, async () => {
	await showCard(
		page,
		"Current revision verification",
		[
			"Backend · frontend · typecheck · production build: passed",
			"Alembic migration and browser release gates: passed",
			"RBAC · immutable versions · provenance · sharing: passed",
			`Git ${RUNTIME_REVISION.slice(0, 16)}`,
			`OCI ${IMAGE_DIGEST.slice(0, 24)}`,
		],
		10,
	);
});

await runSegment(page, 12, async () => {
	await login(page, context, "clinician", "/patients");
	await openPatient(page, "Jordan Wong");
	await moveTo(
		page,
		page.getByRole("heading", { name: "Current priorities" }),
		"Supported · ready",
	);
	await moveTo(
		page,
		page.getByRole("heading", { name: "Needs clinical review" }),
		"Abstained · review required",
	);
	const source = page.getByRole("button", { name: "View source" }).first();
	if (await source.isVisible().catch(() => false))
		await moveTo(page, source, "Exact source");
});

await page.close();
await context.close();
const raw = await video.path();
await browser.close();
await copyFile(raw, OUTPUT);

const media = await stat(OUTPUT);
await writeFile(
	RECORDING_MANIFEST,
	`${JSON.stringify(
		{
			base_url: BASE,
			expected_segment_duration_seconds: TARGET_DURATION_SECONDS * SPEED,
			failures,
			image_digest: IMAGE_DIGEST,
			language: "en",
			mutate: MUTATE,
			mutation_response_failures: mutationResponseFailures,
			raw_video: OUTPUT,
			pre_roll_seconds: preRoll,
			recorded_at: new Date().toISOString(),
			resolution: `${VIEWPORT.width}x${VIEWPORT.height}`,
			runtime_git_revision: RUNTIME_REVISION,
			segment_timings: segmentTimings,
			segments,
			size_bytes: media.size,
			speed: SPEED,
			trusted_origin_override: TRUSTED_ORIGIN_OVERRIDE,
		},
		null,
		2,
	)}\n`,
);

console.log(`RAW_VIDEO=${OUTPUT}`);
console.log(`RECORDING_MANIFEST=${RECORDING_MANIFEST}`);
console.log(`SIZE_BYTES=${media.size}`);
console.log(`PRE_ROLL_SECONDS=${preRoll}`);
console.log(
	`EXPECTED_SEGMENT_DURATION_SECONDS=${TARGET_DURATION_SECONDS * SPEED}`,
);
if (failures.length) {
	console.error(`Non-fatal scene issues:\n${failures.join("\n\n")}`);
	if (process.env.STRICT_DEMO === "1") process.exitCode = 2;
}
if (process.env.KEEP_DEMO_TEMP !== "1")
	await rm(temp, { recursive: true, force: true });
