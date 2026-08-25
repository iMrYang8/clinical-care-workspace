import { Copy, GitCompareArrows, TriangleAlert } from "lucide-react"

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

type VersionConflictDialogProps = {
  open: boolean
  conflict: VersionConflict | null
  draftTitle: string
  draftContent: string
  onOpenChange: (open: boolean) => void
  onReviewVersions?: () => void
}

export function VersionConflictDialog({
  open,
  conflict,
  draftTitle,
  draftContent,
  onOpenChange,
  onReviewVersions,
}: VersionConflictDialogProps) {
  const copyDraft = async () => {
    await navigator.clipboard.writeText(`${draftTitle}\n\n${draftContent}`)
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 font-serif text-2xl">
            <TriangleAlert className="text-amber-600" /> Version conflict
          </DialogTitle>
          <DialogDescription>
            Another session saved this entry first. Nightingale did not
            overwrite it, and your draft is still here.
          </DialogDescription>
        </DialogHeader>
        <Alert className="border-amber-200 bg-amber-50 text-amber-950">
          <AlertTitle>
            {conflict?.message ?? "The care note changed."}
          </AlertTitle>
          <AlertDescription>
            Current server version:{" "}
            {conflict?.current_version_id ?? "available after refresh"}
          </AlertDescription>
        </Alert>
        <div className="max-h-56 overflow-auto rounded-xl border bg-slate-50 p-4">
          <p className="font-semibold text-slate-900">{draftTitle}</p>
          <pre className="mt-2 whitespace-pre-wrap font-sans text-sm leading-6 text-slate-700">
            {draftContent}
          </pre>
        </div>
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
            <Button onClick={() => onOpenChange(false)}>Keep editing</Button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
