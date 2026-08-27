import { expect, test } from "@playwright/test"

test.use({ storageState: { cookies: [], origins: [] } })

test("longitudinal complex case exposes priorities, provenance, roles, conflicts and history", async ({
  page,
}) => {
  const login = await page.request.post("/api/v1/auth/demo-login", {
    data: { persona: "clinician" },
  })
  expect(login.ok(), await login.text()).toBe(true)
  await page.goto("/patients")
  await page
    .getByRole("link", { name: "Open care note for Jordan Wong" })
    .click()

  await expect(page.getByRole("heading", { name: "Jordan Wong" })).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Back to patients" }),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Clinical review", exact: true }),
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Current priorities", exact: true }),
  ).toBeVisible()
  await expect(page.getByText(/Age \d+ · DOB/)).toBeVisible()
  await expect(page.getByText(/22-year history/)).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Current priorities" }),
  ).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Longitudinal timeline" }),
  ).toBeVisible()
  const conflictHeading = page.getByRole("heading", {
    name: "Clinical conflicts",
  })
  const prioritiesHeading = page.getByRole("heading", {
    name: "Current priorities",
  })
  const [conflictBox, prioritiesBox] = await Promise.all([
    conflictHeading.boundingBox(),
    prioritiesHeading.boundingBox(),
  ])
  expect(conflictBox).not.toBeNull()
  expect(prioritiesBox).not.toBeNull()
  expect(conflictBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(
    prioritiesBox?.y ?? Number.NEGATIVE_INFINITY,
  )

  await page.getByRole("button", { name: "Add care note" }).click()
  const noteDialog = page.getByRole("dialog", { name: "New care note" })
  await expect(noteDialog).toBeVisible()
  await noteDialog.getByRole("button", { name: "Cancel" }).click()

  await page.getByRole("button", { name: "Resolve conflict" }).click()
  const conflictDialog = page.getByRole("dialog", {
    name: "Resolve clinical conflict",
  })
  await expect(conflictDialog).toBeVisible()
  await conflictDialog.getByRole("button", { name: "Cancel" }).click()

  await page.getByRole("button", { name: "Invite patient" }).click()
  const invitationDialog = page.getByRole("dialog", {
    name: "Invite patient to My Care",
  })
  await expect(invitationDialog).toBeVisible()
  await invitationDialog.getByRole("button", { name: "Cancel" }).click()

  for (const year of ["2026", "2025", "2021", "2018", "2012", "2004"]) {
    await expect(
      page.getByRole("heading", { name: year, exact: true }),
    ).toBeVisible()
  }

  const aiPriority = page.getByRole("listitem").filter({
    hasText:
      "AI-scribed handover: hydration-plan discrepancy escalated for clinician review",
  })
  await expect(aiPriority).toBeVisible()
  await aiPriority.getByRole("button", { name: "View source" }).click()
  const sourceDialog = page.getByRole("dialog", { name: /Source details/ })
  await expect(sourceDialog).toBeVisible()
  await expect(sourceDialog.locator("mark[data-source-span]")).toHaveText(
    "the hydration discrepancy has been escalated to the acute-care clinician",
  )
  await sourceDialog.getByRole("button", { name: "Close" }).click()

  await expect(
    page.getByRole("heading", { name: "Structured clinical context" }),
  ).toBeVisible()
  await expect(page.getByText("type 2 diabetes", { exact: true })).toBeVisible()
  await expect(
    page.getByText("oral intake", { exact: true }).first(),
  ).toBeVisible()

  const conflictPanel = page.getByRole("heading", {
    name: "Clinical conflicts",
  })
  await expect(conflictPanel).toBeVisible()
  await expect(page.getByText("oral intake care_plan")).toBeVisible()
  await expect(page.getByText("Status: unresolved")).toBeVisible()

  const currentPlan = page.locator(
    'article[aria-label="Clinical note: Current pancreatitis admission plan"]',
  )
  await expect(currentPlan).toContainText("v3")
  await currentPlan.getByRole("button", { name: "Edit" }).click()
  const editDialog = page.getByRole("dialog", { name: "Edit note" })
  await expect(editDialog).toBeVisible()
  await editDialog.getByRole("button", { name: "Cancel" }).click()
  await currentPlan.getByRole("button", { name: "Change history" }).click()
  const history = page.getByRole("dialog", { name: /Change history/ })
  await expect(history.getByText(/^Version 1 ·/)).toBeVisible()
  await expect(history.getByText(/^Version 2 ·/)).toBeVisible()
  await expect(history.getByText(/^Version 3 ·/)).toBeVisible()
  await history.getByRole("button", { name: "Close" }).click()

  await expect(
    page.getByText(/Please reconcile the current oral-intake restriction/),
  ).toBeVisible()
  await expect(page.getByText("Assigned to Clinician")).toBeVisible()
  await expect(
    page.getByRole("heading", { name: "Historical retention" }),
  ).toBeVisible()
})
