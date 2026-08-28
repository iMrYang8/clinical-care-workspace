import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import type { ReactElement } from "react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { TrustService } from "@/client"
import { AiManualHighlight, aiDisplayProjection } from "./AiManualHighlight"

const acceptedHighlight = {
  id: "highlight-1",
  patient_id: "patient-1",
  entry_id: "entry-1",
  source_entry_version_id: "version-1",
  label: "oral intake remains restricted",
  status: "accepted",
  pinned: false,
  critical: false,
  patient_facing: false,
  anchor_state: "resolved",
  review_required: false,
  feature_keys: ["entry_type:ai_nurse_consult_summary"],
  base_score: 0.35,
  learned_score: 0,
  final_score: 0.35,
  risk_reason: "clinician_confirmed",
  unresolved: false,
  clinician_confirmed: true,
  provenance_pointer_id: "pointer-1",
}

function renderHighlight(component: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  })
  const invalidate = vi.spyOn(queryClient, "invalidateQueries")
  render(component, {
    wrapper: ({ children }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    ),
  })
  return { invalidate }
}

function selectText(root: HTMLElement, quote: string) {
  const node = root.querySelector("p")?.firstChild
  if (!node?.textContent) throw new Error("Source text node was not rendered")
  const start = node.textContent.indexOf(quote)
  if (start < 0) throw new Error("Quote was not rendered")
  const range = document.createRange()
  range.setStart(node, start)
  range.setEnd(node, start + quote.length)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
  fireEvent.mouseUp(root)
}

describe("AI note manual highlights", () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    window.getSelection()?.removeAllRanges()
  })

  it("binds a clinician-confirmed selection to the immutable AI version", async () => {
    const rawContent =
      "AI-assisted nursing draft: Patient 💊 review: oral intake remains restricted pending reassessment."
    const projection = aiDisplayProjection(rawContent)
    const quote = "oral intake remains restricted"
    const create = vi
      .spyOn(TrustService, "createHighlight")
      .mockResolvedValue({ data: acceptedHighlight } as never)
    const pin = vi.spyOn(TrustService, "pin").mockResolvedValue({
      data: { ...acceptedHighlight, pinned: true },
    } as never)
    const { invalidate } = renderHighlight(
      <AiManualHighlight
        enabled
        entryId="entry-1"
        entryType="ai_nurse_consult_summary"
        entryVersionId="version-1"
        patientId="patient-1"
        rawContent={rawContent}
        rawOffsetUtf16={projection.rawOffsetUtf16}
      >
        <p>{projection.content}</p>
      </AiManualHighlight>,
    )

    selectText(screen.getByTestId("ai-highlight-source-entry-1"), quote)
    fireEvent.click(screen.getByRole("button", { name: "Add to priorities" }))

    expect(screen.getByRole("dialog")).toHaveTextContent(quote)
    fireEvent.click(
      screen.getByRole("button", { name: "Add to Current priorities" }),
    )

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(pin).toHaveBeenCalledWith({
        path: { highlight_id: acceptedHighlight.id },
      }),
    )
    const rawStartUtf16 = rawContent.indexOf(quote)
    const expectedStart = Array.from(rawContent.slice(0, rawStartUtf16)).length
    expect(create).toHaveBeenCalledWith({
      path: { entry_id: "entry-1" },
      body: expect.objectContaining({
        entry_version_id: "version-1",
        start_offset: expectedStart,
        end_offset: expectedStart + Array.from(quote).length,
        exact_quote: quote,
        label: quote,
        patient_facing: false,
        feature_keys: ["entry_type:ai_nurse_consult_summary"],
        clinician_confirmed: true,
      }),
    })
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({
        queryKey: ["patients", "patient-1", "glance"],
      }),
    )
    expect(
      screen.getByText(
        "Added to Current priorities: oral intake remains restricted",
      ),
    ).toBeInTheDocument()
  })

  it("does not expose the selection workflow when it is disabled", () => {
    const rawContent = "AI-assisted nursing draft: Selected clinical wording"
    const projection = aiDisplayProjection(rawContent)
    renderHighlight(
      <AiManualHighlight
        enabled={false}
        entryId="entry-2"
        entryType="ai_nurse_consult_summary"
        entryVersionId="version-2"
        patientId="patient-1"
        rawContent={rawContent}
        rawOffsetUtf16={projection.rawOffsetUtf16}
      >
        <p>{projection.content}</p>
      </AiManualHighlight>,
    )

    expect(screen.getByText("Selected clinical wording")).toBeInTheDocument()
    expect(
      screen.queryByTestId("ai-highlight-source-entry-2"),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByRole("button", { name: "Add to priorities" }),
    ).not.toBeInTheDocument()
  })
})
