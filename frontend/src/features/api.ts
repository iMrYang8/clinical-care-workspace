import { AxiosError } from "axios"
import type {
  AssignmentUpdate,
  AuditEventPublic,
  ClinicAISettingPublic,
  ClinicAISettingUpdate,
  ClinicalFactAssertionPublic,
  ClinicalGlancePublic,
  CommentCreate,
  CommentPublic,
  ConflictPublic,
  DecisionExplanationPublic,
  DiffPublic,
  EntryCreate,
  EntryPatch,
  EntryPublic,
  EntryVersionPublic,
  GlancePublic,
  HighlightPublic,
  MedicationReviewAttestation,
  MembershipCreate,
  MembershipInvitationPublic,
  MembershipPublic,
  MePublic,
  NotificationPublic,
  PatientAccessEnrollStartRequest,
  PatientAccessLoginStartRequest,
  PatientAccessProvisionCreate,
  PatientAccessProvisionPublic,
  PatientAccessPublic,
  PatientAccessRecoveryCreate,
  PatientAccessVerifyPublic,
  PatientAccessVerifyRequest,
  PatientDetailPublic,
  PatientDuplicateCandidate,
  PatientDuplicateCheckPublic,
  PatientIdentityInput,
  PatientInvitationPreviewPublic,
  PatientOTPChallengePublic,
  PatientPortalEventPublic,
  PatientPortalInvitationCreate,
  PatientPortalInvitationPublic,
  PatientPublic,
  PatientPublicationAcknowledgementPublic,
  PatientPublicationCorrectionCreate,
  PatientPublicationPublic,
  PatientPublicationReceiptPublic,
  PatientSharingRequestPublic,
  PatientsPublic,
  PatientsSearchRequest,
  PatientTimelineEntry,
  ProvenanceResolved,
  ProvisionalSafetyAlertPublic,
  TeamMemberPublic,
} from "@/client"
import {
  AdminService,
  AuthService,
  CollaborationService,
  EntriesService,
  PatientAccessService,
  PatientsService,
  TeamService,
  TrustService,
} from "@/client"
import {
  authenticatedFetch,
  notifyAuthenticationRejection,
} from "@/features/authenticatedFetch"
import {
  type EditorPresenceRecord,
  editorPresenceRecordFrom,
} from "@/features/editorPresence"

export type {
  ClinicAISettingUpdate,
  PatientDuplicateCandidate,
  PatientIdentityInput,
}

export type DemoPersona = "patient" | "staff" | "clinician" | "admin"
export type ClinicalRole = MePublic["role"]
export type ClinicalComment = CommentPublic

export type ClinicalTimelineEntry = PatientTimelineEntry & {
  origin: string
  author_id: string | null
}

export type PatientDuplicateCheck = PatientDuplicateCheckPublic

export type PatientDetail = PatientDetailPublic & {
  /** Compatibility for pre-hardening fixtures; current API uses today_visit_id. */
  active_visit_id?: string | null
}

export type ClinicalConflict = ConflictPublic

export type ClinicalFactAssertion = ClinicalFactAssertionPublic

export type PatientSharingRequest = PatientSharingRequestPublic

export type MedicationAssertion = {
  assertion_id: string
  medication: string
  dose_value: number
  dose_unit: string
  route: string
  frequency: string
}

export type MedicationReviewInput = MedicationAssertion &
  Required<Pick<MedicationReviewAttestation, "confirmed">> & {
    confirmed: true
  }

export type PatientPublication = PatientPublicationPublic

export type PatientPublicationReceipt = PatientPublicationReceiptPublic

export type PatientPublicationAcknowledgement =
  PatientPublicationAcknowledgementPublic

export type PatientPortalEvent = PatientPortalEventPublic

/** UI discriminator over the two generated invitation request contracts. */
export type PatientPortalInvitationInput =
  | ({ channel: "email" } & PatientPortalInvitationCreate)
  | {
      channel: NonNullable<PatientAccessProvisionCreate["channel"]>
      phone: PatientAccessProvisionCreate["phone"]
    }

/** The API intentionally returns a different generated contract per channel. */
export type PatientPortalInvitationResult =
  | PatientPortalInvitationPublic
  | PatientAccessProvisionPublic

export type PatientAccessChallenge = PatientOTPChallengePublic

export type PatientAccessSession = PatientAccessPublic

export type PatientAccessVerifyResult = PatientAccessVerifyPublic

export type NotificationDelivery = NotificationPublic

export type ProvisionalSafetyAlert = ProvisionalSafetyAlertPublic

async function jsonRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(
    `${import.meta.env.VITE_API_URL ?? ""}${url}`,
    { credentials: "same-origin", ...init },
  )
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = (payload as { detail?: unknown } | null)?.detail
    const detailMessage =
      typeof detail === "string"
        ? detail
        : detail && typeof detail === "object"
          ? String(
              (detail as { message?: unknown; code?: unknown }).message ??
                (detail as { code?: unknown }).code ??
                "",
            )
          : ""
    throw new Error(detailMessage || `Request failed with ${response.status}`)
  }
  return (await response.json()) as T
}

async function heartbeatEditorPresence(
  entryId: string,
  entryVersionId: string,
  signal?: AbortSignal,
): Promise<EditorPresenceRecord> {
  const payload = await jsonRequest<unknown>(
    `/api/v1/entries/${entryId}/presence`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry_version_id: entryVersionId }),
      signal,
    },
  )
  const presence = editorPresenceRecordFrom(payload)
  if (!presence) throw new Error("Invalid editor presence response")
  return presence
}

async function duplicateCheck(
  body: PatientIdentityInput,
): Promise<PatientDuplicateCheck> {
  return jsonRequest("/api/v1/patients/duplicate-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

async function createPatientRecord(
  body: PatientIdentityInput & { duplicate_confirmation_token?: string },
): Promise<PatientDetail> {
  return jsonRequest("/api/v1/patients", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID(),
    },
    body: JSON.stringify(body),
  })
}

async function patientDetail(patientId: string): Promise<PatientDetail> {
  return jsonRequest(`/api/v1/patients/${patientId}`)
}

async function invitePatient(
  patientId: string,
  destination: string | PatientPortalInvitationInput,
): Promise<PatientPortalInvitationResult> {
  if (typeof destination === "string" || destination.channel === "email") {
    const email =
      typeof destination === "string" ? destination : destination.email
    return (
      await PatientsService.invitePatient({
        path: { patient_id: patientId },
        body: { email },
      })
    ).data
  }
  return (
    await PatientAccessService.accessProvisionPatientAccess({
      path: { patient_id: patientId },
      body: { phone: destination.phone, channel: destination.channel },
    })
  ).data
}

async function revokePatientAccess(
  patientId: string,
): Promise<PatientAccessSession> {
  return (
    await PatientAccessService.accessRevokePatientAccess({
      path: { patient_id: patientId },
      body: { reason_code: "access_revoked_by_care_team" },
    })
  ).data
}

async function recoverPatientAccess(
  patientId: string,
  body: Pick<PatientAccessRecoveryCreate, "phone" | "channel">,
): Promise<PatientPortalInvitationResult> {
  return (
    await PatientAccessService.accessRecoverPatientAccess({
      path: { patient_id: patientId },
      body: {
        ...body,
        reason_code: "patient_access_recovery_requested",
      },
    })
  ).data
}

async function notificationDeliveries(
  patientId: string,
  visitId: string,
): Promise<NotificationDelivery[]> {
  const response = await jsonRequest<
    NotificationDelivery[] | { data: NotificationDelivery[] }
  >(`/api/v1/patients/${patientId}/visits/${visitId}/notifications`)
  return Array.isArray(response) ? response : response.data
}

async function createAppointmentNotification(
  patientId: string,
  visitId: string,
  body: {
    channel: "email" | "sms" | "whatsapp"
    destination: string
  },
): Promise<NotificationDelivery> {
  return jsonRequest(
    `/api/v1/patients/${patientId}/visits/${visitId}/notifications`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(body),
    },
  )
}

async function resendNotification(
  notificationId: string,
): Promise<NotificationDelivery> {
  return jsonRequest(`/api/v1/notifications/${notificationId}/resend`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
  })
}

async function revokeNotification(
  notificationId: string,
): Promise<NotificationDelivery> {
  return jsonRequest(`/api/v1/notifications/${notificationId}/revoke`, {
    method: "POST",
  })
}

async function patientConflicts(
  patientId: string,
): Promise<ClinicalConflict[]> {
  return jsonRequest(`/api/v1/patients/${patientId}/conflicts`)
}

async function patientClinicalFacts(
  patientId: string,
): Promise<ClinicalFactAssertion[]> {
  return jsonRequest(`/api/v1/patients/${patientId}/clinical-facts`)
}

async function resolveConflict(
  conflictId: string,
  correctionEntryId: string,
  resolution: string,
): Promise<ClinicalConflict> {
  return jsonRequest(`/api/v1/conflicts/${conflictId}/resolve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      correction_entry_id: correctionEntryId,
      resolution,
    }),
  })
}

async function requestPatientSharing(
  entryId: string,
  entryVersionId: string,
): Promise<PatientSharingRequest> {
  return jsonRequest(`/api/v1/entries/${entryId}/patient-sharing-requests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ entry_version_id: entryVersionId }),
  })
}

async function patientSharingRequests(
  patientId: string,
): Promise<PatientSharingRequest[]> {
  return jsonRequest(`/api/v1/patients/${patientId}/patient-sharing-requests`)
}

async function patientPublications(
  patientId: string,
): Promise<PatientPublication[]> {
  return jsonRequest(`/api/v1/patients/${patientId}/patient-publications`)
}

async function approvePatientSharing(
  requestId: string,
  medicationReviews: MedicationReviewInput[] = [],
): Promise<PatientPublication> {
  return jsonRequest(`/api/v1/patient-sharing-requests/${requestId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ medication_reviews: medicationReviews }),
  })
}

async function withdrawPatientPublication(
  publicationId: string,
): Promise<PatientPublication> {
  return jsonRequest(`/api/v1/patient-publications/${publicationId}/withdraw`, {
    method: "POST",
  })
}

async function correctPatientPublication(
  publicationId: string,
  body: PatientPublicationCorrectionCreate,
  idempotencyKey: string,
): Promise<PatientPublication> {
  return jsonRequest(`/api/v1/patient-publications/${publicationId}/correct`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(body),
  })
}

export function patientSharingErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : ""
  if (message.includes("UNRESOLVED_CLINICAL_CONFLICT"))
    return "Resolve the high-risk clinical conflict before sharing this note."
  if (message.includes("REDACTION_EVALUATION_REQUIRED"))
    return "Patient sharing is paused until the clinic redaction check passes."
  if (message.includes("DECISION_ASSESSMENT_NOT_PUBLISHABLE"))
    return "This AI-assisted content needs a supported clinical assessment before sharing."
  if (message.includes("CLAIM_LEVEL_PROVENANCE_REQUIRED"))
    return "Each clinical claim needs an exact source before sharing."
  if (message.includes("Review the latest version"))
    return "This note changed after the request. Ask care staff to submit the latest version."
  if (message.includes("already"))
    return "This sharing request has already been reviewed."
  return "Patient sharing could not be completed. Review the note and try again."
}

export type PatientInvitationPreview = PatientInvitationPreviewPublic

async function previewPatientInvitation(body: {
  token: string
  email: string
}): Promise<PatientInvitationPreview> {
  return jsonRequest("/api/v1/auth/patient-invitations/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

async function acceptPatientInvitation(body: {
  token: string
  email: string
  password: string
  full_name?: string
}): Promise<void> {
  await jsonRequest("/api/v1/auth/patient-invitations/accept", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

async function requestPatientEnrollmentOtp(
  body: PatientAccessEnrollStartRequest,
): Promise<PatientAccessChallenge> {
  return (await PatientAccessService.accessBeginPatientEnrollment({ body }))
    .data
}

async function acceptPatientPhoneInvitation(
  body: PatientAccessVerifyRequest,
): Promise<PatientAccessVerifyResult> {
  return (await PatientAccessService.accessVerifyPatientOtp({ body })).data
}

async function requestPatientLoginOtp(
  body: PatientAccessLoginStartRequest,
): Promise<PatientAccessChallenge> {
  return (await PatientAccessService.accessBeginPatientLogin({ body })).data
}

async function verifyPatientLoginOtp(
  body: PatientAccessVerifyRequest,
): Promise<PatientAccessVerifyResult> {
  return (await PatientAccessService.accessVerifyPatientOtp({ body })).data
}

// Deliberately narrower than GlancePublic. The patient view never receives
// care-team ranking reasons, critical flags, or review state in its component
// contract, even when the server response contains those internal fields.
export type PatientSafeGlance = {
  patient_id: string
  source: "precomputed"
  generated_at: string
  cards: Array<{
    highlight_id: string
    label: string
    provenance_pointer_id: string
  }>
}

export type VersionConflict = {
  code: "VERSION_CONFLICT"
  message: string
  current_version_id?: string
}

export function quotedEtag(versionId: string): string {
  return `"${versionId}"`
}

export function httpStatus(error: unknown): number | undefined {
  return error instanceof AxiosError ? error.response?.status : undefined
}

export function apiErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const status = error.response?.status
    if (status === undefined) {
      return "Nightingale could not reach the clinic service. Check your connection and try again."
    }
    if (status === 400 || status === 422) {
      return "Review the information you entered and try again."
    }
    if (status === 401) {
      return "Your account details could not be verified. Sign in again."
    }
    if (status === 403) {
      return "Your account does not have access to this action."
    }
    if (status === 404) {
      return "This care information is no longer available."
    }
    if (status === 409) {
      return "This care information changed in another session. Refresh and try again."
    }
    if (status === 429) {
      return "Too many requests were made. Wait a moment and try again."
    }
    return "Nightingale could not complete this request. Try again."
  }
  return "Nightingale could not complete this request. Try again."
}

export function versionConflictFrom(error: unknown): VersionConflict | null {
  if (!(error instanceof AxiosError) || error.response?.status !== 409)
    return null
  const payload = error.response.data as {
    code?: string
    message?: string
    current_version_id?: string
    detail?: {
      code?: string
      message?: string
      current_version_id?: string
    }
  }
  const candidate = payload.detail ?? payload
  if (candidate.code !== "VERSION_CONFLICT") return null
  return {
    code: "VERSION_CONFLICT",
    message: candidate.message ?? "This care note changed in another session.",
    current_version_id: candidate.current_version_id,
  }
}

async function demoLogin(persona: DemoPersona): Promise<void> {
  await AuthService.demoLogin({ body: { persona } })
}

export type PasswordLoginInput = {
  clinicCode: string
  email: string
  password: string
}

async function passwordLogin(input: PasswordLoginInput): Promise<void> {
  await AuthService.passwordLogin({
    headers: { "X-Clinic-Code": input.clinicCode.toUpperCase() },
    body: {
      username: input.email.trim().toLowerCase(),
      password: input.password,
    },
  })
}

export type TeamMemberOption = TeamMemberPublic

async function teamMembers(): Promise<TeamMemberOption[]> {
  return (await TeamService.teamMembers()).data.data
}

async function me(): Promise<MePublic> {
  return (await AuthService.me()).data
}

async function logout(): Promise<void> {
  const controller = new AbortController()
  // The request remains bounded, but allows enough time for a congested local
  // TLS proxy to return the authoritative HttpOnly-cookie deletion response.
  // PHI is already masked in every tab before this wait begins.
  const timeout = window.setTimeout(() => controller.abort(), 5_000)
  try {
    await AuthService.logout({ signal: controller.signal })
  } finally {
    window.clearTimeout(timeout)
  }
}

async function acceptInvitation(body: {
  email: string
  token: string
  password: string
  full_name?: string | null
}): Promise<MembershipPublic> {
  return (await AuthService.acceptMembershipInvitation({ body })).data
}

async function memberships(): Promise<MembershipPublic[]> {
  return (await AdminService.memberships()).data.data
}

async function createMembership(
  body: MembershipCreate,
): Promise<MembershipInvitationPublic> {
  return (await AdminService.createMembership({ body })).data
}

async function deactivateMembership(id: string): Promise<MembershipPublic> {
  return (
    await AdminService.deactivateMembership({ path: { membership_id: id } })
  ).data
}

async function auditEvents(): Promise<AuditEventPublic[]> {
  return (await AdminService.auditEvents()).data.data
}

export type ClinicAISetting = ClinicAISettingPublic

async function clinicAISettings(): Promise<ClinicAISetting> {
  return jsonRequest("/api/v1/admin/ai-settings")
}

async function updateClinicAISettings(
  body: ClinicAISettingUpdate,
): Promise<ClinicAISetting> {
  return jsonRequest("/api/v1/admin/ai-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

async function patients(): Promise<PatientPublic[]> {
  return (await PatientsService.patients()).data.data
}

export type PatientDirectoryItem = PatientPublic

export type PatientDirectoryPage = PatientsPublic

async function patientDirectory(input: {
  search?: string
  visitScope?: "all" | "today" | "previous"
  offset?: number
  limit?: number
}): Promise<PatientDirectoryPage> {
  const body: PatientsSearchRequest = {
    search: input.search ?? null,
    visit_scope: input.visitScope ?? "all",
    offset: input.offset ?? 0,
    limit: input.limit ?? 24,
  }
  return (await PatientsService.searchPatients({ body })).data
}

async function patientTimeline(
  patientId: string,
): Promise<PatientTimelineEntry[]> {
  return (
    await PatientsService.patientTimeline({ path: { patient_id: patientId } })
  ).data.data
}

async function clinicalTimeline(
  patientId: string,
): Promise<ClinicalTimelineEntry[]> {
  const safeTimeline = await patientTimeline(patientId)
  return Promise.all(
    safeTimeline.map(async (timelineEntry) => {
      const detail = (
        await EntriesService.read({ path: { entry_id: timelineEntry.id } })
      ).data
      return {
        ...timelineEntry,
        origin: "origin" in detail ? detail.origin : "human",
        author_id: "author_id" in detail ? detail.author_id : null,
      }
    }),
  )
}

async function readClinicalEntry(
  entryId: string,
): Promise<ClinicalTimelineEntry> {
  const detail = (await EntriesService.read({ path: { entry_id: entryId } }))
    .data
  return {
    ...detail,
    origin: "origin" in detail ? detail.origin : "human",
    author_id: "author_id" in detail ? detail.author_id : null,
  } as ClinicalTimelineEntry
}

async function glance(patientId: string): Promise<ClinicalGlancePublic> {
  return (
    await PatientsService.patientGlance({ path: { patient_id: patientId } })
  ).data as ClinicalGlancePublic
}

async function patientSafeGlance(
  patientId: string,
): Promise<PatientSafeGlance> {
  const response = (
    await PatientsService.patientGlance({ path: { patient_id: patientId } })
  ).data as GlancePublic
  return {
    patient_id: response.patient_id,
    source: response.source ?? "precomputed",
    generated_at: response.generated_at,
    cards: response.cards.map((card) => ({
      highlight_id: card.highlight_id,
      label: card.label,
      provenance_pointer_id: card.provenance_pointer_id,
    })),
  }
}

async function patientPublicationReceipts(
  patientId: string,
): Promise<PatientPublicationReceipt[]> {
  return jsonRequest(`/api/v1/patients/${patientId}/publication-receipts`)
}

async function acknowledgePatientPublication(
  publicationId: string,
): Promise<PatientPublicationAcknowledgement> {
  return jsonRequest(
    `/api/v1/patient-publications/${publicationId}/acknowledgements`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_type: "acknowledged" }),
    },
  )
}

async function patientPortalEvents(
  patientId: string,
  since?: string,
): Promise<PatientPortalEvent[]> {
  const query = new URLSearchParams({ limit: "100" })
  if (since) query.set("since", since)
  return jsonRequest(
    `/api/v1/patients/${patientId}/portal-events?${query.toString()}`,
  )
}

async function liveSafetyAlerts(
  sessionId: string,
): Promise<ProvisionalSafetyAlert[]> {
  const response = await jsonRequest<
    ProvisionalSafetyAlert[] | { data: ProvisionalSafetyAlert[] }
  >(`/api/v1/voice/sessions/${sessionId}/safety-alerts`)
  return Array.isArray(response) ? response : response.data
}

async function updateLiveSafetyAlert(
  alertId: string,
  action: "confirm" | "dismiss",
): Promise<ProvisionalSafetyAlert> {
  return jsonRequest(`/api/v1/voice/safety-alerts/${alertId}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      reason_code:
        action === "confirm" ? "clinician_confirmed" : "clinician_dismissed",
    }),
  })
}

async function createEntry(
  body: EntryCreate,
): Promise<EntryPublic | PatientTimelineEntry> {
  return (await EntriesService.create({ body })).data
}

async function createPatientInsight(
  patientId: string,
  title: string,
  content: string,
): Promise<PatientTimelineEntry> {
  const created = (
    await EntriesService.create({
      body: {
        patient_id: patientId,
        section: "patient",
        title,
        content,
        patient_facing: true,
        origin: "human",
      },
    })
  ).data
  return {
    id: created.id,
    patient_id: created.patient_id,
    section: created.section,
    entry_type: created.entry_type,
    author_role: created.author_role,
    provenance: created.provenance,
    patient_facing: created.patient_facing,
    version_id: created.version_id,
    version_no: created.version_no,
    title: created.title,
    content: created.content,
    created_at: created.created_at,
    occurred_at: created.occurred_at,
  }
}

async function patchEntry(
  entryId: string,
  versionId: string,
  body: EntryPatch,
): Promise<EntryPublic | PatientTimelineEntry> {
  return (
    await EntriesService.patch({
      path: { entry_id: entryId },
      headers: { "If-Match": quotedEtag(versionId) },
      body,
    })
  ).data
}

async function versions(entryId: string): Promise<EntryVersionPublic[]> {
  return (await EntriesService.versions({ path: { entry_id: entryId } })).data
    .data
}

async function diff(
  entryId: string,
  fromVersionId: string,
  againstVersionId: string,
): Promise<DiffPublic> {
  return (
    await EntriesService.diff({
      path: { entry_id: entryId, version_id: fromVersionId },
      query: { against: againstVersionId },
    })
  ).data
}

async function revert(
  entryId: string,
  targetVersionId: string,
  currentVersionId: string,
): Promise<EntryPublic | PatientTimelineEntry> {
  return (
    await EntriesService.revert({
      path: { entry_id: entryId, version_id: targetVersionId },
      headers: { "If-Match": quotedEtag(currentVersionId) },
    })
  ).data
}

async function comments(entryId: string): Promise<ClinicalComment[]> {
  return (
    await CollaborationService.listComments({ path: { entry_id: entryId } })
  ).data as ClinicalComment[]
}

async function createComment(
  entryId: string,
  body: CommentCreate,
): Promise<CommentPublic> {
  return jsonRequest(`/api/v1/entries/${entryId}/comments`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "If-Match": quotedEtag(body.entry_version_id),
    },
    body: JSON.stringify(body),
  })
}

async function reply(
  commentId: string,
  body: CommentCreate,
): Promise<CommentPublic> {
  return jsonRequest(`/api/v1/comments/${commentId}/replies`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "If-Match": quotedEtag(body.entry_version_id),
    },
    body: JSON.stringify(body),
  })
}

async function resolveComment(
  commentId: string,
  revision: number,
): Promise<CommentPublic> {
  return jsonRequest(`/api/v1/comments/${commentId}/resolve`, {
    method: "POST",
    headers: { "If-Match": quotedEtag(String(revision)) },
  })
}

async function unresolveComment(
  commentId: string,
  revision: number,
): Promise<CommentPublic> {
  return jsonRequest(`/api/v1/comments/${commentId}/unresolve`, {
    method: "POST",
    headers: { "If-Match": quotedEtag(String(revision)) },
  })
}

async function assignComment(
  commentId: string,
  revision: number,
  body: AssignmentUpdate,
): Promise<ClinicalComment> {
  return jsonRequest(`/api/v1/comments/${commentId}/assignment`, {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      "If-Match": quotedEtag(String(revision)),
    },
    body: JSON.stringify(body),
  })
}

async function acceptHighlight(highlightId: string): Promise<HighlightPublic> {
  return (await TrustService.accept({ path: { highlight_id: highlightId } }))
    .data
}

async function rejectHighlight(highlightId: string): Promise<HighlightPublic> {
  return (await TrustService.reject({ path: { highlight_id: highlightId } }))
    .data
}

async function pinHighlight(highlightId: string): Promise<HighlightPublic> {
  return (await TrustService.pin({ path: { highlight_id: highlightId } })).data
}

export type DismissReason =
  | "not_relevant"
  | "outdated"
  | "already_addressed"
  | "too_busy_to_review"

async function dismissHighlight(
  highlightId: string,
  reason: DismissReason,
): Promise<HighlightPublic> {
  return (
    await TrustService.feedback({
      path: { highlight_id: highlightId },
      body: { signal: "dismiss", reason },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    })
  ).data
}

async function decisionExplanation(
  highlightId: string,
): Promise<DecisionExplanationPublic> {
  return (
    await TrustService.decisionExplanation({
      path: { highlight_id: highlightId },
    })
  ).data
}

async function requestHighlightReview(
  highlightId: string,
  reason: string,
): Promise<HighlightPublic> {
  return (
    await TrustService.requestReview({
      path: { highlight_id: highlightId },
      body: { reason },
    })
  ).data
}

async function resolveHighlightSupportReview(
  highlightId: string,
  resolution: "reaffirm" | "supersede",
): Promise<HighlightPublic> {
  return jsonRequest(
    `/api/v1/highlights/${highlightId}/support-review/${resolution}`,
    { method: "POST" },
  )
}

async function recordImportanceImpression(input: {
  highlightId: string
  viewEventId: string
  rank: number
  surface: "current_priorities" | "clinical_review"
  exposureProbability: number
  visibleRatio: number
  visibleDurationMs: number
}): Promise<void> {
  await TrustService.recordImportanceImpression({
    body: {
      highlight_id: input.highlightId,
      view_event_id: input.viewEventId,
      rank: input.rank,
      surface: input.surface,
      exposure_probability: input.exposureProbability,
      visible_ratio: input.visibleRatio,
      visible_duration_ms: input.visibleDurationMs,
    },
  })
}

async function resolveProvenance(
  pointerId: string,
): Promise<ProvenanceResolved> {
  return (
    await TrustService.provenanceResolve({ path: { pointer_id: pointerId } })
  ).data
}

export type DomainEvent = {
  id: number | string | null
  event: string
  data: {
    aggregate_type: string
    aggregate_id: string
    payload?: Record<string, unknown>
    event_type?: string
    patient_id?: string
    created_at?: string
  }
}

async function streamEvents(
  path: string,
  onEvent: (event: DomainEvent) => void,
  options: { signal: AbortSignal; lastEventId?: number | string },
): Promise<void> {
  const response = await authenticatedFetch(
    `${import.meta.env.VITE_API_URL ?? ""}${path}`,
    {
      credentials: "same-origin",
      headers: {
        ...(options.lastEventId !== undefined
          ? { "Last-Event-ID": String(options.lastEventId) }
          : {}),
      },
      signal: options.signal,
    },
  )
  if (!response.ok || !response.body) {
    throw new Error(`Event stream failed with ${response.status}`)
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader()
  let buffer = ""
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += value
    const frames = buffer.split("\n\n")
    buffer = frames.pop() ?? ""
    for (const frame of frames) {
      if (!frame || frame.startsWith(":")) continue
      let id: number | string | null = null
      let event = "message"
      let data = "{}"
      for (const line of frame.split("\n")) {
        if (line.startsWith("id:")) {
          const rawId = line.slice(3).trim()
          id = /^\d+$/.test(rawId) ? Number(rawId) : rawId
        }
        if (line.startsWith("event:")) event = line.slice(6).trim()
        if (line.startsWith("data:")) data = line.slice(5).trim()
      }
      if (event === "session.revoked") {
        notifyAuthenticationRejection()
        await reader.cancel()
        return
      }
      onEvent({ id, event, data: JSON.parse(data) })
    }
  }
}

export function streamDomainEvents(
  onEvent: (event: DomainEvent) => void,
  options: { signal: AbortSignal; lastEventId?: number },
): Promise<void> {
  return streamEvents("/api/v1/events/stream", onEvent, options)
}

export function streamPatientEvents(
  patientId: string,
  onEvent: (event: DomainEvent) => void,
  options: { signal: AbortSignal; lastEventId?: number | string },
): Promise<void> {
  return streamEvents(
    `/api/v1/patients/${patientId}/portal-events/stream`,
    onEvent,
    options,
  )
}

export const authApi = {
  demoLogin,
  passwordLogin,
  me,
  logout,
  acceptInvitation,
}
export const patientInvitationApi = {
  preview: previewPatientInvitation,
  accept: acceptPatientInvitation,
  requestEnrollmentOtp: requestPatientEnrollmentOtp,
  acceptPhone: acceptPatientPhoneInvitation,
}
export const patientAccessApi = {
  requestLoginOtp: requestPatientLoginOtp,
  verifyOtp: verifyPatientLoginOtp,
}
export const adminApi = {
  memberships,
  createMembership,
  deactivateMembership,
  auditEvents,
  clinicAISettings,
  updateClinicAISettings,
}
export const patientSafeApi = {
  me,
  patients,
  timeline: patientTimeline,
  glance: patientSafeGlance,
  publicationReceipts: patientPublicationReceipts,
  acknowledgePublication: acknowledgePatientPublication,
  portalEvents: patientPortalEvents,
  streamEvents: streamPatientEvents,
  resolveProvenance,
  createInsight: createPatientInsight,
}
export const clinicalApi = {
  teamMembers,
  patients,
  patientDirectory,
  timeline: clinicalTimeline,
  readEntry: readClinicalEntry,
  heartbeatEditorPresence,
  glance,
  createEntry,
  patchEntry,
  versions,
  diff,
  revert,
  comments,
  createComment,
  reply,
  resolveComment,
  unresolveComment,
  assignComment,
  acceptHighlight,
  rejectHighlight,
  pinHighlight,
  dismissHighlight,
  decisionExplanation,
  requestHighlightReview,
  reaffirmHighlightSupport: (highlightId: string) =>
    resolveHighlightSupportReview(highlightId, "reaffirm"),
  supersedeHighlightSupport: (highlightId: string) =>
    resolveHighlightSupportReview(highlightId, "supersede"),
  recordImportanceImpression,
  resolveProvenance,
  duplicateCheck,
  createPatientRecord,
  patientDetail,
  invitePatient,
  revokePatientAccess,
  recoverPatientAccess,
  notificationDeliveries,
  createAppointmentNotification,
  resendNotification,
  revokeNotification,
  patientClinicalFacts,
  patientConflicts,
  resolveConflict,
  requestPatientSharing,
  patientSharingRequests,
  patientPublications,
  approvePatientSharing,
  withdrawPatientPublication,
  correctPatientPublication,
  liveSafetyAlerts,
  confirmLiveSafetyAlert: (alertId: string) =>
    updateLiveSafetyAlert(alertId, "confirm"),
  dismissLiveSafetyAlert: (alertId: string) =>
    updateLiveSafetyAlert(alertId, "dismiss"),
}
