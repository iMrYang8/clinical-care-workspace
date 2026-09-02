import { AxiosError } from "axios"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AuthService, PatientAccessService, PatientsService } from "@/client"
import {
  apiErrorMessage,
  authApi,
  clinicalApi,
  patientAccessApi,
  patientInvitationApi,
  patientSafeApi,
  streamDomainEvents,
  streamPatientEvents,
} from "./api"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("clinic authentication transport", () => {
  it("uppercases the clinic code without hiding invalid whitespace", async () => {
    const login = vi
      .spyOn(AuthService, "passwordLogin")
      .mockResolvedValue({} as never)

    await authApi.passwordLogin({
      clinicCode: " nightingale ",
      email: " Care.Team@Example.COM ",
      password: "  password whitespace remains  ",
    })

    expect(login).toHaveBeenCalledWith({
      headers: { "X-Clinic-Code": " NIGHTINGALE " },
      body: {
        username: "care.team@example.com",
        password: "  password whitespace remains  ",
      },
    })
  })
})

describe("product-safe request errors", () => {
  function responseError(status: number, detail: string) {
    return new AxiosError(
      "Request failed with a technical status",
      "ERR_BAD_RESPONSE",
      undefined,
      undefined,
      {
        config: {} as never,
        data: { detail },
        headers: {},
        status,
        statusText: "Error",
      },
    )
  }

  it("maps backend details to clinical product language", () => {
    expect(apiErrorMessage(responseError(400, "If-Match is required"))).toBe(
      "Review the information you entered and try again.",
    )
    expect(
      apiErrorMessage(responseError(403, "RLS tenant policy rejected UUID")),
    ).toBe("Your account does not have access to this action.")
    expect(
      apiErrorMessage(responseError(500, "provider model error_code")),
    ).toBe("Nightingale could not complete this request. Try again.")
  })

  it("does not expose transport or local exception messages", () => {
    expect(apiErrorMessage(new AxiosError("Network Error"))).toBe(
      "Nightingale could not reach the clinic service. Check your connection and try again.",
    )
    expect(
      apiErrorMessage(new Error("raw IndexedDB implementation detail")),
    ).toBe("Nightingale could not complete this request. Try again.")
  })
})

describe("domain event transport", () => {
  it("uses the same-origin cookie and Last-Event-ID without browser tokens", async () => {
    const bytes = new TextEncoder().encode(
      'id: 42\nevent: entry.updated\ndata: {"aggregate_type":"entry","aggregate_id":"entry-1","payload":{}}\n\n',
    )
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes)
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(body))
    vi.stubGlobal("fetch", fetchMock)
    const received: Array<{ id: number | string | null; event: string }> = []

    await streamDomainEvents(
      (event) => received.push({ id: event.id, event: event.event }),
      { signal: new AbortController().signal, lastEventId: 41 },
    )

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/api\/v1\/events\/stream$/)
    expect(url).not.toContain("token")
    expect(init.credentials).toBe("same-origin")
    expect(init.headers).toMatchObject({
      "Last-Event-ID": "41",
    })
    expect(init.headers).not.toHaveProperty("Authorization")
    expect(received).toEqual([{ id: 42, event: "entry.updated" }])
  })

  it("uses the patient-scoped portal stream and preserves UUID event cursors", async () => {
    const bytes = new TextEncoder().encode(
      'id: event-uuid\nevent: patient_publication.corrected\ndata: {"patient_id":"patient-1","aggregate_type":"patient_publication","aggregate_id":"publication-1","event_type":"patient_publication.corrected","created_at":"2026-09-01T01:00:00Z"}\n\n',
    )
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes)
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(body))
    vi.stubGlobal("fetch", fetchMock)
    const received: Array<{ id: number | string | null; event: string }> = []

    await streamPatientEvents(
      "patient-1",
      (event) => received.push({ id: event.id, event: event.event }),
      { signal: new AbortController().signal },
    )

    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/v1\/patients\/patient-1\/portal-events\/stream$/,
    )
    expect(received).toEqual([
      { id: "event-uuid", event: "patient_publication.corrected" },
    ])
  })
})

describe("PHI-safe handwritten API routes", () => {
  it("sends editor presence with only the immutable entry-version pointer", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        clinic_id: "clinic-1",
        patient_id: "patient-1",
        entry_id: "entry-1",
        entry_version_id: "version-1",
        actor_id: "actor-1",
        actor_role: "clinician",
        actor_display_name: "Dr Lee",
        expires_at: "2026-09-02T02:00:45Z",
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await clinicalApi.heartbeatEditorPresence("entry-1", "version-1")

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/api\/v1\/entries\/entry-1\/presence$/)
    expect(init.credentials).toBe("same-origin")
    expect(init.method).toBe("POST")
    expect(JSON.parse(init.body)).toEqual({
      entry_version_id: "version-1",
    })
    expect(init.body).not.toMatch(/content|draft|title|patient/i)
  })

  it("provisions phone access without changing the existing email invitation route", async () => {
    const provision = vi
      .spyOn(PatientAccessService, "accessProvisionPatientAccess")
      .mockResolvedValue({
        data: {
          access: {
            credential_id: "credential-1",
            patient_id: "patient-1",
            clinic_id: "clinic-1",
            portal_id: "MYCARE-1",
            masked_phone: "+65 **** 1234",
            access_state: "active",
          },
          invitation_token: "enrollment-token",
          claim_code: "CLAIM-ONCE",
          claim_code_expires_at: "2026-09-08T01:00:00Z",
          notification_id: "notification-1",
          notification_state: "queued",
        },
      } as never)
    const invite = vi
      .spyOn(PatientsService, "invitePatient")
      .mockResolvedValue({
        data: {
          id: "legacy-invitation",
          patient_id: "patient-1",
          email: "patient@example.test",
          state: "pending",
          expires_at: "2026-09-08T01:00:00Z",
          occurred_at: "2026-09-01T01:00:00Z",
        },
      } as never)

    const phoneResult = await clinicalApi.invitePatient("patient-1", {
      channel: "sms",
      phone: "+6512345678",
    })
    await clinicalApi.invitePatient("patient-1", {
      channel: "email",
      email: "patient@example.test",
    })

    expect(provision).toHaveBeenCalledWith({
      path: { patient_id: "patient-1" },
      body: { phone: "+6512345678", channel: "sms" },
    })
    expect("access" in phoneResult && phoneResult.access.portal_id).toBe(
      "MYCARE-1",
    )
    expect("access" in phoneResult && phoneResult.claim_code).toBe("CLAIM-ONCE")
    expect(invite).toHaveBeenCalledWith({
      path: { patient_id: "patient-1" },
      body: { email: "patient@example.test" },
    })
  })

  it("puts patient search terms in a POST body instead of a URL", async () => {
    const searchPatients = vi
      .spyOn(PatientsService, "searchPatients")
      .mockResolvedValue({
        data: { data: [], count: 0, offset: 0, limit: 24 },
      } as never)
    await clinicalApi.patientDirectory({
      search: "Synthetic Patient 2001-02-03",
      visitScope: "today",
    })
    expect(searchPatients).toHaveBeenCalledWith({
      body: {
        search: "Synthetic Patient 2001-02-03",
        visit_scope: "today",
        offset: 0,
        limit: 24,
      },
    })
    expect(JSON.stringify(searchPatients.mock.calls[0])).not.toContain(
      "/patients/search?",
    )
  })

  it("queues an appointment with only the route-scoped delivery fields", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        id: "notification-1",
        patient_id: "patient-1",
        visit_id: "visit-1",
        publication_id: null,
        purpose: "appointment",
        channel: "sms",
        destination_masked: "***1234",
        state: "queued",
        available_at: "2026-09-01T01:00:00Z",
        submitted_at: null,
        delivered_at: null,
        failed_at: null,
        acknowledged_at: null,
        revoked_at: null,
        created_at: "2026-09-01T01:00:00Z",
        updated_at: "2026-09-01T01:00:00Z",
        attempt_count: 0,
        receipts: [],
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await clinicalApi.createAppointmentNotification("patient-1", "visit-1", {
      channel: "sms",
      destination: "+6512345678",
    })

    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/v1\/patients\/patient-1\/visits\/visit-1\/notifications$/,
    )
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      channel: "sms",
      destination: "+6512345678",
    })
  })

  it("uses an opaque OTP challenge token for patient sign-in verification", async () => {
    const login = vi
      .spyOn(PatientAccessService, "accessBeginPatientLogin")
      .mockResolvedValue({
        data: {
          challenge_id: "challenge-id",
          challenge_token: "opaque-challenge",
          purpose: "login",
          portal_id: "MYCARE-1",
          masked_phone: "+65 **** 1234",
          expires_at: "2026-09-01T01:10:00Z",
          resend_available_at: "2026-09-01T01:01:00Z",
          attempts_remaining: 5,
          notification_id: "notification-1",
          delivery_state: "submitted",
        },
      } as never)
    const verify = vi
      .spyOn(PatientAccessService, "accessVerifyPatientOtp")
      .mockResolvedValue({
        data: {
          access: {
            credential_id: "credential-1",
            patient_id: "patient-1",
            clinic_id: "clinic-1",
            portal_id: "MYCARE-1",
            masked_phone: "+65 **** 1234",
            access_state: "active",
          },
          token: { access_token: "browser-cookie-token", token_type: "bearer" },
        },
      } as never)
    await patientAccessApi.requestLoginOtp({ portal_id: "MYCARE-1" })
    await patientAccessApi.verifyOtp({
      challenge_token: "opaque-challenge",
      otp: "123456",
    })
    expect(login).toHaveBeenCalledWith({ body: { portal_id: "MYCARE-1" } })
    expect(verify).toHaveBeenCalledWith({
      body: { challenge_token: "opaque-challenge", otp: "123456" },
    })
  })

  it("starts enrollment with the invitation, patient claim, and registered phone", async () => {
    const enrollment = vi
      .spyOn(PatientAccessService, "accessBeginPatientEnrollment")
      .mockResolvedValue({
        data: {
          challenge_id: "challenge-id",
          challenge_token: "opaque-enrollment-challenge",
          purpose: "enrollment",
          portal_id: "MYCARE-1",
          masked_phone: "+65 **** 1234",
          expires_at: "2026-09-01T01:10:00Z",
          resend_available_at: "2026-09-01T01:01:00Z",
          attempts_remaining: 5,
          notification_id: "notification-1",
          delivery_state: "submitted",
        },
      } as never)

    await patientInvitationApi.requestEnrollmentOtp({
      invitation_token: "clinic-id.secret-enrollment-token",
      claim_code: "CLAIM-ONCE",
      phone: "+6512345678",
    })

    expect(enrollment).toHaveBeenCalledWith({
      body: {
        invitation_token: "clinic-id.secret-enrollment-token",
        claim_code: "CLAIM-ONCE",
        phone: "+6512345678",
      },
    })
  })

  it("acknowledges a corrected publication through the canonical plural resource", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        id: "ack-1",
        publication_id: "publication-1",
        patient_id: "patient-1",
        channel: "portal",
        event_type: "acknowledged",
        acknowledged_at: "2026-09-01T01:00:00Z",
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await patientSafeApi.acknowledgePublication("publication-1")

    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/v1\/patient-publications\/publication-1\/acknowledgements$/,
    )
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      event_type: "acknowledged",
    })
  })

  it("requires an explicit clinician action to resolve changed source support", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      Response.json({
        id: "highlight-1",
        patient_id: "patient-1",
        support_state: "historical",
        support_review_required: false,
        current_priority_eligible: true,
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await clinicalApi.reaffirmHighlightSupport("highlight-1")

    expect(fetchMock.mock.calls[0][0]).toMatch(
      /\/api\/v1\/highlights\/highlight-1\/support-review\/reaffirm$/,
    )
    expect(fetchMock.mock.calls[0][1].method).toBe("POST")
  })
})
