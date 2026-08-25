import { fireEvent, render, screen } from "@testing-library/react"
import { describe, expect, it, vi } from "vitest"

import type { GlanceCard } from "@/client"
import { GlanceTopCard } from "./GlanceTopCard"

const cards: GlanceCard[] = Array.from({ length: 7 }, (_, index) => ({
  highlight_id: `highlight-${index}`,
  label: `Care signal ${index}`,
  critical: index === 0,
  pinned: index === 1,
  risk_reason: index === 0 ? "critical" : "clinician_accepted",
  provenance_pointer_id: `pointer-${index}`,
}))

describe("GlanceTopCard", () => {
  it("shows at most five ranked cards with risk, status, and source actions", () => {
    const onSource = vi.fn()
    render(<GlanceTopCard cards={cards} onSource={onSource} />)

    expect(screen.getAllByRole("listitem")).toHaveLength(5)
    expect(screen.queryByText("Care signal 5")).not.toBeInTheDocument()
    expect(screen.getByText("Critical risk")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: "View source" })).toHaveLength(
      5,
    )

    fireEvent.click(screen.getAllByRole("button", { name: "View source" })[0])
    expect(onSource).toHaveBeenCalledWith(cards[0])
  })
})
