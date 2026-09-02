import { describe, expect, it } from "vitest"

import { allergyCategoryLabel } from "./conflictPresentation"

describe("conflict allergy category presentation", () => {
  it.each(["drug", "food", "environmental"] as const)(
    "renders the audited %s category",
    (category) => {
      expect(allergyCategoryLabel(category)).toBe(category)
    },
  )

  it("renders missing category evidence as unavailable", () => {
    expect(allergyCategoryLabel(null)).toBe("unavailable")
    expect(allergyCategoryLabel(undefined)).toBe("unavailable")
  })
})
