import { describe, expect, it } from "vitest"

import {
  clinicLoginSchema,
  normalizeClinicCode,
  normalizeEmail,
} from "./ClinicLoginForm"

const validLogin = {
  clinicCode: "NIGHTINGALE",
  email: "care@example.com",
  password: "a secure passphrase",
}

describe("clinic sign-in validation", () => {
  it("normalizes user-facing clinic codes and email addresses", () => {
    expect(normalizeClinicCode("NtuHealth")).toBe("NTUHEALTH")
    expect(normalizeEmail(" Care.Team@Example.COM ")).toBe(
      "care.team@example.com",
    )
  })

  it.each(["AB", "ABCDEFGHIJKLM", "NTU-01", "NTU HEALTH", "诊所", "ABC1"])(
    "rejects an invalid clinic code: %s",
    (clinicCode) => {
      expect(
        clinicLoginSchema.safeParse({ ...validLogin, clinicCode }).success,
      ).toBe(false)
    },
  )

  it.each(["ABC", "NIGHTINGALE"])(
    "accepts a clinic code at the supported boundaries: %s",
    (clinicCode) => {
      expect(
        clinicLoginSchema.safeParse({ ...validLogin, clinicCode }).success,
      ).toBe(true)
    },
  )

  it("preserves password whitespace", () => {
    const password = "  a secure passphrase  "
    const parsed = clinicLoginSchema.parse({ ...validLogin, password })
    expect(parsed.password).toBe(password)
  })

  it("accepts existing account passwords from 1 through 200 characters", () => {
    expect(
      clinicLoginSchema.safeParse({ ...validLogin, password: "" }).success,
    ).toBe(false)
    expect(
      clinicLoginSchema.safeParse({ ...validLogin, password: "p" }).success,
    ).toBe(true)
    expect(
      clinicLoginSchema.safeParse({
        ...validLogin,
        password: "p".repeat(200),
      }).success,
    ).toBe(true)
    expect(
      clinicLoginSchema.safeParse({
        ...validLogin,
        password: "p".repeat(201),
      }).success,
    ).toBe(false)
  })
})
