import { describe, expect, it } from "vitest"

import { createCanonicalAnchor, locateExactQuote } from "./anchors"

describe("canonical care-note anchors", () => {
  it("uses Unicode code-point offsets for Chinese and emoji", () => {
    const content = "患者🙂说：今天好一些。"
    const start = content.indexOf("今天")
    const anchor = createCanonicalAnchor(content, start, start + "今天".length)

    expect(anchor).toMatchObject({
      start_offset: 5,
      end_offset: 7,
      exact_quote: "今天",
      prefix: "患者🙂说：",
      suffix: "好一些。",
    })
  })

  it("preserves CRLF and decomposed Unicode from the immutable source", () => {
    const content = "Plan\r\ncafe\u0301 review"
    const start = content.indexOf("cafe")
    const anchor = createCanonicalAnchor(content, start, start + 5)

    expect(anchor.exact_quote).toBe("cafe\u0301")
    expect(anchor.start_offset).toBe(6)
    expect(anchor.end_offset).toBe(11)
    expect(anchor.prefix).toBe("Plan\r\n")
  })

  it("anchors the requested duplicate quote occurrence", () => {
    const anchor = locateExactQuote("pain improved; pain returned", "pain", 1)

    expect(anchor.start_offset).toBe(15)
    expect(anchor.exact_quote).toBe("pain")
    expect(anchor.prefix).toContain("pain improved; ")
    expect(anchor.suffix).toBe(" returned")
  })
})
