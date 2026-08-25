import { EditorContent, useEditor } from "@tiptap/react"
import StarterKit from "@tiptap/starter-kit"
import {
  Bold,
  Italic,
  Link2,
  LoaderCircle,
  MessageSquarePlus,
  Save,
  X,
} from "lucide-react"
import { useEffect, useState } from "react"

import type { CommentCreate, CommentPublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  apiErrorMessage,
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
  onSave: (draft: EntryDraft) => Promise<void>
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

export function EntryEditor({
  initialDraft,
  versionId,
  onSave,
  onCancel,
  onCreateComment,
  onReviewVersions,
}: EntryEditorProps) {
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

  const editor = useEditor({
    extensions: [StarterKit, createCommentExtension(setActiveCommentId)],
    content: textDocument(initialDraft.content),
    editorProps: {
      attributes: {
        class:
          "min-h-44 rounded-b-xl px-4 py-3 text-[0.95rem] leading-7 text-slate-800 outline-none focus-visible:ring-2 focus-visible:ring-teal-600",
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

  const save = async () => {
    setError(null)
    setIsSaving(true)
    try {
      await onSave(draft)
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
    try {
      const { from, to } = editor.state.selection
      setCaptured({ anchor: selectionToCanonicalAnchor(editor), from, to })
      setError(null)
    } catch (caught) {
      setError(apiErrorMessage(caught))
    }
  }

  const submitComment = async () => {
    if (!editor || !captured || !onCreateComment || !commentBody.trim()) return
    setCommentPending(true)
    setError(null)
    try {
      const comment = await onCreateComment({
        entry_version_id: versionId,
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
        <Label htmlFor={`entry-title-${versionId}`}>Entry title</Label>
        <Input
          id={`entry-title-${versionId}`}
          onChange={(event) =>
            setDraft((current) => ({ ...current, title: event.target.value }))
          }
          value={draft.title}
        />
      </div>

      <div>
        <div className="flex flex-wrap items-center gap-1 rounded-t-xl border border-b-0 bg-slate-50 p-2">
          <Button
            aria-label="Bold"
            className="min-h-11 min-w-11"
            onClick={() => editor?.chain().focus().toggleBold().run()}
            size="icon"
            type="button"
            variant={editor?.isActive("bold") ? "secondary" : "ghost"}
          >
            <Bold />
          </Button>
          <Button
            aria-label="Italic"
            className="min-h-11 min-w-11"
            onClick={() => editor?.chain().focus().toggleItalic().run()}
            size="icon"
            type="button"
            variant={editor?.isActive("italic") ? "secondary" : "ghost"}
          >
            <Italic />
          </Button>
          {onCreateComment && (
            <Button
              className="ml-auto min-h-11"
              onClick={captureComment}
              type="button"
              variant="outline"
            >
              <MessageSquarePlus /> Comment on selection
            </Button>
          )}
        </div>
        <div className="rounded-b-xl border bg-white">
          <EditorContent editor={editor} />
        </div>
        {activeCommentId && (
          <p className="mt-2 flex items-center gap-1 text-xs text-teal-700">
            <Link2 className="size-3" /> Active comment{" "}
            {activeCommentId.slice(0, 8)}
          </p>
        )}
      </div>

      {captured && (
        <div className="space-y-3 rounded-xl border border-violet-200 bg-violet-50 p-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="font-semibold text-violet-950">Anchored comment</p>
              <p className="mt-1 line-clamp-2 text-sm text-violet-800">
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
            <Label htmlFor={`comment-${versionId}`}>Comment</Label>
            <textarea
              className="min-h-24 rounded-lg border bg-white px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-violet-500"
              id={`comment-${versionId}`}
              onChange={(event) => setCommentBody(event.target.value)}
              placeholder="Add clinical context or a question…"
              value={commentBody}
            />
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="grid gap-2">
              <Label htmlFor={`mention-${versionId}`}>
                Mention user ID (optional)
              </Label>
              <Input
                id={`mention-${versionId}`}
                onChange={(event) => setMentionUserId(event.target.value)}
                placeholder="Validated by clinic membership"
                value={mentionUserId}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor={`assign-${versionId}`}>
                Assign membership ID (optional)
              </Label>
              <Input
                id={`assign-${versionId}`}
                onChange={(event) =>
                  setAssignmentMembershipId(event.target.value)
                }
                placeholder="Staff or clinician membership"
                value={assignmentMembershipId}
              />
            </div>
          </div>
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
            Save anchored comment
          </Button>
        </div>
      )}

      <label className="flex min-h-11 items-center gap-3 rounded-xl border bg-slate-50 px-3 py-2 text-sm text-slate-700">
        <input
          checked={draft.patient_facing}
          className="size-4 accent-teal-700"
          onChange={(event) =>
            setDraft((current) => ({
              ...current,
              patient_facing: event.target.checked,
            }))
          }
          type="checkbox"
        />
        Patient-facing after clinical review
      </label>

      {error && (
        <Alert className="border-red-200 bg-red-50 text-red-900" role="alert">
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
          Save with If-Match
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
