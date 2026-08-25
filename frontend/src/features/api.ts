import { AxiosError } from "axios"
import type {
  AssignmentUpdate,
  CommentCreate,
  CommentPublic,
  DiffPublic,
  EntryCreate,
  EntryPatch,
  EntryPublic,
  EntryVersionPublic,
  GlancePublic,
  HighlightPublic,
  MePublic,
  PatientPublic,
  PatientTimelineEntry,
  ProvenanceResolved,
} from "@/client"
import {
  AuthService,
  CollaborationService,
  EntriesService,
  PatientsService,
  TrustService,
} from "@/client"

export const ACCESS_TOKEN_KEY = "nightingale_access_token"

export type DemoPersona = "patient" | "staff" | "clinician" | "admin"
export type ClinicalRole = MePublic["role"]

export type ClinicalTimelineEntry = PatientTimelineEntry & {
  origin: string
  author_id: string | null
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

export function getAccessToken(): string | null {
  return window.localStorage.getItem(ACCESS_TOKEN_KEY)
}

export function storeAccessToken(token: string): void {
  window.localStorage.setItem(ACCESS_TOKEN_KEY, token)
}

export function discardAccessToken(): void {
  window.localStorage.removeItem(ACCESS_TOKEN_KEY)
}

export function quotedEtag(versionId: string): string {
  return `"${versionId}"`
}

export function httpStatus(error: unknown): number | undefined {
  return error instanceof AxiosError ? error.response?.status : undefined
}

export function apiErrorMessage(error: unknown): string {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as
      | {
          detail?: string | { message?: string } | Array<{ msg?: string }>
          message?: string
        }
      | undefined
    if (typeof payload?.detail === "string") return payload.detail
    if (Array.isArray(payload?.detail)) {
      return payload.detail[0]?.msg ?? error.message
    }
    if (payload?.detail && "message" in payload.detail) {
      return payload.detail.message ?? error.message
    }
    return payload?.message ?? error.message
  }
  return error instanceof Error ? error.message : "Unexpected request error"
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
  const { data } = await AuthService.demoLogin({ body: { persona } })
  storeAccessToken(data.access_token)
}

async function me(): Promise<MePublic> {
  return (await AuthService.me()).data
}

async function logout(): Promise<void> {
  await AuthService.logout()
}

async function patients(): Promise<PatientPublic[]> {
  return (await PatientsService.patients()).data.data
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

async function glance(patientId: string): Promise<GlancePublic> {
  return (
    await PatientsService.patientGlance({ path: { patient_id: patientId } })
  ).data
}

async function patientSafeGlance(
  patientId: string,
): Promise<PatientSafeGlance> {
  const response = await glance(patientId)
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
    patient_facing: created.patient_facing,
    version_id: created.version_id,
    version_no: created.version_no,
    title: created.title,
    content: created.content,
    created_at: created.created_at,
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
  const token = getAccessToken()
  if (!token) throw new Error("A signed-in membership is required")
  const response = await fetch(
    `${import.meta.env.VITE_API_URL ?? ""}/api/v1/events/stream`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
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
      onEvent({ id, event, data: JSON.parse(data) })
    }
  }
}

export const authApi = { demoLogin, me, logout }
export const patientSafeApi = {
  me,
  patients,
  timeline: patientTimeline,
  glance: patientSafeGlance,
  resolveProvenance,
  createInsight: createPatientInsight,
}
export const clinicalApi = {
  patients,
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
  assignComment,
  acceptHighlight,
  rejectHighlight,
  pinHighlight,
  resolveProvenance,
}
