import {
  Clock3,
  FileClock,
  MessageCircle,
  PencilLine,
  Sparkles,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import type { CommentCreate, CommentPublic, MePublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import type { ClinicalTimelineEntry } from "@/features/api"
import { type EntryDraft, EntryEditor } from "./EntryEditor"

export type SourceFocus = {
  entryId: string
  entryVersionId: string
  startOffset: number
  endOffset: number
}

type TimelineEntryCardProps = {
  entry: ClinicalTimelineEntry
  currentUser: MePublic
  sourceFocus?: SourceFocus | null
  onSave: (entry: ClinicalTimelineEntry, draft: EntryDraft) => Promise<void>
  onCreateComment: (
    entryId: string,
    body: CommentCreate,
  ) => Promise<CommentPublic>
  onOpenComments: (entry: ClinicalTimelineEntry) => void
  onOpenVersions: (entry: ClinicalTimelineEntry) => void
}

const sectionStyle: Record<string, string> = {
  staff: "border-blue-200 bg-blue-50 text-blue-800",
  clinician: "border-teal-200 bg-teal-50 text-teal-800",
  patient: "border-amber-200 bg-amber-50 text-amber-900",
  system: "border-violet-200 bg-violet-50 text-violet-800",
}

const entryTypeLabels: Record<string, string> = {
  manual_staff_note: "Manual Staff",
  manual_clinician_note: "Manual Clinician",
  manual_patient_insight: "Manual Patient",
  ai_doctor_consult_summary: "AI Doctor Consult",
  ai_nurse_consult_summary: "AI Nurse Consult",
  ai_patient_session_summary: "AI Patient Session",
  voice_transcript_source: "Voice Transcript Source",
  voice_reviewed_result: "Reviewed Voice Result",
  system_record: "System Record",
}

function originLabel(entry: ClinicalTimelineEntry): string {
  return entryTypeLabels[entry.entry_type] ?? entry.entry_type
}

function highlightedContent(
  entry: ClinicalTimelineEntry,
  focus?: SourceFocus | null,
) {
  if (!focus || focus.entryVersionId !== entry.version_id) return entry.content
  const points = Array.from(entry.content)
  return (
    <>
      {points.slice(0, focus.startOffset).join("")}
      <mark
        className="rounded bg-amber-200 px-0.5 text-slate-950"
        data-source-span
      >
        {points.slice(focus.startOffset, focus.endOffset).join("")}
      </mark>
      {points.slice(focus.endOffset).join("")}
    </>
  )
}

export function TimelineEntryCard({
  entry,
  currentUser,
  sourceFocus,
  onSave,
  onCreateComment,
  onOpenComments,
  onOpenVersions,
}: TimelineEntryCardProps) {
  const [editingEntry, setEditingEntry] =
    useState<ClinicalTimelineEntry | null>(null)
  const cardRef = useRef<HTMLElement>(null)
  const canEdit =
    entry.origin === "human" &&
    ((currentUser.role === "staff" && entry.section === "staff") ||
      (currentUser.role === "clinician" && entry.section === "clinician"))
  const focused = sourceFocus?.entryId === entry.id
  const editing = editingEntry !== null

  useEffect(() => {
    if (!focused) return
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
    cardRef.current?.focus({ preventScroll: true })
  }, [focused])

  return (
    <article
      aria-label={`${originLabel(entry)}: ${entry.title}`}
      className="scroll-mt-24 outline-none focus-visible:ring-2 focus-visible:ring-teal-600 focus-visible:ring-offset-4"
      data-entry-id={entry.id}
      data-entry-version-id={entry.version_id}
      ref={cardRef}
      tabIndex={-1}
    >
      <Card
        className={`overflow-hidden bg-white shadow-sm transition ${
          focused ? "border-amber-400 shadow-amber-100" : "border-slate-200"
        }`}
      >
        <CardHeader className="space-y-3 border-b bg-slate-50/70 pb-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <Badge
                  className={sectionStyle[entry.section] ?? sectionStyle.system}
                >
                  {(entry.origin === "ai" || entry.origin === "system") && (
                    <Sparkles aria-hidden="true" className="mr-1 size-3" />
                  )}
                  {originLabel(entry)}
                </Badge>
                <Badge variant="outline">v{entry.version_no}</Badge>
                {entry.patient_facing && (
                  <Badge className="bg-emerald-100 text-emerald-800">
                    Patient-facing
                  </Badge>
                )}
              </div>
              <h3 className="font-serif text-xl font-semibold text-slate-950">
                {entry.title}
              </h3>
              <p className="mt-1 flex items-center gap-1 text-xs text-slate-500">
                <Clock3 className="size-3" />
                <time dateTime={entry.occurred_at}>
                  {new Date(entry.occurred_at).toLocaleString()}
                </time>
              </p>
            </div>
            <div className="flex flex-wrap gap-1">
              {canEdit && !editing && (
                <Button
                  onClick={() => setEditingEntry(entry)}
                  size="sm"
                  variant="outline"
                >
                  <PencilLine /> Edit
                </Button>
              )}
              <Button
                onClick={() => onOpenVersions(entry)}
                size="sm"
                variant="ghost"
              >
                <FileClock /> Versions
              </Button>
              <Button
                onClick={() => onOpenComments(entry)}
                size="sm"
                variant="ghost"
              >
                <MessageCircle /> Comments
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-5">
          {editingEntry ? (
            <EntryEditor
              currentVersionId={entry.version_id}
              initialDraft={{
                title: editingEntry.title,
                content: editingEntry.content,
                patient_facing: editingEntry.patient_facing,
              }}
              onCancel={() => setEditingEntry(null)}
              onCreateComment={(body) => onCreateComment(editingEntry.id, body)}
              onReviewVersions={() => onOpenVersions(entry)}
              onSave={async (draft, baseVersionId) => {
                await onSave(
                  { ...editingEntry, version_id: baseVersionId },
                  draft,
                )
                setEditingEntry(null)
              }}
              versionId={editingEntry.version_id}
            />
          ) : (
            <p className="whitespace-pre-wrap text-[0.95rem] leading-7 text-slate-700">
              {highlightedContent(entry, sourceFocus)}
            </p>
          )}
          {(entry.origin === "ai" || entry.origin === "system") && (
            <div className="mt-4 rounded-xl border border-violet-100 bg-violet-50 p-3 text-sm leading-6 text-violet-900">
              AI system record · immutable. Clinical correction creates a
              separate superseding or conflicting entry; this text is never
              edited in place.
            </div>
          )}
        </CardContent>
      </Card>
    </article>
  )
}
