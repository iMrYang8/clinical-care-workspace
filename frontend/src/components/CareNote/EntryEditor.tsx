import { useQuery } from "@tanstack/react-query"
import { EditorContent, useEditor } from "@tiptap/react"
import StarterKit from "@tiptap/starter-kit"
import {
  Link2,
  LoaderCircle,
  MessageSquarePlus,
  Save,
  Users,
  X,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import type { CommentCreate, CommentPublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  apiErrorMessage,
  clinicalApi,
  type TeamMemberOption,
  type VersionConflict,
  versionConflictFrom,
} from "@/features/api"
import type { CanonicalAnchor } from "@/features/care-note/anchors"
import {
  createCommentExtension,
  markCommentSelection,
  selectionToCanonicalAnchor,
} from "@/features/care-note/CommentAnchorAdapter"
import {
  currentOtherEditors,
  type EditorPresenceRecord,
} from "@/features/editorPresence"
import { VersionConflictDialog } from "./VersionConflictDialog"

export type EntryDraft = {
  title: string
  content: string
  patient_facing: boolean
}

type EntryEditorProps = {
  actorId: string
  entryId: string
  initialDraft: EntryDraft
  versionId: string
  currentVersionId?: string
  editorPresence?: EditorPresenceRecord[]
  onSave: (draft: EntryDraft, baseVersionId: string) => Promise<void>
  onLoadLatest?: () => Promise<{
    draft: EntryDraft
    versionId: string
  }>
  onCancel: () => void
  onCreateComment?: (body: CommentCreate) => Promise<CommentPublic>
  onPresence?: (presence: EditorPresenceRecord) => void
  onReviewVersions?: () => void
}

type CapturedSelection = {
  anchor: CanonicalAnchor
  from: number
  to: number
}

function textDocument(content: string) {
  return {
    type: "doc",
    content: content.split("\n").map((line) => ({
      type: "paragraph",
      content: line ? [{ type: "text", text: line }] : undefined,
    })),
  }
}

const roleLabel: Record<TeamMemberOption["role"], string> = {
  staff: "Care staff",
  clinician: "Clinician",
  admin: "Clinic admin",
}

function memberLabel(member: TeamMemberOption): string {
  return `${member.full_name?.trim() || roleLabel[member.role]} — ${roleLabel[member.role]}`
}

const PRESENCE_HEARTBEAT_MS = 20_000
const PRESENCE_EXPIRY_REFRESH_MS = 5_000

function presenceDisplayName(editor: EditorPresenceRecord): string {
  return (
    editor.actor_display_name ??
    (editor.actor_role === "clinician"
      ? "Another clinician"
      : "Another care staff member")
  )
}

function presenceMessage(editors: EditorPresenceRecord[]): string {
  const names = editors.slice(0, 2).map(presenceDisplayName)
  if (editors.length === 1)
    return `${names[0]} is also editing this saved version.`
  if (editors.length === 2)
    return `${names[0]} and ${names[1]} are also editing this saved version.`
  return `${names[0]}, ${names[1]}, and ${editors.length - 2} other care team ${editors.length === 3 ? "member" : "members"} are also editing this saved version.`
}

export function EntryEditor({
  actorId,
  entryId,
  initialDraft,
  versionId,
  currentVersionId = versionId,
  editorPresence = [],
  onSave,
  onLoadLatest,
  onCancel,
  onCreateComment,
  onPresence,
  onReviewVersions,
}: EntryEditorProps) {
  const [baseVersionId, setBaseVersionId] = useState(versionId)
  const observedCurrentVersionId = useRef(currentVersionId)
  const [draft, setDraft] = useState(initialDraft)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState<VersionConflict | null>(null)
  const [latestForReconciliation, setLatestForReconciliation] = useState<{
    draft: EntryDraft
    versionId: string
  } | null>(null)
  const [loadingLatest, setLoadingLatest] = useState(false)
  const [captured, setCaptured] = useState<CapturedSelection | null>(null)
  const [commentBody, setCommentBody] = useState("")
  const [mentionUserId, setMentionUserId] = useState("")
  const [assignmentMembershipId, setAssignmentMembershipId] = useState("")
  const [commentPending, setCommentPending] = useState(false)
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
  const [presenceNow, setPresenceNow] = useState(() => Date.now())
  const teamQuery = useQuery({
    queryKey: ["team", "members"],
    queryFn: clinicalApi.teamMembers,
    enabled: Boolean(onCreateComment),
    staleTime: 5 * 60 * 1000,
  })
  const teamMembers = teamQuery.data ?? []
  const assignableMembers = teamMembers.filter(
    (member) => member.role === "staff" || member.role === "clinician",
  )

  const editor = useEditor({
    // Content is the backend's canonical plaintext projection. Disable visual
    // formatting that cannot be round-tripped until rich JSON has a separate
    // persisted field; the comment mark remains an interaction-only adapter.
    extensions: [
      StarterKit.configure({
        blockquote: false,
        bold: false,
        bulletList: false,
        code: false,
        codeBlock: false,
        heading: false,
        horizontalRule: false,
        italic: false,
        listItem: false,
        orderedList: false,
        strike: false,
      }),
      createCommentExtension(setActiveCommentId),
    ],
    content: textDocument(initialDraft.content),
    editorProps: {
      attributes: {
        class:
          "min-h-44 rounded-b-xl px-4 py-3 text-[0.95rem] leading-7 text-foreground outline-none focus-visible:ring-2 focus-visible:ring-primary",
        "aria-label": "Care note content",
      },
    },
    onUpdate: ({ editor: currentEditor }) => {
      setDraft((current) => ({
        ...current,
        content: currentEditor.getText({ blockSeparator: "\n" }),
      }))
    },
  })

  useEffect(() => () => editor?.destroy(), [editor])

  useEffect(() => {
    const interval = window.setInterval(
      () => setPresenceNow(Date.now()),
      PRESENCE_EXPIRY_REFRESH_MS,
    )
    return () => window.clearInterval(interval)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    const heartbeat = async () => {
      try {
        const presence = await clinicalApi.heartbeatEditorPresence(
          entryId,
          baseVersionId,
          controller.signal,
        )
        if (!controller.signal.aborted) onPresence?.(presence)
      } catch {
        // Presence is advisory. ETag enforcement remains the save boundary
        // when a heartbeat is delayed or the event stream is unavailable.
      }
    }
    void heartbeat()
    const interval = window.setInterval(heartbeat, PRESENCE_HEARTBEAT_MS)
    return () => {
      controller.abort()
      window.clearInterval(interval)
    }
  }, [baseVersionId, entryId, onPresence])

  useEffect(() => {
    if (currentVersionId === observedCurrentVersionId.current) return
    observedCurrentVersionId.current = currentVersionId
    if (currentVersionId === baseVersionId) return
    setConflict({
      code: "VERSION_CONFLICT",
      message:
        "This note changed while your draft was open. Review the latest saved note before continuing.",
      current_version_id: currentVersionId,
    })
  }, [baseVersionId, currentVersionId])

  const save = async () => {
    setError(null)
    setIsSaving(true)
    try {
      await onSave(draft, baseVersionId)
    } catch (caught) {
      const nextConflict = versionConflictFrom(caught)
      if (nextConflict) setConflict(nextConflict)
      else setError(apiErrorMessage(caught))
    } finally {
      setIsSaving(false)
    }
  }

  const loadLatest = async () => {
    if (!onLoadLatest) return
    setLoadingLatest(true)
    setError(null)
    try {
      setLatestForReconciliation(await onLoadLatest())
    } catch (caught) {
      setError(apiErrorMessage(caught))
    } finally {
      setLoadingLatest(false)
    }
  }

  const applyReconciled = (reconciled: EntryDraft) => {
    if (!latestForReconciliation) return
    setDraft(reconciled)
    editor?.commands.setContent(textDocument(reconciled.content))
    setBaseVersionId(latestForReconciliation.versionId)
    setLatestForReconciliation(null)
    setConflict(null)
    setError(null)
  }

  const captureComment = () => {
    if (!editor) return
    if (draft.content !== initialDraft.content) {
      setError(
        "Save your changes before starting a discussion on selected text.",
      )
      return
    }
    try {
      const { from, to } = editor.state.selection
      setCaptured({ anchor: selectionToCanonicalAnchor(editor), from, to })
      setError(null)
    } catch (caught) {
      setError(apiErrorMessage(caught))
    }
  }

  const submitComment = async () => {
    if (
      !editor ||
      !captured ||
      !onCreateComment ||
      !commentBody.trim() ||
      draft.content !== initialDraft.content
    )
      return
    setCommentPending(true)
    setError(null)
    try {
      const comment = await onCreateComment({
        entry_version_id: baseVersionId,
        ...captured.anchor,
        body: commentBody.trim(),
        mentioned_user_ids: mentionUserId.trim() ? [mentionUserId.trim()] : [],
        assigned_membership_id: assignmentMembershipId.trim() || null,
      })
      editor.commands.setTextSelection({ from: captured.from, to: captured.to })
      markCommentSelection(editor, comment.id)
      setCaptured(null)
      setCommentBody("")
      setMentionUserId("")
      setAssignmentMembershipId("")
    } catch (caught) {
      setError(apiErrorMessage(caught))
    } finally {
      setCommentPending(false)
    }
  }

  const otherEditors = currentOtherEditors(
    editorPresence,
    baseVersionId,
    actorId,
    presenceNow,
  )

  return (
    <div className="space-y-4">
      {otherEditors.length > 0 && (
        <Alert
          aria-live="polite"
          className="border-warning/40 bg-warning-muted text-warning-muted-foreground"
          role="status"
        >
          <Users className="size-4" />
          <AlertDescription>
            {presenceMessage(otherEditors)} Your draft remains local until you
            save; review any version conflict before publishing changes.
          </AlertDescription>
        </Alert>
      )}
      <div className="grid gap-2">
        <Label htmlFor={`entry-title-${baseVersionId}`}>Entry title</Label>
        <Input
          id={`entry-title-${baseVersionId}`}
          onChange={(event) =>
            setDraft((current) => ({ ...current, title: event.target.value }))
          }
          value={draft.title}
        />
      </div>

      <div>
        <div className="flex min-h-14 flex-wrap items-center gap-2 rounded-t-xl border border-b-0 bg-muted/40 p-2">
          <span className="px-2 text-xs text-muted-foreground">
            Select text to discuss with the care team
          </span>
          {onCreateComment && (
            <Button
              className="ml-auto min-h-11"
              disabled={draft.content !== initialDraft.content}
              onClick={captureComment}
              type="button"
              variant="outline"
            >
              <MessageSquarePlus /> Comment on selection
            </Button>
          )}
        </div>
        <div className="rounded-b-xl border bg-background">
          <EditorContent editor={editor} />
        </div>
        {activeCommentId && (
          <p className="mt-2 flex items-center gap-1 text-xs text-primary">
            <Link2 className="size-3" /> Discussion linked to selected text
          </p>
        )}
      </div>

      {captured && (
        <div className="space-y-3 rounded-xl border border-ai/40 bg-ai-muted p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="font-semibold text-ai-muted-foreground">
                Team discussion
              </p>
              <p className="mt-1 line-clamp-2 text-sm text-ai-muted-foreground">
                “{captured.anchor.exact_quote}”
              </p>
            </div>
            <Button
              aria-label="Cancel comment"
              onClick={() => setCaptured(null)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <X />
            </Button>
          </div>
          <div className="grid gap-2">
            <Label htmlFor={`comment-${baseVersionId}`}>Comment</Label>
            <textarea
              className="min-h-24 rounded-lg border bg-background px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-primary"
              id={`comment-${baseVersionId}`}
              onChange={(event) => setCommentBody(event.target.value)}
              placeholder="Add clinical context or a question…"
              value={commentBody}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor={`mention-${baseVersionId}`}>
                Mention (optional)
              </Label>
              <select
                className="h-10 rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                id={`mention-${baseVersionId}`}
                onChange={(event) => setMentionUserId(event.target.value)}
                value={mentionUserId}
              >
                <option value="">No mention</option>
                {assignableMembers.map((member) => (
                  <option key={member.user_id} value={member.user_id}>
                    {memberLabel(member)}
                  </option>
                ))}
              </select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`assign-${baseVersionId}`}>
                Assign to (optional)
              </Label>
              <select
                className="h-10 rounded-md border border-input bg-background px-3 text-sm text-foreground shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                id={`assign-${baseVersionId}`}
                onChange={(event) =>
                  setAssignmentMembershipId(event.target.value)
                }
                value={assignmentMembershipId}
              >
                <option value="">No assignment</option>
                {assignableMembers.map((member) => (
                  <option
                    key={member.membership_id}
                    value={member.membership_id}
                  >
                    {memberLabel(member)}
                  </option>
                ))}
              </select>
            </div>
          </div>
          {teamQuery.isError && (
            <p className="text-sm text-destructive">
              The care-team directory is temporarily unavailable.
            </p>
          )}
          <Button
            disabled={!commentBody.trim() || commentPending}
            onClick={submitComment}
            type="button"
          >
            {commentPending ? (
              <LoaderCircle className="animate-spin" />
            ) : (
              <MessageSquarePlus />
            )}
            Add to team discussion
          </Button>
        </div>
      )}

      <label className="flex min-h-11 items-center gap-3 rounded-xl border bg-muted/40 px-3 py-2 text-sm text-foreground">
        <input
          checked={draft.patient_facing}
          className="size-4 accent-primary"
          onChange={(event) =>
            setDraft((current) => ({
              ...current,
              patient_facing: event.target.checked,
            }))
          }
          type="checkbox"
        />
        Request patient sharing
      </label>

      {error && (
        <Alert
          className="border-critical/40 bg-critical-muted text-critical-muted-foreground"
          role="alert"
        >
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="flex flex-wrap justify-end gap-2">
        <Button onClick={onCancel} type="button" variant="ghost">
          Cancel
        </Button>
        <Button
          disabled={isSaving || !draft.title.trim() || !draft.content.trim()}
          onClick={save}
          type="button"
        >
          {isSaving ? <LoaderCircle className="animate-spin" /> : <Save />}
          Save changes
        </Button>
      </div>

      <VersionConflictDialog
        conflict={conflict}
        draftContent={draft.content}
        draftPatientFacing={draft.patient_facing}
        draftTitle={draft.title}
        latestDraft={latestForReconciliation?.draft}
        loadingLatest={loadingLatest}
        onApplyReconciled={applyReconciled}
        onLoadLatest={onLoadLatest ? loadLatest : undefined}
        onOpenChange={(open) => !open && setConflict(null)}
        onReviewVersions={onReviewVersions}
        open={conflict !== null}
      />
    </div>
  )
}
