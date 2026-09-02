import { afterEach, describe, expect, it, vi } from "vitest"

import type { JobPublic, VoicePublishPublic } from "@/client"
import { VoiceService } from "@/client"
import { publishReviewedVoice, voiceJob } from "./voiceApi"

afterEach(() => {
  vi.restoreAllMocks()
})

describe("generated voice API contracts", () => {
  it("reads job status through the generated JobPublic operation", async () => {
    const job = {
      id: "job-1",
      patient_id: "patient-1",
      kind: "voice_process",
      state: "delayed",
      attempt_count: 1,
      max_attempts: 5,
      error_code: "PROVIDER_TIMEOUT",
      error_class: "timeout",
      next_run_at: "2026-09-02T01:02:00Z",
      provider_outage: true,
      retry_history: [],
      delayed_at: "2026-09-02T01:00:15Z",
      timed_out_at: null,
      last_attempt_at: "2026-09-02T01:00:00Z",
      created_at: "2026-09-02T01:00:00Z",
      updated_at: "2026-09-02T01:00:15Z",
      ai_run: null,
    } as JobPublic
    const status = vi
      .spyOn(VoiceService, "sessionJobStatus")
      .mockResolvedValue({ data: job } as never)

    await expect(voiceJob("session-1")).resolves.toBe(job)
    expect(status).toHaveBeenCalledWith({
      path: { session_id: "session-1" },
    })
  })

  it("publishes with generated request and response types", async () => {
    const publication: VoicePublishPublic = {
      session_id: "session-1",
      entry_id: "entry-1",
      entry_version_id: "version-1",
      state: "published",
    }
    const publish = vi
      .spyOn(VoiceService, "publish")
      .mockResolvedValue({ data: publication } as never)

    await expect(
      publishReviewedVoice("session-1", "revision-1", [
        {
          assertion_id: "fact-1",
          medication: "amoxicillin",
          dose_value: 500,
          dose_unit: "mg",
          route: "oral",
          frequency: "twice daily",
          confirmed: true,
        },
      ]),
    ).resolves.toBe(publication)
    expect(publish).toHaveBeenCalledWith({
      path: { session_id: "session-1" },
      body: {
        expected_revision_id: "revision-1",
        medication_reviews: [
          {
            assertion_id: "fact-1",
            medication: "amoxicillin",
            dose_value: 500,
            dose_unit: "mg",
            route: "oral",
            frequency: "twice daily",
            confirmed: true,
          },
        ],
      },
    })
  })
})
