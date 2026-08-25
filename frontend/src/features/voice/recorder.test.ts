import { describe, expect, it } from "vitest"

import { analyzeInputLevel, preferredRecorderMimeType } from "./recorder"

describe("voice recorder capability helpers", () => {
  it("prefers opus and falls back to Safari mp4", () => {
    expect(preferredRecorderMimeType(() => true)).toBe("audio/webm;codecs=opus")
    expect(preferredRecorderMimeType((type) => type === "audio/mp4")).toBe(
      "audio/mp4",
    )
    expect(preferredRecorderMimeType(() => false)).toBe("")
  })

  it("surfaces clipping and low-level noise review signals", () => {
    expect(analyzeInputLevel(Uint8Array.from([0, 128, 255])).clipping).toBe(
      true,
    )
    expect(analyzeInputLevel(Uint8Array.from([128, 129, 127])).noise).toBe(true)
  })
})
