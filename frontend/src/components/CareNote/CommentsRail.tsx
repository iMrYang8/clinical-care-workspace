import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle2,
  LoaderCircle,
  MessageCircle,
  Reply,
  RotateCcw,
  UserCheck,
} from "lucide-react"
import { useMemo, useState } from "react"

import type { CommentPublic, MePublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import {
  apiErrorMessage,
  clinicalApi,
  type TeamMemberOption,
} from "@/features/api"
import { formatSingaporeDateTime } from "@/lib/dateTime"

type CommentsRailProps = {
  entryId: string | null
  entryVersionId: string | null
  currentUser: MePublic
  readOnly?: boolean
}

const roleLabel: Record<TeamMemberOption["role"], string> = {
  staff: "Care staff",
  clinician: "Clinician",
  admin: "Clinic admin",
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
  const teamQuery = useQuery({
    queryKey: ["team", "members"],
    queryFn: clinicalApi.teamMembers,
    enabled: Boolean(entryId),
    staleTime: 5 * 60 * 1000,
  })
  const teamMembers = teamQuery.data ?? []
  const userNames = useMemo(
    () =>
      new Map(
        teamMembers.map((member) => [
          member.user_id,
          member.full_name?.trim() || roleLabel[member.role],
        ]),
      ),
    [teamMembers],
  )
  const membershipNames = useMemo(
    () =>
      new Map(
        teamMembers.map((member) => [
          member.membership_id,
          member.full_name?.trim() || roleLabel[member.role],
        ]),
      ),
    [teamMembers],
  )
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
  const unresolveMutation = useMutation({
    mutationFn: clinicalApi.unresolveComment,
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
      if (!entryVersionId) throw new Error("Select a saved care note")
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
      <div className="rounded-2xl border border-dashed bg-card p-6 text-center">
        <MessageCircle className="mx-auto mb-3 text-muted-foreground" />
        <p className="font-medium text-foreground">
          Select a care timeline note
        </p>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Team discussions are visible only to authorized clinic members.
        </p>
      </div>
    )
  }

  return (
    <section
      aria-labelledby="comments-heading"
      className="rounded-2xl border bg-card"
    >
      <div className="flex items-center justify-between border-b p-4">
        <div>
          <p className="text-xs font-bold uppercase tracking-[0.16em] text-ai-muted-foreground">
            {readOnly ? "Read-only oversight" : "Care team only"}
          </p>
          <h2
            className="font-serif text-xl font-semibold"
            id="comments-heading"
          >
            Team discussion
          </h2>
        </div>
        <Badge variant="secondary">{comments.length}</Badge>
      </div>
      <div className="space-y-4 p-4">
        {commentsQuery.isLoading && (
          <LoaderCircle className="mx-auto animate-spin text-ai" />
        )}
        {commentsQuery.isError && (
          <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
            <AlertDescription>
              {apiErrorMessage(commentsQuery.error)}
            </AlertDescription>
          </Alert>
        )}
        {!commentsQuery.isLoading && roots.length === 0 && (
          <p className="py-4 text-center text-sm leading-6 text-muted-foreground">
            No discussion yet. Edit the note and select text to start one.
          </p>
        )}
        {roots.map((comment) => (
          <article
            className={`rounded-xl border p-3 ${
              comment.resolved_at ? "bg-muted/40 opacity-75" : "border-ai/40"
            }`}
            key={comment.id}
          >
            <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span>
                {userNames.get(comment.author_id) ?? "Care team member"}
              </span>
              <span>·</span>
              <time dateTime={comment.created_at}>
                {formatSingaporeDateTime(comment.created_at)}
              </time>
              {comment.review_required && (
                <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                  Review required
                </Badge>
              )}
            </div>
            <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground">
              {comment.body}
            </p>
            {comment.assigned_membership_id && (
              <p className="mt-2 text-xs text-ai-muted-foreground">
                Assigned to{" "}
                {membershipNames.get(comment.assigned_membership_id) ??
                  "care team member"}
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
                {comment.resolved_at && (
                  <Button
                    disabled={unresolveMutation.isPending}
                    onClick={() => unresolveMutation.mutate(comment.id)}
                    size="sm"
                    variant="ghost"
                  >
                    <RotateCcw /> Reopen
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
                className="ml-4 mt-3 border-l-2 border-ai/40 pl-3"
                key={reply.id}
              >
                <p className="text-xs text-muted-foreground">
                  {userNames.get(reply.author_id) ?? "Care team member"} ·{" "}
                  {formatSingaporeDateTime(reply.created_at)}
                </p>
                <p className="mt-1 text-sm leading-6">{reply.body}</p>
              </div>
            ))}
          </article>
        ))}

        <Dialog
          open={!readOnly && replyingTo !== null}
          onOpenChange={(open) => {
            if (!open && !replyMutation.isPending) {
              setReplyingTo(null)
              setReplyBody("")
            }
          }}
        >
          <DialogContent className="sm:max-w-md">
            <DialogHeader>
              <DialogTitle className="font-serif text-2xl">
                Reply to discussion
              </DialogTitle>
              <DialogDescription>
                Add context or answer the care team’s question.
              </DialogDescription>
            </DialogHeader>
            <form
              className="space-y-4"
              onSubmit={(event) => {
                event.preventDefault()
                if (replyingTo) replyMutation.mutate(replyingTo)
              }}
            >
              <div className="space-y-2">
                <Label htmlFor="discussion-reply">Reply</Label>
                <textarea
                  autoFocus
                  className="min-h-28 w-full rounded-lg border bg-background p-3 text-sm text-foreground outline-none focus:ring-2 focus:ring-primary"
                  id="discussion-reply"
                  onChange={(event) => setReplyBody(event.target.value)}
                  value={replyBody}
                />
              </div>
              <DialogFooter>
                <Button
                  disabled={replyMutation.isPending}
                  onClick={() => setReplyingTo(null)}
                  type="button"
                  variant="outline"
                >
                  Cancel
                </Button>
                <Button
                  disabled={!replyBody.trim() || replyMutation.isPending}
                  type="submit"
                >
                  Send reply
                </Button>
              </DialogFooter>
            </form>
          </DialogContent>
        </Dialog>

        {(resolveMutation.isError ||
          unresolveMutation.isError ||
          assignMutation.isError ||
          replyMutation.isError) && (
          <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
            <AlertDescription>
              {apiErrorMessage(
                resolveMutation.error ??
                  unresolveMutation.error ??
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
