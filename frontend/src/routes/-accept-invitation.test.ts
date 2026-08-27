import { describe, expect, it } from "vitest"

import { invitationAcceptanceSchema } from "./accept-invitation"

function invitation(password: string, confirmPassword = password) {
  return {
    email: "invited@example.com",
    token: "t".repeat(64),
    fullName: "Invited Clinician",
    password,
    confirmPassword,
  }
}

describe("invitation password validation", () => {
  it("enforces the 16–200 character boundary", () => {
    expect(
      invitationAcceptanceSchema.safeParse(invitation("p".repeat(15))).success,
    ).toBe(false)
    expect(
      invitationAcceptanceSchema.safeParse(invitation("p".repeat(16))).success,
    ).toBe(true)
    expect(
      invitationAcceptanceSchema.safeParse(invitation("p".repeat(200))).success,
    ).toBe(true)
    expect(
      invitationAcceptanceSchema.safeParse(invitation("p".repeat(201))).success,
    ).toBe(false)
  })

  it("requires matching confirmation without trimming the password", () => {
    const spaced = "  long passphrase  "
    const parsed = invitationAcceptanceSchema.parse(invitation(spaced))
    expect(parsed.password).toBe(spaced)
    expect(
      invitationAcceptanceSchema.safeParse(
        invitation("matching password", "different password"),
      ).success,
    ).toBe(false)
  })
})
