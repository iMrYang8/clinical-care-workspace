import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type { TranscriptRevisionPublic, VoiceSessionPublic } from "@/client"
import { VoiceReviewMode } from "./VoiceReviewMode"

const session: VoiceSessionPublic = {
  id: "session-1",
  patient_id: "patient-1",
  capture_kind: "clinical",
  state: "needs_review",
  patient_summary: null,
  warning_codes: ["DOWNSTREAM_RESULTS_STALE"],
  error_code: null,
  current_transcript_revision_id: "revision-1",
  published_entry_id: null,
  created_at: "2026-08-26T00:00:00Z",
  updated_at: "2026-08-26T00:00:00Z",
}

const transcript: TranscriptRevisionPublic = {
  id: "revision-1",
  session_id: "session-1",
  revision_no: 2,
  previous_revision_id: "revision-0",
  text: "Patient confirms penicillin allergy.",
  text_sha256: "a".repeat(64),
  summary: "Allergy requires review.",
  provider: "human-correction",
  model: "review-v1",
  detected_language: "en",
  status: "needs_review",
  needs_review: true,
  stale: true,
  fallback: false,
  warning_codes: ["DOWNSTREAM_RESULTS_STALE"],
  segments: [
    {
      id: "segment-1",
      ordinal: 0,
      text: "Patient confirms penicillin allergy.",
      text_start: 0,
      text_end: 37,
      start_ms: 2_000,
      end_ms: 5_000,
      speaker_id: "SPEAKER_00",
      detected_language: "en",
      confidence: 0.96,
      confidence_source: "provider",
      overlap_group_id: null,
      provider: "fixture",
      model: "fixture-v1",
    },
  ],
  facts: [
    {
      id: "fact-1",
      ordinal: 0,
      fact_type: "allergy",
      value: "penicillin allergy",
      exact_quote: "penicillin allergy",
      transcript_start: 17,
      transcript_end: 36,
      audio_asset_id: "asset-1",
      audio_start_ms: 2_100,
      audio_end_ms: 4_900,
      status: "proposed",
      stale: true,
    },
  ],
  created_at: "2026-08-26T00:00:00Z",
}

vi.mock("@/features/voice/voiceApi", () => ({
  voiceSession: vi.fn(async () => session),
  voiceTranscript: vi.fn(async () => transcript),
  loadAuthorizedAudio: vi.fn(async () => "blob:voice-test"),
}))

describe("VoiceReviewMode", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn()
    HTMLMediaElement.prototype.play = vi.fn(async () => undefined)
    URL.revokeObjectURL = vi.fn()
    window.matchMedia = vi.fn().mockReturnValue({ matches: false })
  })

  it("jumps from a fact to transcript/audio and blocks stale publication", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <VoiceReviewMode sessionId="session-1" membershipRole="clinician" />
      </QueryClientProvider>,
    )

    expect(await screen.findAllByText("penicillin allergy")).not.toHaveLength(0)
    const publish = screen.getByRole("button", {
      name: /Publish reviewed result/i,
    })
    expect(publish).toBeDisabled()
    expect(screen.getByText(/Publication disabled/)).toBeInTheDocument()

    fireEvent.click(screen.getAllByRole("button", { name: "Jump to 0:02" })[0])
    const audio = document.querySelector("audio")
    expect(audio?.currentTime).toBe(2)

    fireEvent.click(
      screen.getByRole("checkbox", { name: /Low confidence \/ overlap only/i }),
    )
    expect(document.getElementById("voice-segment-mobile-segment-1")).toBeNull()

    fireEvent.click(screen.getAllByRole("button", { name: /allergy/i })[0])
    await waitFor(() => {
      expect(
        document.getElementById("voice-segment-mobile-segment-1"),
      ).not.toBeNull()
      expect(Element.prototype.scrollIntoView).toHaveBeenCalled()
    })
    const scrollMock = vi.mocked(Element.prototype.scrollIntoView)
    const scrollTarget =
      scrollMock.mock.contexts[scrollMock.mock.contexts.length - 1]
    expect(scrollTarget).toBe(
      document.getElementById("voice-segment-mobile-segment-1"),
    )
    expect(scrollTarget).not.toBe(
      document.getElementById("voice-segment-desktop-segment-1"),
    )
    expect(audio?.currentTime).toBe(2.1)
  })

  it("labels unavailable segment confidence instead of implying certainty", async () => {
    const priorConfidence = transcript.segments[0].confidence
    transcript.segments[0].confidence = null
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    try {
      render(
        <QueryClientProvider client={client}>
          <VoiceReviewMode sessionId="session-1" membershipRole="clinician" />
        </QueryClientProvider>,
      )
      expect(await screen.findAllByText("confidence unavailable")).toHaveLength(
        2,
      )
    } finally {
      transcript.segments[0].confidence = priorConfidence
    }
  })
})
