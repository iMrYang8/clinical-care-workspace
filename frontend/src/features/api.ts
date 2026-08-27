import { AxiosError } from "axios"
import type {
  AssignmentUpdate,
  AuditEventPublic,
  ClinicalGlancePublic,
  CommentCreate,
  CommentPublic,
  DecisionExplanationPublic,
  DiffPublic,
  EntryCreate,
  EntryPatch,
  EntryPublic,
  EntryVersionPublic,
  GlancePublic,
  HighlightPublic,
  MembershipCreate,
  MembershipInvitationPublic,
  MembershipPublic,
  MePublic,
  PatientPublic,
  PatientTimelineEntry,
  ProvenanceResolved,
  TeamMemberPublic,
} from "@/client"
import {
  AdminService,
  AuthService,
  CollaborationService,
  EntriesService,
  PatientsService,
  TeamService,
  TrustService,
} from "@/client"
import {
  authenticatedFetch,
  notifyAuthenticationRejection,
} from "@/features/authenticatedFetch"

export type DemoPersona = "patient" | "staff" | "clinician" | "admin"
export type ClinicalRole = MePublic["role"]

export type ClinicalTimelineEntry = PatientTimelineEntry & {
  origin: string
  author_id: string | null
}

export type PatientIdentityInput = {
  display_name: string
  date_of_birth: string
  medical_record_number: string
  identity_document_type: "nric_fin" | "passport" | "other"
  identity_document_number: string
}

export type PatientDuplicateCandidate = {
  patient_id: string
  display_name: string
  date_of_birth: string | null
  medical_record_number: string | null
  masked_identity_document: string | null
}

export type PatientDuplicateCheck = {
  status: "clear" | "possible_match" | "exact_match"
  candidates: PatientDuplicateCandidate[]
  duplicate_confirmation_token?: string | null
}

export type PatientDetail = PatientPublic & {
  date_of_birth: string | null
  medical_record_number: string | null
  identity_document_type: string | null
  masked_identity_document: string | null
  portal_access_state: "not_invited" | "pending" | "active" | "deactivated"
  status: string
}

export type ClinicalConflict = {
  id: string
  patient_id: string
  fact_type: string
  normalized_key: string
  severity: "high" | "critical" | string
  status: string
  left_entry_id: string
  right_entry_id: string
  left_pointer_id: string | null
  right_pointer_id: string | null
  resolution: string | null
  created_at: string
}

export type ClinicalFactAssertion = {
  id: string
  fact_type: string
  subject: string
  normalized_value: string
  clinical_status: string
  effective_time: string | null
  origin: "human" | "ai" | "voice" | string
  source_entry_version_id: string
  provenance_pointer_id: string
}

async function jsonRequest<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(
    `${import.meta.env.VITE_API_URL ?? ""}${url}`,
    { credentials: "same-origin", ...init },
  )
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    throw new Error(
      (payload as { detail?: string } | null)?.detail ??
        `Request failed with ${response.status}`,
    )
  }
  return (await response.json()) as T
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

async function invitePatient(patientId: string, email: string): Promise<void> {
  await jsonRequest(`/api/v1/patients/${patientId}/portal-invitations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
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

export type PatientInvitationPreview = {
  clinic_name: string
  patient_display_name: string
  email: string
  account_exists: boolean
}

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

export type ClinicAISetting = {
  provider: "openai"
  api_key_configured: boolean
  api_key_last4: string | null
  credential_source: "clinic" | "environment" | "none"
  fast_model: string
  careful_model: string
  transcribe_model: string
  updated_at: string | null
}

export type ClinicAISettingUpdate = {
  api_key?: string | null
  clear_api_key?: boolean
  fast_model: string
  careful_model: string
  transcribe_model: string
}

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

export type PatientDirectoryItem = PatientPublic & {
  date_of_birth: string | null
  medical_record_number: string | null
  same_name_count: number
  today_visit_at: string | null
  today_visit_status: string | null
  today_visit_type: string | null
  last_activity_at: string | null
}

export type PatientDirectoryPage = {
  data: PatientDirectoryItem[]
  count: number
  offset: number
  limit: number
}

async function patientDirectory(input: {
  search?: string
  visitScope?: "all" | "today" | "previous"
  offset?: number
  limit?: number
}): Promise<PatientDirectoryPage> {
  const query = new URLSearchParams()
  if (input.search) query.set("search", input.search)
  if (input.visitScope) query.set("visit_scope", input.visitScope)
  query.set("offset", String(input.offset ?? 0))
  query.set("limit", String(input.limit ?? 24))
  return jsonRequest(`/api/v1/patients?${query.toString()}`)
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

async function comments(entryId: string): Promise<CommentPublic[]> {
  return (
    await CollaborationService.listComments({ path: { entry_id: entryId } })
  ).data
}

async function createComment(
  entryId: string,
  body: CommentCreate,
): Promise<CommentPublic> {
  return (
    await CollaborationService.createComment({
      path: { entry_id: entryId },
      body,
    })
  ).data
}

async function reply(
  commentId: string,
  body: CommentCreate,
): Promise<CommentPublic> {
  return (
    await CollaborationService.reply({
      path: { comment_id: commentId },
      body,
    })
  ).data
}

async function resolveComment(commentId: string): Promise<CommentPublic> {
  return (
    await CollaborationService.resolve({ path: { comment_id: commentId } })
  ).data
}

async function unresolveComment(commentId: string): Promise<CommentPublic> {
  return jsonRequest(`/api/v1/comments/${commentId}/unresolve`, {
    method: "POST",
  })
}

async function assignComment(
  commentId: string,
  body: AssignmentUpdate,
): Promise<CommentPublic> {
  return (
    await CollaborationService.assign({
      path: { comment_id: commentId },
      body,
    })
  ).data
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

async function recordImportanceImpression(input: {
  highlightId: string
  viewEventId: string
  rank: number
  exposureProbability: number
  visibleRatio: number
  visibleDurationMs: number
}): Promise<void> {
  await TrustService.recordImportanceImpression({
    body: {
      highlight_id: input.highlightId,
      view_event_id: input.viewEventId,
      rank: input.rank,
      surface: "current_priorities",
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
  id: number | null
  event: string
  data: {
    aggregate_type: string
    aggregate_id: string
    payload: Record<string, unknown>
  }
}

export async function streamDomainEvents(
  onEvent: (event: DomainEvent) => void,
  options: { signal: AbortSignal; lastEventId?: number },
): Promise<void> {
  const response = await authenticatedFetch(
    `${import.meta.env.VITE_API_URL ?? ""}/api/v1/events/stream`,
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
      let id: number | null = null
      let event = "message"
      let data = "{}"
      for (const line of frame.split("\n")) {
        if (line.startsWith("id:")) id = Number(line.slice(3).trim())
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
  resolveProvenance,
  createInsight: createPatientInsight,
}
export const clinicalApi = {
  teamMembers,
  patients,
  patientDirectory,
  timeline: clinicalTimeline,
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
  recordImportanceImpression,
  resolveProvenance,
  duplicateCheck,
  createPatientRecord,
  patientDetail,
  invitePatient,
  patientClinicalFacts,
  patientConflicts,
  resolveConflict,
}
