import { act, fireEvent, render, screen } from "@testing-library/react"
import { afterEach, describe, expect, it, vi } from "vitest"

import type { ClinicalGlanceCard, RiskReason } from "@/client"
import { GlanceTopCard, riskReasonLabel } from "./GlanceTopCard"

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
  current_confidence_state: "unavailable",
  current_confidence_reasons: ["CONFIDENCE_NOT_APPLICABLE"],
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
  it("labels every generated server risk reason exhaustively", () => {
    expect(
      (
        [
          "critical",
          "unresolved",
          "clinician_confirmed",
          "clinical_entity",
          "clinic_feedback",
          "recency",
          "clinician_accepted",
          "care_plan_conflict",
          "clinician_confirmed_follow_up",
          "medication_status_conflict",
          "open_medication_reconciliation",
          "scheduled_follow_up",
          "synthetic_dataset_recent_encounter",
          "unavailable_review_required",
        ] satisfies RiskReason[]
      ).map(riskReasonLabel),
    ).toEqual([
      "Critical risk",
      "Unresolved clinical issue",
      "Confirmed by clinician",
      "Clinical fact requiring attention",
      "Raised by clinic feedback",
      "Recent information",
      "Accepted by clinician",
      "Conflicting care plan",
      "Clinician-confirmed follow-up",
      "Medication status conflict",
      "Medication reconciliation open",
      "Scheduled follow-up",
      "Recent imported encounter",
      "Reason unavailable · review required",
    ])
  })

  it("separates abstained critical information from ready priorities", () => {
    render(
      <GlanceTopCard
        cards={[ready]}
        onSource={vi.fn()}
        reviewCards={[abstained]}
      />,
    )
    expect(screen.getByText("Reviewed medication plan")).toBeInTheDocument()
    expect(screen.getByText("Confidence not applicable")).toBeInTheDocument()
    expect(screen.getByText("Needs clinical review")).toBeInTheDocument()
    expect(screen.getByText("Unverified severe allergy")).toBeInTheDocument()
    expect(screen.getByText("Critical · unverified")).toBeInTheDocument()
  })

  it("fails closed when a protected item is accidentally returned in the top-five list", () => {
    render(
      <GlanceTopCard
        cards={[
          ready,
          {
            ...abstained,
            review_state: "ready",
            current_priority_eligible: false,
          },
          {
            ...ready,
            highlight_id: "44444444-4444-4444-8444-444444444444",
            label: "Unassessed AI candidate",
            current_confidence_state: "review_required",
            current_confidence_reasons: ["AI_HIGHLIGHT_ASSESSMENT_MISSING"],
          },
        ]}
        onSource={vi.fn()}
      />,
    )
    expect(screen.getByText("1/5")).toBeInTheDocument()
    expect(screen.getByText("Unverified severe allergy")).toBeInTheDocument()
    expect(screen.getByText("Critical · unverified")).toBeInTheDocument()
    expect(screen.getByText("Unassessed AI candidate")).toBeInTheDocument()
  })

  it("discloses shadow learning without claiming weight updates or a personal profile", () => {
    render(
      <GlanceTopCard
        cards={[ready]}
        importanceMode="shadow"
        onSource={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByText("Why this decision?"))
    expect(
      screen.getByText(/Importance learning is in shadow mode/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/ranking weights are not changed/),
    ).toBeInTheDocument()
    expect(screen.getByText(/not a personal profile/)).toBeInTheDocument()
  })

  it("distinguishes disabled and unavailable importance modes", () => {
    const { rerender } = render(
      <GlanceTopCard
        cards={[ready]}
        importanceMode="disabled"
        onSource={vi.fn()}
      />,
    )
    fireEvent.click(screen.getByText("Why this decision?"))
    expect(
      screen.getByText(/Importance learning is disabled/),
    ).toBeInTheDocument()
    rerender(<GlanceTopCard cards={[ready]} onSource={vi.fn()} />)
    expect(
      screen.getByText(/Importance mode is unavailable/),
    ).toBeInTheDocument()
  })

  it("marks provider outage, stale age, fallback, and source review explicitly", () => {
    const onResolveSupport = vi.fn()
    render(
      <GlanceTopCard
        ageSeconds={3_600}
        cards={[]}
        fallbackKind="stored"
        freshnessState="stale"
        onSource={vi.fn()}
        outageMessage="Provider circuit open for one hour."
        providerOutage
        canResolveSupport
        onResolveSupport={onResolveSupport}
        reviewCards={[
          {
            ...abstained,
            fallback_kind: "rule_derived",
            support_state: "superseded",
            support_review_required: true,
          },
        ]}
      />,
    )
    expect(
      screen.getByText(/Provider circuit open for one hour/),
    ).toHaveTextContent("Last generated 60 minutes ago")
    expect(screen.getByText(/not limited to five/)).toBeInTheDocument()
    expect(
      screen.getByText("Rule-derived · review required"),
    ).toBeInTheDocument()
    expect(screen.getByText("Source superseded")).toBeInTheDocument()
    fireEvent.click(
      screen.getByRole("button", { name: "Reaffirm historical support" }),
    )
    expect(onResolveSupport).toHaveBeenCalledWith(
      expect.objectContaining({
        highlight_id: abstained.highlight_id,
        support_review_required: true,
      }),
      "reaffirm",
    )
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
      "current_priorities",
    )
    vi.unstubAllGlobals()
  })
})
