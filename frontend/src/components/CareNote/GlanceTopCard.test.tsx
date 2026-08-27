import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ClinicalGlanceCard } from "@/client"
import { GlanceTopCard } from "./GlanceTopCard"

const ready: ClinicalGlanceCard = {
  highlight_id: "11111111-1111-4111-8111-111111111111",
  label: "Reviewed medication plan",
  critical: false,
  pinned: false,
  risk_reason: "recency",
  provenance_pointer_id: "22222222-2222-4222-8222-222222222222",
  score_components: {},
  review_state: "ready",
  risk: {
    effective: "standard",
    floor: "standard",
    rule_ids: [],
    rule_version: "clinical-risk-rules-v2",
  },
  confidence: { band: "not_applicable" },
  importance: {
    score: 0.5,
    protected: false,
    components: { final: 0.5, learned: 0.08 },
  },
}

const abstained: ClinicalGlanceCard = {
  ...ready,
  highlight_id: "33333333-3333-4333-8333-333333333333",
  label: "Unverified severe allergy",
  critical: true,
  review_state: "abstained",
  confidence: { band: "unavailable" },
  abstention_reason: "CALIBRATION_UNAVAILABLE",
}

afterEach(() => vi.useRealTimers())

describe("trustworthy Current priorities", () => {
  it("separates abstained critical information from ready priorities", () => {
    render(
      <GlanceTopCard
        cards={[ready]}
        onSource={vi.fn()}
        reviewCards={[abstained]}
      />,
    )
    expect(screen.getByText("Reviewed medication plan")).toBeInTheDocument()
    expect(screen.getByText("Needs clinical review")).toBeInTheDocument()
    expect(screen.getByText("Unverified severe allergy")).toBeInTheDocument()
    expect(screen.getByText("Critical · unverified")).toBeInTheDocument()
  })

  it("explains the clinic-level learning adjustment without claiming a personal profile", () => {
    render(<GlanceTopCard cards={[ready]} onSource={vi.fn()} />)
    fireEvent.click(screen.getByText("Why this decision?"))
    expect(screen.getByText(/rule-based score of 0\.420/)).toBeInTheDocument()
    expect(
      screen.getByText(/clinic feedback adjustment of \+0\.080/),
    ).toBeInTheDocument()
    expect(screen.getByText(/it is not a personal profile/)).toBeInTheDocument()
  })

  it("requires a dismissal reason and records an impression after two seconds", async () => {
    vi.useFakeTimers()
    let observerCallback: IntersectionObserverCallback | undefined
    class Observer {
      constructor(callback: IntersectionObserverCallback) {
        observerCallback = callback
      }
      observe() {
        observerCallback?.(
          [{ intersectionRatio: 0.5 } as IntersectionObserverEntry],
          this as unknown as IntersectionObserver,
        )
      }
      disconnect() {}
      unobserve() {}
      takeRecords() {
        return []
      }
      root = null
      rootMargin = "0px"
      thresholds = [0.5]
    }
    vi.stubGlobal("IntersectionObserver", Observer)
    const onDismiss = vi.fn()
    const onImpression = vi.fn()
    render(
      <GlanceTopCard
        canReview
        cards={[ready]}
        onDismiss={onDismiss}
        onImpression={onImpression}
        onSource={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: "Dismiss…" }))
    fireEvent.change(
      screen.getByLabelText("Why are you dismissing this item?"),
      { target: { value: "too_busy_to_review" } },
    )
    fireEvent.click(screen.getByRole("button", { name: "Record dismissal" }))
    expect(onDismiss).toHaveBeenCalledWith(ready, "too_busy_to_review")
    await act(() => vi.advanceTimersByTimeAsync(2_000))
    expect(onImpression).toHaveBeenCalledWith(
      ready,
      1,
      expect.stringMatching(/^priority:/),
    )
    vi.unstubAllGlobals()
  })
})
