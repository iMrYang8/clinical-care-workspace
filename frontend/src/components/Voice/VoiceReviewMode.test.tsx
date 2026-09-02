import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import type {
  ClinicalFactPublic,
  TranscriptRevisionPublic,
  VoiceSessionPublic,
} from "@/client"
import {
  sourceLanguageLabel,
  VoiceReviewMode,
  voiceJobPollInterval,
  voiceMedicationAssertions,
} from "./VoiceReviewMode"

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
  audio_quality: null,
  audio_quality_unavailable_reason: null,
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
  audio_quality: null,
  audio_quality_unavailable_reason: null,
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
  voiceJob: vi.fn(async () => ({
    id: "job-1",
    patient_id: "patient-1",
    kind: "voice_process",
    state: "needs_review",
    attempt_count: 1,
    max_attempts: 5,
    error_code: null,
    error_class: null,
    next_run_at: null,
    provider_outage: false,
    retry_history: [],
    delayed_at: null,
    timed_out_at: null,
    last_attempt_at: "2026-08-26T00:00:00Z",
    created_at: "2026-08-26T00:00:00Z",
    updated_at: "2026-08-26T00:00:00Z",
    ai_run: null,
  })),
  loadAuthorizedAudio: vi.fn(async () => "blob:voice-test"),
  publishReviewedVoice: vi.fn(async () => session),
}))

describe("VoiceReviewMode", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn()
    HTMLMediaElement.prototype.play = vi.fn(async () => undefined)
    URL.revokeObjectURL = vi.fn()
    window.matchMedia = vi.fn().mockReturnValue({ matches: false })
  })

  it("renders every qualified clinic language code with an explicit label", () => {
    expect(["en", "ms", "nan", "zh", "cmn"].map(sourceLanguageLabel)).toEqual([
      "English (en)",
      "Malay (ms)",
      "Hokkien / Southern Min (nan)",
      "Chinese (zh)",
      "Mandarin Chinese (cmn)",
    ])
    expect(sourceLanguageLabel("und")).toBe("und · review required")
    expect(sourceLanguageLabel(null)).toBeNull()
  })

  it("renders qualified source language instead of the provider detector hint", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const sourceQualified = {
      ...transcript,
      segments: [
        {
          ...transcript.segments[0],
          detected_language: "zh",
          source_language: "cmn",
          language_confidence: 0.92,
          language_spans: [
            {
              start_offset: 0,
              end_offset: 36,
              language_code: "cmn" as const,
              confidence: 0.92,
              detection_source: "lexicon_and_provider" as const,
              review_required: false,
            },
          ],
        },
      ],
    }
    const voiceApi = await import("@/features/voice/voiceApi")
    vi.mocked(voiceApi.voiceTranscript).mockResolvedValueOnce(sourceQualified)

    render(
      <QueryClientProvider client={client}>
        <VoiceReviewMode sessionId="session-1" membershipRole="clinician" />
      </QueryClientProvider>,
    )

    expect(
      await screen.findAllByText("Mandarin Chinese (cmn)", { exact: true }),
    ).toHaveLength(2)
    expect(
      screen.getAllByText(
        /Mandarin Chinese \(cmn\) · characters 0–36 · lexicon and provider · 92% confidence/,
      ),
    ).toHaveLength(2)
    expect(screen.queryByText("zh", { exact: true })).not.toBeInTheDocument()
  })

  it("continues polling a retryable failed audio job by its visible state", () => {
    expect(
      voiceJobPollInterval({
        id: "job-retry",
        patient_id: "patient-1",
        kind: "voice_process",
        state: "failed",
        attempt_count: 1,
        max_attempts: 5,
        error_code: "PROVIDER_TIMEOUT",
        error_class: "timeout",
        next_run_at: "2026-08-26T00:00:30Z",
        provider_outage: true,
        retry_history: [
          {
            attempt: 1,
            error_code: "PROVIDER_TIMEOUT",
            error_class: "timeout",
            attempted_at: "2026-08-26T00:00:00Z",
            next_retry_at: "2026-08-26T00:00:30Z",
          },
        ],
        delayed_at: "2026-08-26T00:00:00Z",
        timed_out_at: "2026-08-26T00:00:00Z",
        last_attempt_at: "2026-08-26T00:00:00Z",
        outage_started_at: "2026-08-26T00:00:00Z",
        outage_age_seconds: 10,
        retry_after_seconds: 20,
        visible_state: "delayed",
        created_at: "2026-08-26T00:00:00Z",
        updated_at: "2026-08-26T00:00:00Z",
      }),
    ).toBe(2000)
  })

  it("renders every structured transcript warning rather than hiding all but one", async () => {
    const queryClient = new QueryClient({
      defaultOptions: {
        queries: { retry: false },
        mutations: { retry: false },
      },
    })
    const multipleWarnings = {
      ...transcript,
      warning_codes: ["NOISE_REVIEW", "LOW_SIGNAL_REVIEW"],
    }
    const voiceApi = await import("@/features/voice/voiceApi")
    vi.mocked(voiceApi.voiceTranscript).mockResolvedValueOnce(multipleWarnings)

    render(
      <QueryClientProvider client={queryClient}>
        <VoiceReviewMode sessionId="session-1" membershipRole="clinician" />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText(/Low signal-to-noise audio was denoised/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/recording signal is too quiet/i),
    ).toBeInTheDocument()
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
      name: /Publish reviewed note/i,
    })
    expect(publish).toBeDisabled()
    expect(
      screen.getByText(/Update the summary and clinical findings/),
    ).toBeInTheDocument()

    fireEvent.click(screen.getAllByRole("button", { name: "Jump to 0:02" })[0])
    const audio = document.querySelector("audio")
    expect(audio?.currentTime).toBe(2)

    fireEvent.click(
      screen.getByRole("checkbox", {
        name: /Show uncertain or overlapping speech only/i,
      }),
    )
    expect(
      document.getElementById("voice-segment-mobile-segment-1"),
    ).not.toBeNull()
    expect(
      screen.getAllByText("Confidence unavailable").length,
    ).toBeGreaterThan(0)

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
      expect(
        await screen.findAllByText(/Confidence unavailable/i),
      ).toHaveLength(2)
    } finally {
      transcript.segments[0].confidence = priorConfidence
    }
  })

  it("requires complete structured medication fields before creating a review", () => {
    const fact: ClinicalFactPublic = {
      ...transcript.facts[0],
      fact_type: "medication",
      medication: "amoxicillin",
      dose_value: 500,
      dose_unit: "mg",
      route: "oral",
      frequency: "three times daily",
    }
    expect(voiceMedicationAssertions([fact])).toEqual([
      expect.objectContaining({
        assertion_id: "fact-1",
        medication: "amoxicillin",
        dose_value: 500,
        dose_unit: "mg",
        route: "oral",
        frequency: "three times daily",
      }),
    ])
    expect(
      voiceMedicationAssertions([{ ...fact, dose_unit: undefined }]),
    ).toEqual([])
  })

  it("shows consult roles and an unresolved family-vs-patient conflict", async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const consultTranscript = {
      ...transcript,
      warning_codes: [
        "UNRESOLVED_ALLERGY_CONFLICT",
        "PUBLISH_BLOCKED",
        "MULTI_AGENT_CONSULT_PROPOSAL",
      ],
      consult_agent: {
        enabled: true,
        speaker_roles: {
          SPEAKER_00: "clinician",
          SPEAKER_01: "patient",
          SPEAKER_02: "family",
        },
        conflicts: [
          {
            fact_type: "allergy",
            key: "penicillin",
            reason: "polarity",
            severity: "critical",
            auto_resolved: false,
            left_speaker_role: "patient",
            right_speaker_role: "family",
            left_polarity: "absent",
            right_polarity: "present",
          },
        ],
        summaries: {},
      },
      segments: [
        {
          ...transcript.segments[0],
          speaker_id: "SPEAKER_02",
          source_language: "ms",
        },
      ],
      facts: [
        {
          ...transcript.facts[0],
          value: "penicillin allergy:present",
          speaker_role: "family",
          source_language: "ms",
        },
      ],
    }
    const voiceApi = await import("@/features/voice/voiceApi")
    vi.mocked(voiceApi.voiceTranscript).mockResolvedValue(consultTranscript)

    render(
      <QueryClientProvider client={client}>
        <VoiceReviewMode sessionId="session-1" membershipRole="clinician" />
      </QueryClientProvider>,
    )

    expect(await screen.findByTestId("consult-conflicts")).toBeInTheDocument()
    expect(
      screen.getByText(/Patient \(absent\) vs Family \(present\)/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Patient and family allergy statements disagree/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/Agents blocked publication/i)).toBeInTheDocument()
    expect(screen.getAllByText("Family").length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Malay/).length).toBeGreaterThan(0)
  })
})
