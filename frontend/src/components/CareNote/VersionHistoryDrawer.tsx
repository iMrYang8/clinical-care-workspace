import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  FileClock,
  GitCompareArrows,
  LoaderCircle,
  RotateCcw,
} from "lucide-react"
import { useMemo, useState } from "react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { apiErrorMessage, clinicalApi } from "@/features/api"
import { formatSingaporeDateTime } from "@/lib/dateTime"

type VersionHistoryDrawerProps = {
  entryId: string
  entryOrigin: string
  entrySection: string
  currentVersionId: string
  canRevert: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
  onReverted: () => void | Promise<void>
}

export function VersionHistoryDrawer({
  entryId,
  entryOrigin,
  entrySection,
  currentVersionId,
  canRevert,
  open,
  onOpenChange,
  onReverted,
}: VersionHistoryDrawerProps) {
  const queryClient = useQueryClient()
  const [fromVersion, setFromVersion] = useState<string | null>(null)
  const [toVersion, setToVersion] = useState<string | null>(null)

  const versionsQuery = useQuery({
    queryKey: ["entries", entryId, "versions"],
    queryFn: () => clinicalApi.versions(entryId),
    enabled: open,
  })
  const teamQuery = useQuery({
    queryKey: ["team", "members"],
    queryFn: clinicalApi.teamMembers,
    enabled: open,
    staleTime: 5 * 60 * 1000,
  })
  const versions = versionsQuery.data ?? []
  const current = useMemo(
    () => versions.find((version) => version.id === currentVersionId),
    [currentVersionId, versions],
  )

  const diffQuery = useQuery({
    queryKey: ["entries", entryId, "diff", fromVersion, toVersion],
    queryFn: () => clinicalApi.diff(entryId, fromVersion!, toVersion!),
    enabled:
      open && Boolean(fromVersion && toVersion && fromVersion !== toVersion),
  })
  const visibleDiff = useMemo(() => {
    const diff = diffQuery.data?.unified_diff
    if (!diff) return "No textual changes."

    const versionLabel = (versionId: string) => {
      const version = versions.find((item) => item.id === versionId)
      return version ? `Version ${version.version_no}` : "Earlier version"
    }

    return diff
      .replace(/^---\s+.*$/m, `--- ${versionLabel(fromVersion ?? "")}`)
      .replace(/^\+\+\+\s+.*$/m, `+++ ${versionLabel(toVersion ?? "")}`)
  }, [diffQuery.data?.unified_diff, fromVersion, toVersion, versions])

  const revertMutation = useMutation({
    mutationFn: (targetVersionId: string) =>
      clinicalApi.revert(entryId, targetVersionId, currentVersionId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["entries", entryId] })
      await onReverted()
    },
  })

  return (
    <Sheet onOpenChange={onOpenChange} open={open}>
      <SheetContent className="w-full overflow-y-auto p-0 sm:max-w-2xl">
        <SheetHeader className="border-b bg-muted/40 p-6 text-left">
          <SheetTitle className="flex items-center gap-2 font-serif text-2xl">
            <FileClock className="text-primary" /> Change history
          </SheetTitle>
          <SheetDescription>
            Review earlier changes or restore a previous version without losing
            the record of what happened.
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-5 p-6">
          {versionsQuery.isLoading && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <LoaderCircle className="animate-spin" /> Loading versions…
            </p>
          )}
          {versionsQuery.isError && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>
                {apiErrorMessage(versionsQuery.error)}
              </AlertDescription>
            </Alert>
          )}
          <ol className="space-y-3">
            {versions
              .slice()
              .sort((left, right) => right.version_no - left.version_no)
              .map((version) => (
                <li className="rounded-xl border bg-card p-4" key={version.id}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="font-semibold text-foreground">
                          Version {version.version_no} · {version.title}
                        </p>
                        {version.id === currentVersionId && (
                          <Badge className="bg-primary/10 text-primary">
                            Current
                          </Badge>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {entryOrigin === "ai"
                          ? "AI-assisted draft"
                          : entryOrigin === "system"
                            ? "Care service"
                            : entrySection === "patient"
                              ? "Patient"
                              : (teamQuery.data?.find(
                                  (member) =>
                                    member.user_id === version.author_id,
                                )?.full_name ?? "Care team member")}
                        {" · "}
                        {formatSingaporeDateTime(version.created_at)}
                      </p>
                    </div>
                    {canRevert && version.id !== currentVersionId && (
                      <Button
                        disabled={revertMutation.isPending}
                        onClick={() => revertMutation.mutate(version.id)}
                        size="sm"
                        variant="outline"
                      >
                        <RotateCcw /> Restore this version
                      </Button>
                    )}
                  </div>
                  <p className="mt-3 line-clamp-3 whitespace-pre-wrap text-sm leading-6 text-foreground/80">
                    {version.content}
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button
                      onClick={() => setFromVersion(version.id)}
                      size="sm"
                      variant={
                        fromVersion === version.id ? "secondary" : "ghost"
                      }
                    >
                      Compare from
                    </Button>
                    <Button
                      onClick={() => setToVersion(version.id)}
                      size="sm"
                      variant={toVersion === version.id ? "secondary" : "ghost"}
                    >
                      Compare to
                    </Button>
                  </div>
                </li>
              ))}
          </ol>

          {fromVersion && toVersion && fromVersion !== toVersion && (
            <section className="rounded-xl border border-ai/40 bg-ai-muted/50 p-4">
              <h3 className="flex items-center gap-2 font-semibold text-ai-muted-foreground">
                <GitCompareArrows /> Changes
              </h3>
              {diffQuery.isLoading ? (
                <LoaderCircle className="mt-4 animate-spin text-ai" />
              ) : diffQuery.isError ? (
                <p className="mt-3 text-sm text-critical-muted-foreground">
                  {apiErrorMessage(diffQuery.error)}
                </p>
              ) : (
                <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-foreground p-4 text-xs leading-5 text-background">
                  {visibleDiff}
                </pre>
              )}
            </section>
          )}

          {revertMutation.isError && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>
                {apiErrorMessage(revertMutation.error)}
              </AlertDescription>
            </Alert>
          )}
          {current && (
            <p className="text-xs text-muted-foreground">
              Version {current.version_no} is the current saved note.
            </p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
