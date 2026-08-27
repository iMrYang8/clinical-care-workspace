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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { ClinicalTimelineEntry } from "@/features/api"
import { formatSingaporeDateTime } from "@/lib/dateTime"
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
  authorName?: string | null
  authorRole?: "staff" | "clinician" | "admin" | null
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
  staff: "border-ai/40 bg-ai-muted text-ai-muted-foreground",
  clinician: "border-primary/40 bg-primary/10 text-primary",
  patient: "border-warning/40 bg-warning-muted text-warning-muted-foreground",
  system: "border-ai/40 bg-ai-muted text-ai-muted-foreground",
}

const entryTypeLabels: Record<string, string> = {
  manual_staff_note: "Care staff note",
  manual_clinician_note: "Clinical note",
  manual_patient_insight: "Patient insight",
  ai_doctor_consult_summary: "AI-assisted consultation note",
  ai_nurse_consult_summary: "AI-assisted nursing note",
  ai_patient_session_summary: "AI-assisted patient summary",
  voice_transcript_source: "Visit transcript",
  voice_reviewed_result: "Reviewed visit note",
  legacy_review_required: "Review required",
  system_record: "Care activity",
}

function originLabel(entry: ClinicalTimelineEntry): string {
  return entryTypeLabels[entry.entry_type] ?? "Care note"
}

function highlightedContent(
  entry: ClinicalTimelineEntry,
  focus?: SourceFocus | null,
) {
  const redundantPrefix =
    entry.origin === "ai"
      ? [
          "AI-assisted nursing draft: ",
          "AI-assisted patient-session draft: ",
          "AI-assisted review extracted that ",
        ].find((prefix) => entry.content.startsWith(prefix))
      : undefined
  const offset = redundantPrefix?.length ?? 0
  const rawContent = entry.content.slice(offset)
  const displayContent = rawContent
    ? rawContent.charAt(0).toLocaleUpperCase() + rawContent.slice(1)
    : rawContent
  if (!focus || focus.entryVersionId !== entry.version_id) return displayContent
  const points = Array.from(displayContent)
  const startOffset = Math.max(0, focus.startOffset - offset)
  const endOffset = Math.max(startOffset, focus.endOffset - offset)
  return (
    <>
      {points.slice(0, startOffset).join("")}
      <mark
        className="rounded bg-warning-muted px-0.5 text-warning-muted-foreground"
        data-source-span
      >
        {points.slice(startOffset, endOffset).join("")}
      </mark>
      {points.slice(endOffset).join("")}
    </>
  )
}

export function TimelineEntryCard({
  entry,
  currentUser,
  authorName,
  authorRole,
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
  const authorRoleLabel =
    authorRole === "clinician"
      ? "Clinician"
      : authorRole === "staff"
        ? "Care staff"
        : authorRole === "admin"
          ? "Clinic administrator"
          : null
  const authorLine =
    entry.origin === "system"
      ? "Care service"
      : entry.section === "patient"
        ? "Patient-reported"
        : authorName &&
            authorRoleLabel &&
            authorName.toLocaleLowerCase() !==
              authorRoleLabel.toLocaleLowerCase()
          ? `${authorName} · ${authorRoleLabel}`
          : (authorName ?? authorRoleLabel ?? "Care team member")

  useEffect(() => {
    if (!focused) return
    cardRef.current?.scrollIntoView({ behavior: "smooth", block: "center" })
    cardRef.current?.focus({ preventScroll: true })
  }, [focused])

  return (
    <article
      aria-label={`${originLabel(entry)}: ${entry.title}`}
      className="scroll-mt-24 outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-4 focus-visible:ring-offset-background"
      data-entry-id={entry.id}
      data-entry-origin={entry.origin}
      data-entry-type={entry.entry_type}
      data-entry-version-id={entry.version_id}
      ref={cardRef}
      tabIndex={-1}
    >
      <Card
        className={`overflow-hidden bg-card shadow-sm transition ${
          focused ? "border-warning shadow-warning/10" : "border-border"
        }`}
      >
        <CardHeader className="space-y-3 border-b bg-muted/40 pb-4">
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
                  <Badge className="bg-success-muted text-success-muted-foreground">
                    Patient-facing
                  </Badge>
                )}
              </div>
              <h3 className="font-serif text-xl font-semibold text-foreground">
                {entry.title}
              </h3>
              <p className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
                <Clock3 className="size-3" />
                {entry.origin !== "ai" && (
                  <>
                    <span>{authorLine}</span>
                    <span aria-hidden="true">·</span>
                  </>
                )}
                <time dateTime={entry.occurred_at}>
                  {formatSingaporeDateTime(entry.occurred_at)}
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
                <FileClock /> Change history
              </Button>
              <Button
                onClick={() => onOpenComments(entry)}
                size="sm"
                variant="ghost"
              >
                <MessageCircle /> Team discussion
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="p-5">
          <p className="whitespace-pre-wrap text-[0.95rem] leading-7 text-foreground/90">
            {highlightedContent(entry, sourceFocus)}
          </p>
          {entry.origin === "ai" && (
            <div className="mt-4 rounded-xl border border-ai/40 bg-ai-muted p-3 text-sm leading-6 text-ai-muted-foreground">
              AI-assisted draft. Review the cited source before using it for
              care decisions.
            </div>
          )}
        </CardContent>
      </Card>
      <Dialog
        open={editing}
        onOpenChange={(open) => !open && setEditingEntry(null)}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle className="font-serif text-2xl">Edit note</DialogTitle>
            <DialogDescription>
              Save changes as a new version. Previous versions remain in change
              history.
            </DialogDescription>
          </DialogHeader>
          {editingEntry && (
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
          )}
        </DialogContent>
      </Dialog>
    </article>
  )
}
