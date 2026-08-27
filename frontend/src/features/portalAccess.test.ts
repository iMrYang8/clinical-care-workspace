import { describe, expect, it } from "vitest"

import { portalRedirectForRole, roleHome } from "./portalAccess"

describe("portal access decisions", () => {
  it.each(["staff", "clinician", "admin"] as const)(
    "keeps %s in the clinical portal",
    (role) => {
      expect(portalRedirectForRole(role, "clinical")).toBeNull()
    },
  )

  it("redirects a patient before the clinical shell renders", () => {
    expect(portalRedirectForRole("patient", "clinical")).toBe(
      "/patient/my-care",
    )
  })

  it("keeps worker service accounts out of the interactive workspace", () => {
    expect(roleHome("worker")).toBe("/login")
    expect(portalRedirectForRole("worker", "clinical")).toBe("/login")
  })

  it.each(["staff", "clinician", "admin", "worker"] as const)(
    "redirects %s away from the patient portal",
    (role) => {
      expect(portalRedirectForRole(role, "patient")).toBe(roleHome(role))
    },
  )

  it("keeps a patient in the patient portal", () => {
    expect(portalRedirectForRole("patient", "patient")).toBeNull()
  })
})
