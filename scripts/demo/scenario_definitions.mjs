/**
 * Per-scenario demo definitions.
 *
 * One entry per clinic scenario that the build actually survives. Each entry
 * owns its own steps and its own `proofs` — the strings that must be visible on
 * screen for the scenario to be considered demonstrated. The check phase and
 * the recording phase run the identical `steps`, so a green check is evidence
 * about the same footage that gets shipped.
 *
 * Conventions for `steps(ctx)`:
 *   ctx.page      primary page
 *   ctx.context   primary browser context
 *   ctx.base      base URL
 *   ctx.openSecondContext()  second browser context, for two-user scenarios
 *   ctx.expectText(locator-or-page, text)  assert + hold, records the proof
 *   ctx.beat(seconds)        pause so a viewer can read the screen
 */

import {
  closeVisibleDialog,
  highlight,
  login,
  moveTo,
  openPatient,
  sleep,
} from "./scenario_chrome.mjs"

const JORDAN = "Jordan Wong"
const ALEX = "Alex Tan"
const PRIYA = "Priya Nair"

export const scenarios = [
  {
    id: "02-clinic-isolation",
    number: 2,
    title: "One line changes in a route handler",
    role: "Scenario 2 · Tenant isolation",
    summary:
      "The same search runs in two clinics. Clinic A's patients do not exist for clinic B, and the platform workspace is read-only across both.",
    proofs: [
      "0 matching patient records",
      "Read-only clinic view",
    ],
    async steps(ctx) {
      const { page, base } = ctx

      await login(page, ctx.context, base, "staff", "/patients")
      await ctx.beat(1.2)

      const search = page.getByPlaceholder(
        "Search by patient name, MRN, or date of birth",
      )
      await moveTo(page, search, "Search for HFC2024018", { click: true })
      await search.fill("HFC2024018")
      await page.waitForLoadState("networkidle").catch(() => {})
      await ctx.beat(1.4)

      // Clinic A cannot see a clinic B patient.
      await ctx.expectText(page, "0 matching patient records")
      await ctx.beat(1.8)

      // Same query, other clinic.
      await login(page, ctx.context, base, "other_staff", "/patients")
      await ctx.beat(1.0)
      const search2 = page.getByPlaceholder(
        "Search by patient name, MRN, or date of birth",
      )
      await moveTo(page, search2, "The same search, clinic B", { click: true })
      await search2.fill("HFC2024018")
      await page.waitForLoadState("networkidle").catch(() => {})
      await ctx.beat(1.2)
      const priyaLink = page
        .getByRole("link", { name: new RegExp(PRIYA, "i") })
        .first()
      await highlight(page, priyaLink, "Visible only to her own clinic", 2.6)

      // Platform oversight is read-only across both clinics.
      await page.goto(`${base}/platform/login`, {
        waitUntil: "domcontentloaded",
      })
      await page
        .locator("#platform-email")
        .fill("platform.admin@nightingale.example")
      await page.locator("#platform-password").fill("local-platform-owner-only")
      await page.getByRole("button", { name: "Sign in" }).click()
      await page.waitForURL(/\/platform/, { timeout: 20000 })
      await page.waitForLoadState("networkidle").catch(() => {})
      await ctx.beat(1.6)

      const clinicCard = page
        .getByRole("link", { name: /Nightingale Clinic/i })
        .first()
      await moveTo(page, clinicCard, "Open clinic (read-only)", { click: true })
      await page.waitForLoadState("networkidle").catch(() => {})
      await ctx.expectText(page, "Read-only clinic view")
      await ctx.beat(2.4)
    },
  },

  {
    id: "05-clinic-b-onboarding",
    number: 5,
    title: "Clinic B onboards next Monday",
    role: "Scenario 5 · Platform administrator",
    summary:
      "A second clinic is configuration and data only. Preflight names each unmet requirement by reason code, then flips to Ready and the action becomes Create clinic.",
    proofs: ["Action required", "Ready"],
    async steps(ctx) {
      const { page, base } = ctx

      await page.goto(`${base}/platform/login`, {
        waitUntil: "domcontentloaded",
      })
      await page
        .locator("#platform-email")
        .fill("platform.admin@nightingale.example")
      await page.locator("#platform-password").fill("local-platform-owner-only")
      await page.getByRole("button", { name: "Sign in" }).click()
      await page.waitForURL(/\/platform/, { timeout: 20000 })
      await page.waitForLoadState("networkidle").catch(() => {})
      await ctx.beat(1.2)

      const onboard = page.getByRole("button", { name: /Onboard clinic/i })
      await moveTo(page, onboard, "Onboard a clinic", { click: true })
      await ctx.beat(1.2)

      const dialog = page.getByRole("dialog").filter({ visible: true }).last()
      // The clinic code field enforces [A-Za-z]{3,12}; digits would make the
      // browser block submission silently and the preflight would never run.
      const letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
      const code = `DEMO${Array.from(
        { length: 4 },
        () => letters[Math.floor(Math.random() * letters.length)],
      ).join("")}`
      await dialog.getByLabel("Clinic code").fill(code)
      await dialog.getByLabel("Clinic name").fill("Demo Harbour Clinic")
      await dialog.getByLabel("Clinic URL slug").fill(code.toLowerCase())
      await dialog
        .getByLabel("Initial staff email")
        .fill("clinic.admin@demo.example")
      await dialog.getByLabel("Initial staff name").fill("Demo Clinic Admin")
      await ctx.beat(1.4)

      // Deliberately unmet requirement: turn the calibration gate off.
      const calibration = dialog.getByLabel("Calibration required")
      if (await calibration.isChecked().catch(() => false)) {
        await moveTo(page, calibration, "Turn the calibration gate off", {
          click: true,
        })
      }
      await ctx.beat(0.8)

      const submit = dialog.getByRole("button", { name: /Run preflight/i })
      await moveTo(page, submit, "Run preflight", { click: true, after: 1600 })
      await ctx.beat(1.4)

      // Preflight names the unmet requirement rather than failing generically.
      await ctx.expectText(dialog, "Action required")
      await ctx.beat(2.6)

      // Meet the requirement and re-run.
      await moveTo(page, calibration, "Restore the calibration gate", {
        click: true,
      })
      await ctx.beat(0.6)
      const rerun = dialog.getByRole("button", { name: /Run preflight/i })
      await moveTo(page, rerun, "Run preflight again", {
        click: true,
        after: 1600,
      })
      await ctx.beat(1.2)
      await ctx.expectText(dialog, "Ready")
      await ctx.beat(1.6)

      // The action itself changes once every check passes.
      const create = dialog.getByRole("button", { name: /Create clinic/i })
      await highlight(page, create, "Now: Create clinic", 2.8)
      await closeVisibleDialog(page)
    },
  },

  {
    id: "14-meaningful-numbers",
    number: 14,
    title: "A number that means something",
    role: "Scenario 14 · Clinician",
    summary:
      "Confidence is not decoration. An unqualified item says so, explains how it could be wrong, and stays in a protected queue that cannot be shared with the patient.",
    proofs: ["Why this decision?", "Needs clinical review"],
    async steps(ctx) {
      const { page, base } = ctx

      await login(page, ctx.context, base, "clinician", "/patients")
      await openPatient(page, base, JORDAN)
      await ctx.beat(1.6)

      const priorities = page
        .getByRole("heading", { name: "Current priorities" })
        .first()
      await highlight(page, priorities, "Current priorities", 2.2)

      // The confidence badge states the honest state instead of a bare number.
      const confidenceBadge = page
        .getByText(/Confidence (not applicable|unavailable)/i)
        .first()
      await highlight(page, confidenceBadge, "Confidence is qualified", 2.8)

      // The disclosure explains meaning, falsifiability, and consequence.
      const why = page.getByText("Why this decision?").first()
      await moveTo(page, why, "Why this decision?", { click: true, after: 900 })
      await ctx.expectText(page, "Why this decision?")
      await ctx.beat(3.4)

      const howWrong = page.getByText(/How could it be wrong/i).first()
      if (await howWrong.isVisible().catch(() => false)) {
        await highlight(page, howWrong, "Falsifiability", 3.0)
      }
      const whenWrong = page.getByText(/What happens when it is wrong/i).first()
      if (await whenWrong.isVisible().catch(() => false)) {
        await highlight(page, whenWrong, "Consequence", 3.0)
      }

      // The protected queue is uncapped and cannot reach the patient.
      const reviewQueue = page
        .getByRole("heading", { name: "Needs clinical review" })
        .first()
      await moveTo(page, reviewQueue, "Protected review queue", { hold: 800 })
      await ctx.expectText(page, "Needs clinical review")
      await ctx.beat(3.2)
    },
  },

  {
    id: "12-medication-gate",
    number: 12,
    title: "Wrong dosage in a patient summary",
    role: "Scenario 12 · Clinician",
    summary:
      "Two source-linked medication instructions disagree. The conflict is high severity, neither source wins automatically, and publication to the patient stays blocked until a clinician records a correction.",
    proofs: ["Clinical conflicts", "Status: unresolved"],
    async steps(ctx) {
      const { page, base } = ctx

      await login(page, ctx.context, base, "other_staff", "/patients")
      await openPatient(page, base, PRIYA)
      await ctx.beat(1.6)

      const conflicts = page
        .getByRole("heading", { name: /Clinical conflicts/i })
        .first()
      await moveTo(page, conflicts, "Clinical conflicts", { hold: 900 })
      await ctx.expectText(page, "Clinical conflicts")
      await ctx.beat(2.0)

      // Both sides stay visible and addressable.
      await ctx.expectText(page, "Status: unresolved")
      const status = page.getByText(/Status: unresolved/i).first()
      await highlight(page, status, "Unresolved and high severity", 2.6)

      const firstSource = page
        .getByRole("button", { name: /View first source/i })
        .first()
      await moveTo(page, firstSource, "View the exact first source", {
        click: true,
        after: 1200,
      })
      await ctx.beat(3.0)
      await closeVisibleDialog(page)
      await ctx.beat(0.8)

      const secondSource = page
        .getByRole("button", { name: /View conflicting source/i })
        .first()
      await moveTo(page, secondSource, "View the conflicting source", {
        click: true,
        after: 1200,
      })
      await ctx.beat(3.0)
      await closeVisibleDialog(page)

      // Staff cannot dismiss a high-risk conflict.
      const staffBlock = page
        .getByText(/High-risk conflicts cannot be dismissed/i)
        .first()
      if (await staffBlock.isVisible().catch(() => false)) {
        await highlight(page, staffBlock, "Staff cannot dismiss this", 3.0)
      }
      await ctx.beat(1.4)
    },
  },

  {
    id: "13-allergy-vs-nkda",
    number: 13,
    title: "The nurse recorded penicillin allergy, the patient says none",
    role: "Scenario 13 · Care staff, then patient, then clinician",
    summary:
      "A nurse documents a named allergy; the patient denies any allergy in her own portal. Both statements are preserved, the conflict is critical, each side names who asserted it, and neither wins automatically.",
    proofs: [
      "Status: unresolved",
      "does not override an active named allergy",
    ],
    async steps(ctx) {
      const { page, base } = ctx

      // 1. The nurse documents a named allergy.
      await login(page, ctx.context, base, "staff", "/patients")
      await openPatient(page, base, ALEX)
      await ctx.beat(1.2)

      const addNote = page.getByRole("button", { name: /Add care note/i }).first()
      await moveTo(page, addNote, "Nurse documents the allergy", {
        click: true,
        after: 900,
      })
      const noteDialog = page.getByRole("dialog").filter({ visible: true }).last()
      await noteDialog.locator("#new-entry-title").fill("Allergy history")
      const body = noteDialog.locator("#new-entry-content")
      await moveTo(page, body, "A named allergy", { click: true })
      await body.fill("Patient reports penicillin allergy.")
      await ctx.beat(1.6)
      await moveTo(
        page,
        noteDialog.getByRole("button", { name: "Create note" }),
        "Save",
        { click: true, after: 1200 },
      )
      await page.waitForLoadState("networkidle").catch(() => {})
      await noteDialog.waitFor({ state: "hidden", timeout: 20000 }).catch(() => {})
      await closeVisibleDialog(page)
      await ctx.beat(1.2)

      // 2. The patient denies it, in her own words, in her own portal.
      await login(page, ctx.context, base, "patient", "/patient/my-care")
      await ctx.beat(1.4)
      const addInsight = page
        .getByRole("button", { name: /Add my insight/i })
        .first()
      await moveTo(page, addInsight, "The patient's own channel", {
        click: true,
        after: 900,
      })
      const insightDialog = page
        .getByRole("dialog")
        .filter({ visible: true })
        .last()
      await insightDialog
        .locator("#patient-insight-title")
        .fill("About my allergies")
      const insightBody = insightDialog.locator("#patient-insight-content")
      await moveTo(page, insightBody, "A blanket denial", { click: true })
      await insightBody.fill("I have no known drug allergies. NKDA.")
      await ctx.beat(1.6)
      await moveTo(
        page,
        insightDialog.getByRole("button", { name: "Add to My Care" }),
        "Submit to the care team",
        { click: true, after: 1400 },
      )
      await page.waitForLoadState("networkidle").catch(() => {})
      await insightDialog
        .waitFor({ state: "hidden", timeout: 20000 })
        .catch(() => {})
      await closeVisibleDialog(page)
      await ctx.beat(1.6)

      // 3. The clinician sees both, attributed, with neither overwritten.
      await login(page, ctx.context, base, "clinician", "/patients")
      await openPatient(page, base, ALEX)
      await ctx.beat(1.6)

      const conflictsHeading = page
        .getByRole("heading", { name: /Clinical conflicts/i })
        .first()
      await moveTo(page, conflictsHeading, "Both statements are kept", {
        hold: 900,
      })
      await ctx.expectText(page, "Status: unresolved")
      await ctx.beat(2.2)

      const staffSide = page.getByText(/Source: staff/i).first()
      await highlight(page, staffSide, "Nurse-documented", 3.0)
      const patientSide = page.getByText(/Source: patient/i).first()
      await highlight(page, patientSide, "Patient-reported", 3.0)

      await ctx.expectText(page, "does not override an active named allergy")
      const precedence = page
        .getByText(/does not override an active named allergy/i)
        .first()
      await highlight(page, precedence, "Neither source wins", 4.2)
      await ctx.beat(1.6)
    },
  },

  {
    id: "10-concurrent-edits",
    number: 10,
    title: "Two people open the same note at 09:14",
    role: "Scenario 10 · Two care staff sessions",
    summary:
      "Two sessions edit one saved version. The second save is refused rather than silently overwriting, and the losing draft is preserved beside the latest saved note.",
    proofs: ["Version conflict", "No automatic merge was applied"],
    async steps(ctx) {
      const { page, base } = ctx

      await login(page, ctx.context, base, "staff", "/patients")
      await openPatient(page, base, JORDAN)
      await ctx.beat(1.2)

      // Session B opens the same entry.
      const second = await ctx.openSecondContext()
      await login(second.page, second.context, base, "staff", "/patients")
      await openPatient(second.page, base, JORDAN)

      const editA = page.getByRole("button", { name: "Edit" }).first()
      await moveTo(page, editA, "Session A opens the note", {
        click: true,
        after: 900,
      })
      await ctx.beat(1.0)

      const editorA = page.getByRole("textbox").filter({ visible: true }).last()
      await editorA.click()
      await editorA.type(" Session A addition.", { delay: 45 })
      await ctx.beat(1.2)

      // Session B edits and saves first.
      const editB = second.page.getByRole("button", { name: "Edit" }).first()
      await editB.click()
      await second.page.waitForTimeout(600)
      const editorB = second.page
        .getByRole("textbox")
        .filter({ visible: true })
        .last()
      await editorB.click()
      await editorB.type(" Session B addition.", { delay: 20 })
      const saveB = second.page
        .getByRole("button", { name: /^Save/i })
        .first()
      await saveB.click()
      await second.page.waitForLoadState("networkidle").catch(() => {})
      await ctx.beat(1.8)

      // Session A is told before it saves: the SSE stream reports the new
      // version and the editor raises the conflict while the draft is still
      // open. Only if that proactive path did not fire do we force it by
      // saving a stale draft.
      const conflictTitle = page.getByText("Version conflict").first()
      const appearedOnItsOwn = await conflictTitle
        .waitFor({ state: "visible", timeout: 20000 })
        .then(() => true)
        .catch(() => false)
      if (!appearedOnItsOwn) {
        const saveA = page.getByRole("button", { name: /^Save/i }).first()
        await moveTo(page, saveA, "Session A saves a stale draft", {
          click: true,
          after: 2000,
        })
      }
      await ctx.expectText(page, "Version conflict")
      await highlight(page, conflictTitle, "Nothing was overwritten", 3.0)
      await ctx.beat(1.6)

      const loadLatest = page
        .getByRole("button", { name: /Load latest and reconcile/i })
        .first()
      if (await loadLatest.isVisible().catch(() => false)) {
        await moveTo(page, loadLatest, "Load latest and reconcile", {
          click: true,
          after: 1800,
        })
      }
      await ctx.expectText(page, "No automatic merge was applied")
      await ctx.beat(3.6)
    },
  },

  {
    id: "09-provider-outage",
    number: 9,
    title: "The provider returns 503 for an hour",
    role: "Scenario 9 · Clinician during an AI outage",
    summary:
      "With remote text processing unavailable, stored priorities stay readable and are explicitly labelled with their age. The clinician never sees an empty card.",
    proofs: ["stored priorities remain visible", "Last generated"],
    /**
     * There is no seeded outage fixture, no env flag, and no test-only route
     * that opens a provider circuit: the only writer is a real provider
     * failure. The take therefore opens the circuit directly, exactly as
     * `_record_provider_failure` would, and closes it again afterwards.
     */
    setup: {
      sql:
        "INSERT INTO provider_circuit_states " +
        "(id, clinic_id, provider, capability, state, consecutive_failures, " +
        " last_error_class, opened_at, next_probe_at, updated_at) " +
        "SELECT gen_random_uuid(), id, 'openai', 'clinical_text', 'open', 3, " +
        "'provider_http_503', now() - interval '60 minutes', " +
        "now() + interval '30 minutes', now() FROM clinics " +
        "WHERE code = 'NIGHTINGALE' " +
        "ON CONFLICT (clinic_id, provider, capability) DO UPDATE SET " +
        "state = 'open', consecutive_failures = 3, " +
        "last_error_class = 'provider_http_503', " +
        "opened_at = now() - interval '60 minutes', " +
        "next_probe_at = now() + interval '30 minutes', updated_at = now()",
      teardown:
        "UPDATE provider_circuit_states SET state = 'closed', " +
        "consecutive_failures = 0, next_probe_at = NULL, updated_at = now() " +
        "WHERE provider = 'openai' AND capability = 'clinical_text'",
    },
    async steps(ctx) {
      const { page, base } = ctx

      await login(page, ctx.context, base, "clinician", "/patients")
      await openPatient(page, base, JORDAN)
      await ctx.beat(1.6)

      const banner = page
        .getByText(/stored priorities remain visible/i)
        .first()
      await moveTo(page, banner, "AI is down; the record is not", { hold: 900 })
      await ctx.expectText(page, "stored priorities remain visible")
      await ctx.beat(3.0)

      await ctx.expectText(page, "Last generated")
      await ctx.beat(2.4)

      // The priorities themselves are still there and still usable.
      const priorities = page
        .getByRole("heading", { name: "Current priorities" })
        .first()
      await highlight(page, priorities, "Still readable, clearly aged", 3.0)
    },
  },
]

export const scenarioById = new Map(scenarios.map((item) => [item.id, item]))

export { sleep }
