/**
 * Shared on-screen chrome and navigation helpers for per-scenario recordings.
 *
 * These are extracted from scripts/record_final_demo_en.mjs, which cannot be
 * imported because it executes its whole 13-segment film at module scope.
 * Behaviour is intentionally identical so both films look the same.
 *
 * Everything here is presentation-only. No helper asserts clinical meaning;
 * scenario modules own their own proofs.
 */

export const VIEWPORT = { width: 1440, height: 900 }

/** Recording pace. 1 is real time; the check phase runs at 0 to skip holds. */
let speed = 1
export function setSpeed(value) {
  speed = Number.isFinite(value) && value >= 0 ? value : 1
}

export const sleep = (ms) =>
  new Promise((done) => setTimeout(done, Math.max(1, ms * speed)))

let activeChapter = null

export async function ensureDemoChrome(page) {
  await page.evaluate(() => {
    if (!document.querySelector("#nightingale-demo-cursor")) {
      const cursor = document.createElement("div")
      cursor.id = "nightingale-demo-cursor"
      cursor.innerHTML =
        '<svg width="34" height="42" viewBox="0 0 34 42" aria-hidden="true">' +
        '<path d="M3 2 L3 31 L11 24 L17 39 L23 36 L17 22 L29 22 Z" ' +
        'fill="#0f766e" stroke="white" stroke-width="2.5" stroke-linejoin="round"/>' +
        "</svg><span></span>"
      Object.assign(cursor.style, {
        left: "80px",
        top: "80px",
        position: "fixed",
        zIndex: "2147483647",
        pointerEvents: "none",
        filter: "drop-shadow(0 5px 7px rgba(0,0,0,.35))",
        transition:
          "left 620ms cubic-bezier(.2,.8,.2,1), top 620ms cubic-bezier(.2,.8,.2,1)",
      })
      const label = cursor.querySelector("span")
      const style = document.createElement("style")
      style.id = "nightingale-demo-cursor-style"
      style.textContent =
        "#nightingale-demo-cursor span::after{content:attr(data-label)}"
      document.head.append(style)
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
      })
      document.body.append(cursor)
    }
    if (!document.querySelector("#nightingale-demo-chapter")) {
      const chapter = document.createElement("div")
      chapter.id = "nightingale-demo-chapter"
      chapter.innerHTML = "<div data-role></div><strong data-title></strong>"
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
        maxWidth: "560px",
        boxShadow: "0 14px 42px rgba(2,6,23,.25)",
        fontFamily: "Inter,system-ui,sans-serif",
      })
      Object.assign(chapter.querySelector("[data-role]").style, {
        color: "#99f6e4",
        fontSize: "11px",
        fontWeight: "800",
        letterSpacing: ".14em",
        textTransform: "uppercase",
      })
      Object.assign(chapter.querySelector("strong").style, {
        display: "block",
        marginTop: "3px",
        fontSize: "17px",
        lineHeight: "1.25",
      })
      document.body.append(chapter)
    }
  })
  if (activeChapter) await paintChapter(page, activeChapter)
}

async function paintChapter(page, chapter) {
  await page.evaluate((value) => {
    const node = document.querySelector("#nightingale-demo-chapter")
    if (!node) return
    node.querySelector("[data-role]").textContent = value.role
    node.querySelector("[data-title]").textContent = value.title
  }, chapter)
}

/**
 * Record the chapter without touching a page.
 *
 * The recorder must not navigate on its own before the scenario starts: landing
 * on one auth realm and then switching to another trips the app's local-cleanup
 * session boundary and blocks the UI. The card is painted by ensureDemoChrome
 * on the first real page the scenario opens.
 */
export function stageChapter({ role, title }) {
  activeChapter = { role, title }
}

/** Pin the chapter card for the whole scenario. */
export async function setChapter(page, { role, title }) {
  activeChapter = { role, title }
  await ensureDemoChrome(page)
  await paintChapter(page, activeChapter)
}

export function resetChapter() {
  activeChapter = null
}

/**
 * Move the synthetic cursor onto an element, outline it, optionally click.
 * `hold` controls how long the highlight rests before the click.
 */
export async function moveTo(page, locator, label = "", options = {}) {
  const timeout = options.timeout ?? 30000
  await locator.waitFor({ state: "visible", timeout })
  await locator.scrollIntoViewIfNeeded()
  await sleep(250)
  const box = await locator.boundingBox()
  if (!box) throw new Error(`No bounding box for ${label || "element"}`)
  await ensureDemoChrome(page)
  await page.evaluate(
    ({ x, y, label, width, height }) => {
      const cursor = document.querySelector("#nightingale-demo-cursor")
      if (!cursor) return
      cursor.style.left = `${x}px`
      cursor.style.top = `${y}px`
      const bubble = cursor.querySelector("span")
      bubble.textContent = ""
      bubble.dataset.label = label
      bubble.style.opacity = label ? "1" : "0"
      void width
      void height
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
      width: box.width,
      height: box.height,
    },
  )
  await locator.evaluate((node) => {
    node.dataset.demoPointed = "true"
    node.style.transition = "box-shadow 180ms ease, outline 180ms ease"
    node.style.outline = "3px solid rgba(13,148,136,.42)"
    node.style.outlineOffset = "4px"
  })
  await sleep(options.hold ?? 850)
  if (options.click) {
    await page.evaluate(() => {
      const cursor = document.querySelector("#nightingale-demo-cursor")
      cursor?.animate(
        [
          { transform: "scale(1)" },
          { transform: "scale(.72)" },
          { transform: "scale(1)" },
        ],
        { duration: 360 },
      )
    })
    await locator.click()
    await sleep(options.after ?? 650)
  }
  await page
    .evaluate(() => {
      document.querySelectorAll("[data-demo-pointed]").forEach((node) => {
        node.style.outline = ""
        node.style.outlineOffset = ""
        delete node.dataset.demoPointed
      })
    })
    .catch(() => {})
}

/**
 * Hold on a locator without clicking, so a reviewer can read the evidence.
 * Used for the proof beats: the thing on screen that makes the scenario true.
 */
export async function highlight(page, locator, label, seconds = 2.6) {
  await moveTo(page, locator, label, { hold: seconds * 1000 })
}

/**
 * Sign in through the development demo-login endpoint.
 *
 * The about:blank + clearCookies ordering is load-bearing: an in-flight auth or
 * SSE request from the previous React tree can otherwise observe the
 * empty-cookie window and clear the newly issued persona cookie.
 */
export async function login(page, context, base, persona, destination) {
  await page.goto("about:blank")
  await context.clearCookies()
  const response = await page.request.post(`${base}/api/v1/auth/demo-login`, {
    data: { persona },
  })
  if (!response.ok())
    throw new Error(`demo-login ${persona} failed: ${response.status()}`)
  await page.goto(`${base}${destination}`, { waitUntil: "domcontentloaded" })
  await page.waitForLoadState("networkidle").catch(() => {})
  await ensureDemoChrome(page).catch(() => {})
}

export async function openPatient(page, base, name) {
  if (!/\/patients\/?$/.test(new URL(page.url()).pathname)) {
    await page.goto(`${base}/patients`, { waitUntil: "domcontentloaded" })
  }
  const link = page.getByRole("link", { name: `Open care note for ${name}` })
  await link.waitFor({ state: "visible", timeout: 20000 })
  await moveTo(page, link, name, { click: true, hold: 650 })
  await page
    .getByRole("heading", { name })
    .waitFor({ state: "visible", timeout: 20000 })
  await page.waitForLoadState("networkidle").catch(() => {})
}

export async function closeVisibleDialog(page) {
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const visibleDialogs = page.getByRole("dialog").filter({ visible: true })
    const count = await visibleDialogs.count()
    if (count === 0) return
    const dialog = visibleDialogs.last()
    const chromeClose = dialog.locator('[data-slot="dialog-close"]').last()
    const semanticClose = dialog
      .getByRole("button", { name: /Close|Cancel|Keep shared/ })
      .first()
    const close = (await chromeClose.isVisible().catch(() => false))
      ? chromeClose
      : semanticClose
    if (await close.isVisible().catch(() => false)) {
      await moveTo(page, close, "Close", { click: true, hold: 300 })
    } else {
      await page.keyboard.press("Escape")
    }
    await dialog.waitFor({ state: "hidden", timeout: 5000 }).catch(() => {})
  }
}
