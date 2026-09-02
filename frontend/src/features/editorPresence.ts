import type { EditorPresencePublic } from "@/client"
import type { DomainEvent } from "@/features/api"

export type EditorPresenceRecord = EditorPresencePublic

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0
}

export function editorPresenceRecordFrom(
  value: unknown,
): EditorPresenceRecord | null {
  if (!value || typeof value !== "object") return null
  const candidate = value as Record<string, unknown>
  if (
    !nonEmptyString(candidate.clinic_id) ||
    !nonEmptyString(candidate.patient_id) ||
    !nonEmptyString(candidate.entry_id) ||
    !nonEmptyString(candidate.entry_version_id) ||
    !nonEmptyString(candidate.actor_id) ||
    (candidate.actor_role !== "staff" &&
      candidate.actor_role !== "clinician") ||
    !nonEmptyString(candidate.actor_display_name) ||
    !nonEmptyString(candidate.expires_at) ||
    !Number.isFinite(Date.parse(candidate.expires_at))
  )
    return null
  return {
    clinic_id: candidate.clinic_id,
    patient_id: candidate.patient_id,
    entry_id: candidate.entry_id,
    entry_version_id: candidate.entry_version_id,
    actor_id: candidate.actor_id,
    actor_role: candidate.actor_role,
    actor_display_name: candidate.actor_display_name.trim(),
    expires_at: candidate.expires_at,
  }
}

export function editorPresenceFromDomainEvent(
  event: DomainEvent,
): EditorPresenceRecord | null {
  if (
    event.event !== "editor_presence" ||
    event.data.aggregate_type !== "entry"
  )
    return null
  const presence = editorPresenceRecordFrom(event.data.payload)
  if (!presence || event.data.aggregate_id !== presence.entry_id) return null
  return presence
}

export function currentOtherEditors(
  records: EditorPresenceRecord[],
  entryVersionId: string,
  actorId: string,
  nowEpochMs: number,
): EditorPresenceRecord[] {
  const unique = new Map<string, EditorPresenceRecord>()
  for (const editor of records) {
    if (
      editor.entry_version_id !== entryVersionId ||
      editor.actor_id === actorId ||
      Date.parse(editor.expires_at) <= nowEpochMs
    )
      continue
    unique.set(editor.actor_id, editor)
  }
  return [...unique.values()]
}

export function editorPresenceKey(
  record: Pick<
    EditorPresenceRecord,
    "actor_id" | "entry_id" | "entry_version_id"
  >,
): string {
  return `${record.entry_id}:${record.entry_version_id}:${record.actor_id}`
}
