import { describe, expect, it } from "vitest"

import { authoritativeSupportState } from "./provenanceSupport"

describe("authoritative provenance support state", () => {
  it("uses immutable version identity to fail stale current hints closed", () => {
    expect(authoritativeSupportState("current", "current", false)).toBe(
      "historical",
    )
  })

  it("preserves authoritative superseded state", () => {
    expect(authoritativeSupportState("superseded", "historical", true)).toBe(
      "superseded",
    )
    expect(authoritativeSupportState("historical", "superseded", true)).toBe(
      "superseded",
    )
  })

  it("uses current only when the immutable source version is current", () => {
    expect(authoritativeSupportState(undefined, undefined, true)).toBe(
      "current",
    )
    expect(authoritativeSupportState(undefined, "historical", true)).toBe(
      "historical",
    )
  })
})
