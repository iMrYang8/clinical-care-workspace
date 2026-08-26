import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle2,
  LoaderCircle,
  MessageCircle,
  Reply,
  UserCheck,
} from "lucide-react"
import { useMemo, useState } from "react"

import type { CommentPublic, MePublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { apiErrorMessage, clinicalApi } from "@/features/api"

type CommentsRailProps = {
  entryId: string | null
  entryVersionId: string | null
  currentUser: MePublic
  readOnly?: boolean
}

export function CommentsRail({
  entryId,
  entryVersionId,
  currentUser,
  readOnly = false,
}: CommentsRailProps) {
  const queryClient = useQueryClient()
  const [replyingTo, setReplyingTo] = useState<string | null>(null)
  const [replyBody, setReplyBody] = useState("")

  const commentsQuery = useQuery({
    queryKey: ["entries", entryId, "comments"],
    queryFn: () => clinicalApi.comments(entryId!),
    enabled: Boolean(entryId),
  })
  const comments = commentsQuery.data ?? []
  const roots = comments.filter((comment) => comment.parent_id === null)
  const replies = useMemo(() => {
    const grouped = new Map<string, CommentPublic[]>()
    for (const comment of comments) {
      if (!comment.parent_id) continue
      grouped.set(comment.parent_id, [
        ...(grouped.get(comment.parent_id) ?? []),
        comment,
      ])
    }
    return grouped
  }, [comments])

  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: ["entries", entryId, "comments"],
    })

  const resolveMutation = useMutation({
    mutationFn: clinicalApi.resolveComment,
    onSuccess: invalidate,
  })
  const assignMutation = useMutation({
    mutationFn: (commentId: string) =>
      clinicalApi.assignComment(commentId, {
        assigned_membership_id: currentUser.membership_id,
      }),
    onSuccess: invalidate,
  })
  const replyMutation = useMutation({
    mutationFn: (commentId: string) => {
      if (!entryVersionId) throw new Error("Select an immutable entry version")
      return clinicalApi.reply(commentId, {
        entry_version_id: entryVersionId,
        start_offset: 0,
        end_offset: 0,
        exact_quote: "",
        prefix: "",
        suffix: "",
        body: replyBody.trim(),
      })
    },
    onSuccess: async () => {
      setReplyingTo(null)
      setReplyBody("")
      await invalidate()
    },
  })

  if (!entryId) {
    return (
      <div className="rounded-2xl border border-dashed bg-white p-6 text-center">
        <MessageCircle className="mx-auto mb-3 text-slate-400" />
        <p className="font-medium text-slate-700">Select a timeline entry</p>
        <p className="mt-1 text-sm leading-6 text-slate-500">
          Internal comments remain scoped to clinical roles.
        </p>
      </div>
    )
  }

  return (
    <section
      aria-labelledby="comments-heading"
      className="rounded-2xl border bg-white"
    >
      <div className="flex items-center justify-between border-b p-4">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.16em] text-violet-700">
            {readOnly ? "Internal · read-only oversight" : "Internal only"}
          </p>
          <h2
            className="font-serif text-xl font-semibold"
            id="comments-heading"
          >
            Comments
          </h2>
        </div>
        <Badge variant="secondary">{comments.length}</Badge>
      </div>
      <div className="space-y-4 p-4">
        {commentsQuery.isLoading && (
          <LoaderCircle className="mx-auto animate-spin text-violet-600" />
        )}
        {commentsQuery.isError && (
          <Alert className="border-red-200 bg-red-50 text-red-900">
            <AlertDescription>
              {apiErrorMessage(commentsQuery.error)}
            </AlertDescription>
          </Alert>
        )}
        {!commentsQuery.isLoading && roots.length === 0 && (
          <p className="py-4 text-center text-sm leading-6 text-slate-500">
            No comments on this entry. Edit it and select text to start a
            thread.
          </p>
        )}
        {roots.map((comment) => (
          <article
            className={`rounded-xl border p-3 ${
              comment.resolved_at
                ? "bg-slate-50 opacity-75"
                : "border-violet-100"
            }`}
            key={comment.id}
          >
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <span>Author {comment.author_id.slice(0, 8)}</span>
              <span>·</span>
              <time dateTime={comment.created_at}>
                {new Date(comment.created_at).toLocaleString()}
              </time>
              {comment.review_required && (
                <Badge className="bg-amber-100 text-amber-800">
                  Review anchor
                </Badge>
              )}
            </div>
            <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-800">
              {comment.body}
            </p>
            {comment.assigned_membership_id && (
              <p className="mt-2 text-xs text-violet-700">
                Assigned {comment.assigned_membership_id.slice(0, 8)}
              </p>
            )}
            {!readOnly && (
              <div className="mt-3 flex flex-wrap gap-1">
                <Button
                  onClick={() => setReplyingTo(comment.id)}
                  size="sm"
                  variant="ghost"
                >
                  <Reply /> Reply
                </Button>
                {!comment.resolved_at && (
                  <Button
                    disabled={resolveMutation.isPending}
                    onClick={() => resolveMutation.mutate(comment.id)}
                    size="sm"
                    variant="ghost"
                  >
                    <CheckCircle2 /> Resolve
                  </Button>
                )}
                {comment.assigned_membership_id !==
                  currentUser.membership_id && (
                  <Button
                    disabled={assignMutation.isPending}
                    onClick={() => assignMutation.mutate(comment.id)}
                    size="sm"
                    variant="ghost"
                  >
                    <UserCheck /> Assign to me
                  </Button>
                )}
              </div>
            )}
            {(replies.get(comment.id) ?? []).map((reply) => (
              <div
                className="ml-4 mt-3 border-l-2 border-violet-200 pl-3"
                key={reply.id}
              >
                <p className="text-xs text-slate-500">
                  {reply.author_id.slice(0, 8)} ·{" "}
                  {new Date(reply.created_at).toLocaleString()}
                </p>
                <p className="mt-1 text-sm leading-6">{reply.body}</p>
              </div>
            ))}
            {!readOnly && replyingTo === comment.id && (
              <div className="mt-3 space-y-2 rounded-lg bg-violet-50 p-3">
                <Label htmlFor={`reply-${comment.id}`}>Reply</Label>
                <textarea
                  className="min-h-20 w-full rounded-lg border bg-white p-2 text-sm outline-none focus:ring-2 focus:ring-violet-500"
                  id={`reply-${comment.id}`}
                  onChange={(event) => setReplyBody(event.target.value)}
                  value={replyBody}
                />
                <div className="flex gap-2">
                  <Button
                    disabled={!replyBody.trim() || replyMutation.isPending}
                    onClick={() => replyMutation.mutate(comment.id)}
                    size="sm"
                  >
                    Send reply
                  </Button>
                  <Button
                    onClick={() => setReplyingTo(null)}
                    size="sm"
                    variant="ghost"
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </article>
        ))}

        {(resolveMutation.isError ||
          assignMutation.isError ||
          replyMutation.isError) && (
          <Alert className="border-red-200 bg-red-50 text-red-900">
            <AlertDescription>
              {apiErrorMessage(
                resolveMutation.error ??
                  assignMutation.error ??
                  replyMutation.error,
              )}
            </AlertDescription>
          </Alert>
        )}
      </div>
    </section>
  )
}
