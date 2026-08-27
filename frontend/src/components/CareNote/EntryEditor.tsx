import { useQuery } from "@tanstack/react-query"
import { EditorContent, useEditor } from "@tiptap/react"
import StarterKit from "@tiptap/starter-kit"
import { Link2, LoaderCircle, MessageSquarePlus, Save, X } from "lucide-react"
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
import { VersionConflictDialog } from "./VersionConflictDialog"

export type EntryDraft = {
  title: string
  content: string
  patient_facing: boolean
}

type EntryEditorProps = {
  initialDraft: EntryDraft
  versionId: string
  currentVersionId?: string
  onSave: (draft: EntryDraft, baseVersionId: string) => Promise<void>
  onCancel: () => void
  onCreateComment?: (body: CommentCreate) => Promise<CommentPublic>
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

export function EntryEditor({
  initialDraft,
  versionId,
  currentVersionId = versionId,
  onSave,
  onCancel,
  onCreateComment,
  onReviewVersions,
}: EntryEditorProps) {
  const baseVersionId = useRef(versionId).current
  const [draft, setDraft] = useState(initialDraft)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [conflict, setConflict] = useState<VersionConflict | null>(null)
  const [captured, setCaptured] = useState<CapturedSelection | null>(null)
  const [commentBody, setCommentBody] = useState("")
  const [mentionUserId, setMentionUserId] = useState("")
  const [assignmentMembershipId, setAssignmentMembershipId] = useState("")
  const [commentPending, setCommentPending] = useState(false)
  const [activeCommentId, setActiveCommentId] = useState<string | null>(null)
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

  return (
    <div className="space-y-4">
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
        draftTitle={draft.title}
        onOpenChange={(open) => !open && setConflict(null)}
        onReviewVersions={onReviewVersions}
        open={conflict !== null}
      />
    </div>
  )
}
