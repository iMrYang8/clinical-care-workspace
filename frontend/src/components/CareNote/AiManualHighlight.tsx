import { useMutation, useQueryClient } from "@tanstack/react-query"
import { ListPlus, LoaderCircle, Quote, Sparkles } from "lucide-react"
import { type MouseEvent, type ReactNode, useRef, useState } from "react"

import { TrustService } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { apiErrorMessage } from "@/features/api"
import {
  type CanonicalAnchor,
  createCanonicalAnchor,
} from "@/features/care-note/anchors"

const REDUNDANT_AI_PREFIXES = [
  "AI-assisted nursing draft: ",
  "AI-assisted patient-session draft: ",
  "AI-assisted review extracted that ",
] as const

export type AiDisplayProjection = {
  content: string
  rawOffsetUtf16: number
  rawOffsetCodePoints: number
}

export function aiDisplayProjection(rawContent: string): AiDisplayProjection {
  const prefix = REDUNDANT_AI_PREFIXES.find((candidate) =>
    rawContent.startsWith(candidate),
  )
  const rawOffsetUtf16 = prefix?.length ?? 0
  return {
    content: rawContent.slice(rawOffsetUtf16),
    rawOffsetUtf16,
    rawOffsetCodePoints: Array.from(rawContent.slice(0, rawOffsetUtf16)).length,
  }
}

export function anchorFromDomRange(
  rawContent: string,
  rawOffsetUtf16: number,
  root: HTMLElement,
  range: Range,
): CanonicalAnchor {
  if (!root.contains(range.commonAncestorContainer) || range.collapsed) {
    throw new RangeError("Select wording from one AI-assisted note")
  }

  const beforeSelection = document.createRange()
  beforeSelection.selectNodeContents(root)
  beforeSelection.setEnd(range.startContainer, range.startOffset)
  const displayStartUtf16 = beforeSelection.toString().length
  const selectedUtf16Length = range.toString().length
  const anchor = createCanonicalAnchor(
    rawContent,
    rawOffsetUtf16 + displayStartUtf16,
    rawOffsetUtf16 + displayStartUtf16 + selectedUtf16Length,
  )
  if (!anchor.exact_quote.trim()) {
    throw new RangeError("Select clinical wording before creating a priority")
  }
  return anchor
}

type AiManualHighlightProps = {
  children: ReactNode
  enabled: boolean
  entryId: string
  entryType: string
  entryVersionId: string
  patientId: string
  rawContent: string
  rawOffsetUtf16: number
}

export function AiManualHighlight({
  children,
  enabled,
  entryId,
  entryType,
  entryVersionId,
  patientId,
  rawContent,
  rawOffsetUtf16,
}: AiManualHighlightProps) {
  const queryClient = useQueryClient()
  const sourceRef = useRef<HTMLFieldSetElement>(null)
  const [anchor, setAnchor] = useState<CanonicalAnchor | null>(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [selectionError, setSelectionError] = useState<string | null>(null)
  const [savedMessage, setSavedMessage] = useState("")

  const createMutation = useMutation({
    mutationFn: async () => {
      if (!anchor) {
        throw new RangeError("A source selection is required")
      }
      const exactLabel = anchor.exact_quote.replace(/\s+/g, " ").trim()
      const created = (
        await TrustService.createHighlight({
          path: { entry_id: entryId },
          body: {
            entry_version_id: entryVersionId,
            ...anchor,
            label: exactLabel,
            patient_facing: false,
            feature_keys: [`entry_type:${entryType}`],
            clinician_confirmed: true,
          },
        })
      ).data
      // A clinician explicitly chose this exact AI-assisted wording for the
      // five-card Current priorities projection. Keep that deliberate choice
      // at the top so a full existing projection cannot silently hide the
      // newly confirmed item immediately after the dialog says it was added.
      return (
        await TrustService.pin({
          path: { highlight_id: created.id },
        })
      ).data
    },
    onSuccess: async (created) => {
      setDialogOpen(false)
      setAnchor(null)
      setSavedMessage(`Added to Current priorities: ${created.label}`)
      window.getSelection()?.removeAllRanges()
      await queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "glance"],
      })
    },
  })

  const captureSelection = () => {
    if (!enabled || !sourceRef.current) return
    const selection = window.getSelection()
    if (selection?.rangeCount !== 1 || selection.isCollapsed) {
      return
    }
    try {
      const nextAnchor = anchorFromDomRange(
        rawContent,
        rawOffsetUtf16,
        sourceRef.current,
        selection.getRangeAt(0),
      )
      const exactLabel = nextAnchor.exact_quote.replace(/\s+/g, " ").trim()
      if (exactLabel.length > 500) {
        throw new RangeError(
          "Select 500 characters or fewer for one source-linked priority",
        )
      }
      setAnchor(nextAnchor)
      setSelectionError(null)
      setSavedMessage("")
    } catch (caught) {
      if (caught instanceof RangeError) setSelectionError(caught.message)
    }
  }

  const onMouseUp = (_event: MouseEvent<HTMLFieldSetElement>) =>
    captureSelection()
  if (!enabled) return children

  return (
    <div>
      <fieldset
        aria-label="Select exact wording from this AI-assisted note"
        className="m-0 min-w-0 border-0 p-0"
        data-testid={`ai-highlight-source-${entryId}`}
        onMouseUp={onMouseUp}
        ref={sourceRef}
      >
        {children}
      </fieldset>

      {anchor && (
        <div
          className="mt-4 flex flex-col gap-3 rounded-xl border border-primary/30 bg-primary/5 p-3 sm:flex-row sm:items-center sm:justify-between"
          data-testid={`ai-highlight-selection-${entryId}`}
        >
          <p className="min-w-0 text-sm text-foreground">
            <Quote className="mr-1 inline size-4 text-primary" />
            <span className="line-clamp-2">“{anchor.exact_quote}”</span>
          </p>
          <Button
            className="shrink-0"
            onClick={() => setDialogOpen(true)}
            size="sm"
          >
            <ListPlus /> Add to priorities
          </Button>
        </div>
      )}

      {selectionError && (
        <p className="mt-2 text-xs text-critical">{selectionError}</p>
      )}
      {savedMessage && (
        <p aria-live="polite" className="mt-2 text-xs text-success">
          {savedMessage}
        </p>
      )}

      <Dialog
        onOpenChange={(open) => {
          if (!createMutation.isPending) setDialogOpen(open)
        }}
        open={dialogOpen}
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 font-serif text-2xl">
              <Sparkles className="text-primary" /> Add source-linked priority
            </DialogTitle>
            <DialogDescription>
              The selected wording stays linked to this immutable note version.
              The new priority will be recorded as clinician-confirmed.
            </DialogDescription>
          </DialogHeader>

          {anchor && (
            <form
              className="space-y-4"
              onSubmit={(event) => {
                event.preventDefault()
                createMutation.mutate()
              }}
            >
              <blockquote className="rounded-xl border-l-4 border-primary bg-muted/50 p-3 text-sm leading-6 text-foreground">
                “{anchor.exact_quote}”
              </blockquote>
              <p className="text-xs leading-5 text-muted-foreground">
                The priority uses the selected source wording exactly. To
                paraphrase or correct it, create a separate clinical note so the
                new judgement has its own author and history.
              </p>
              {createMutation.isError && (
                <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
                  <AlertDescription>
                    {apiErrorMessage(createMutation.error)}
                  </AlertDescription>
                </Alert>
              )}
              <DialogFooter>
                <Button
                  disabled={createMutation.isPending}
                  onClick={() => setDialogOpen(false)}
                  type="button"
                  variant="outline"
                >
                  Cancel
                </Button>
                <Button disabled={createMutation.isPending} type="submit">
                  {createMutation.isPending ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <ListPlus />
                  )}
                  Add to Current priorities
                </Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
