import {
  Copy,
  GitCompareArrows,
  LoaderCircle,
  RefreshCw,
  TriangleAlert,
} from "lucide-react"
import { useEffect, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import type { VersionConflict } from "@/features/api"
import type { EntryDraft } from "./EntryEditor"

type VersionConflictDialogProps = {
  open: boolean
  conflict: VersionConflict | null
  draftTitle: string
  draftContent: string
  draftPatientFacing: boolean
  latestDraft?: EntryDraft | null
  loadingLatest?: boolean
  onOpenChange: (open: boolean) => void
  onLoadLatest?: () => void | Promise<void>
  onApplyReconciled?: (draft: EntryDraft) => void
  onReviewVersions?: () => void
}

export function VersionConflictDialog({
  open,
  conflict,
  draftTitle,
  draftContent,
  draftPatientFacing,
  latestDraft,
  loadingLatest = false,
  onOpenChange,
  onLoadLatest,
  onApplyReconciled,
  onReviewVersions,
}: VersionConflictDialogProps) {
  const [mergedDraft, setMergedDraft] = useState<EntryDraft | null>(null)
  useEffect(() => {
    if (!open) {
      setMergedDraft(null)
      return
    }
    if (latestDraft && !mergedDraft) {
      // Deliberately start from the local draft. Nightingale never performs a
      // silent textual merge of clinical wording; the editor must reconcile it.
      setMergedDraft({
        title: draftTitle,
        content: draftContent,
        patient_facing: draftPatientFacing,
      })
    }
  }, [
    draftContent,
    draftPatientFacing,
    draftTitle,
    latestDraft,
    mergedDraft,
    open,
  ])
  const copyDraft = async () => {
    await navigator.clipboard.writeText(`${draftTitle}\n\n${draftContent}`)
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-serif text-2xl">
            <TriangleAlert className="text-warning" /> Version conflict
          </DialogTitle>
          <DialogDescription>
            Another session saved this entry first. Nightingale did not
            overwrite it, and your draft is still here.
          </DialogDescription>
        </DialogHeader>
        <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
          <AlertTitle>
            {conflict?.message ?? "The care note changed."}
          </AlertTitle>
          <AlertDescription>
            Review the latest saved note before deciding how to update your
            draft.
          </AlertDescription>
        </Alert>
        {latestDraft && mergedDraft ? (
          <div className="grid gap-3 lg:grid-cols-3">
            <div className="max-h-64 overflow-auto rounded-xl border bg-muted/40 p-3">
              <p className="text-xs font-bold uppercase tracking-wide text-muted-foreground">
                Latest saved note
              </p>
              <p className="mt-2 font-semibold">{latestDraft.title}</p>
              <pre className="mt-2 whitespace-pre-wrap font-sans text-sm leading-6">
                {latestDraft.content}
              </pre>
              <p className="mt-2 text-xs text-muted-foreground">
                Patient sharing:{" "}
                {latestDraft.patient_facing ? "requested" : "not requested"}
              </p>
            </div>
            <div className="max-h-64 overflow-auto rounded-xl border bg-warning-muted/30 p-3">
              <p className="text-xs font-bold uppercase tracking-wide text-warning-muted-foreground">
                My local draft
              </p>
              <p className="mt-2 font-semibold">{draftTitle}</p>
              <pre className="mt-2 whitespace-pre-wrap font-sans text-sm leading-6">
                {draftContent}
              </pre>
              <p className="mt-2 text-xs text-muted-foreground">
                Patient sharing:{" "}
                {draftPatientFacing ? "requested" : "not requested"}
              </p>
            </div>
            <div className="space-y-2 rounded-xl border border-primary/30 bg-primary/5 p-3">
              <label
                className="text-xs font-bold uppercase tracking-wide text-primary"
                htmlFor="reconciled-title"
              >
                Editable reconciled result
              </label>
              <input
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                id="reconciled-title"
                onChange={(event) =>
                  setMergedDraft({ ...mergedDraft, title: event.target.value })
                }
                value={mergedDraft.title}
              />
              <textarea
                aria-label="Reconciled care note"
                className="min-h-40 w-full rounded-md border bg-background p-3 text-sm leading-6"
                onChange={(event) =>
                  setMergedDraft({
                    ...mergedDraft,
                    content: event.target.value,
                  })
                }
                value={mergedDraft.content}
              />
              <label className="flex items-center gap-2 text-xs font-medium">
                <input
                  checked={mergedDraft.patient_facing}
                  onChange={(event) =>
                    setMergedDraft({
                      ...mergedDraft,
                      patient_facing: event.target.checked,
                    })
                  }
                  type="checkbox"
                />
                Request patient sharing in the reconciled result
              </label>
              <p className="text-xs leading-5 text-muted-foreground">
                No automatic merge was applied. Compare both versions and edit
                this result before continuing.
              </p>
            </div>
          </div>
        ) : (
          <div className="max-h-56 overflow-auto rounded-xl border bg-muted/40 p-4">
            <p className="font-semibold text-foreground">{draftTitle}</p>
            <pre className="mt-2 whitespace-pre-wrap font-sans text-sm leading-6 text-foreground/90">
              {draftContent}
            </pre>
          </div>
        )}
        <DialogFooter className="gap-2 sm:justify-between">
          <Button onClick={copyDraft} variant="outline">
            <Copy /> Copy my draft
          </Button>
          <div className="flex gap-2">
            {onReviewVersions && (
              <Button onClick={onReviewVersions} variant="secondary">
                <GitCompareArrows /> Review versions
              </Button>
            )}
            {latestDraft && mergedDraft && onApplyReconciled ? (
              <Button onClick={() => onApplyReconciled(mergedDraft)}>
                <GitCompareArrows /> Continue with reconciled draft
              </Button>
            ) : onLoadLatest ? (
              <Button disabled={loadingLatest} onClick={onLoadLatest}>
                {loadingLatest ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <RefreshCw />
                )}
                Load latest and reconcile my draft
              </Button>
            ) : (
              <Button onClick={() => onOpenChange(false)}>Keep editing</Button>
            )}
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
