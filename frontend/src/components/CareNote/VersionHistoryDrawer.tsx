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

type VersionHistoryDrawerProps = {
  entryId: string
  currentVersionId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  onReverted: () => void | Promise<void>
}

export function VersionHistoryDrawer({
  entryId,
  currentVersionId,
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
        <SheetHeader className="border-b bg-slate-50 p-6 text-left">
          <SheetTitle className="flex items-center gap-2 font-serif text-2xl">
            <FileClock className="text-teal-700" /> Version history
          </SheetTitle>
          <SheetDescription>
            Immutable snapshots. Revert always creates a new version.
          </SheetDescription>
        </SheetHeader>
        <div className="space-y-5 p-6">
          {versionsQuery.isLoading && (
            <p className="flex items-center gap-2 text-sm text-slate-500">
              <LoaderCircle className="animate-spin" /> Loading versions…
            </p>
          )}
          {versionsQuery.isError && (
            <Alert className="border-red-200 bg-red-50 text-red-900">
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
                <li className="rounded-xl border bg-white p-4" key={version.id}>
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div>
                      <div className="flex items-center gap-2">
                        <p className="font-semibold text-slate-900">
                          Version {version.version_no} · {version.title}
                        </p>
                        {version.id === currentVersionId && (
                          <Badge className="bg-teal-100 text-teal-800">
                            Current
                          </Badge>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-slate-500">
                        {new Date(version.created_at).toLocaleString()} ·
                        SHA-256 {version.content_sha256.slice(0, 10)}…
                      </p>
                    </div>
                    {version.id !== currentVersionId && (
                      <Button
                        disabled={revertMutation.isPending}
                        onClick={() => revertMutation.mutate(version.id)}
                        size="sm"
                        variant="outline"
                      >
                        <RotateCcw /> Revert as new version
                      </Button>
                    )}
                  </div>
                  <p className="mt-3 line-clamp-3 whitespace-pre-wrap text-sm leading-6 text-slate-600">
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
                      Diff from
                    </Button>
                    <Button
                      onClick={() => setToVersion(version.id)}
                      size="sm"
                      variant={toVersion === version.id ? "secondary" : "ghost"}
                    >
                      Diff to
                    </Button>
                  </div>
                </li>
              ))}
          </ol>

          {fromVersion && toVersion && fromVersion !== toVersion && (
            <section className="rounded-xl border border-blue-200 bg-blue-50 p-4">
              <h3 className="flex items-center gap-2 font-semibold text-blue-950">
                <GitCompareArrows /> Unified diff
              </h3>
              {diffQuery.isLoading ? (
                <LoaderCircle className="mt-4 animate-spin text-blue-700" />
              ) : diffQuery.isError ? (
                <p className="mt-3 text-sm text-red-700">
                  {apiErrorMessage(diffQuery.error)}
                </p>
              ) : (
                <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-4 text-xs leading-5 text-slate-100">
                  {diffQuery.data?.unified_diff || "No textual changes."}
                </pre>
              )}
            </section>
          )}

          {revertMutation.isError && (
            <Alert className="border-red-200 bg-red-50 text-red-900">
              <AlertDescription>
                {apiErrorMessage(revertMutation.error)}
              </AlertDescription>
            </Alert>
          )}
          {current && (
            <p className="text-xs text-slate-500">
              Current version {current.version_no} remains unchanged until a
              revert request succeeds with its If-Match value.
            </p>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}
