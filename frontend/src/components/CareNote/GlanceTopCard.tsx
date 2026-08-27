import {
  AlertTriangle,
  Check,
  ChevronDown,
  Link2,
  Pin,
  ShieldAlert,
  ShieldCheck,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import type { ClinicalGlanceCard, DecisionExplanationPublic } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { DismissReason } from "@/features/api"

type HighlightAction = "accept" | "pin"

type GlanceTopCardProps = {
  cards: ClinicalGlanceCard[]
  reviewCards?: ClinicalGlanceCard[]
  canReview?: boolean
  onSource: (card: ClinicalGlanceCard) => void | Promise<void>
  onAction?: (
    card: ClinicalGlanceCard,
    action: HighlightAction,
  ) => void | Promise<void>
  onDismiss?: (
    card: ClinicalGlanceCard,
    reason: DismissReason,
  ) => void | Promise<void>
  onRequestReview?: (card: ClinicalGlanceCard) => void | Promise<void>
  onExplain?: (card: ClinicalGlanceCard) => Promise<DecisionExplanationPublic>
  onImpression?: (
    card: ClinicalGlanceCard,
    rank: number,
    viewEventId: string,
  ) => void | Promise<void>
  busyHighlightId?: string | null
}

const reasonLabel: Record<string, string> = {
  critical: "Critical risk",
  pinned: "Pinned by care team",
  clinician_accepted: "Confirmed by clinician",
  ai_scribed_review_required: "Review required",
  care_plan_conflict: "Conflicting care plan",
  recency: "Recent information",
}

const dismissReasons: Array<{ value: DismissReason; label: string }> = [
  { value: "not_relevant", label: "Not relevant" },
  { value: "outdated", label: "Outdated" },
  { value: "already_addressed", label: "Already addressed" },
  { value: "too_busy_to_review", label: "Too busy to review" },
]

function recordValue(
  source: Record<string, unknown> | undefined,
  key: string,
  fallback: string,
) {
  const value = source?.[key]
  return value === null || value === undefined ? fallback : String(value)
}

function listValue(source: Record<string, unknown> | undefined, key: string) {
  const value = source?.[key]
  return Array.isArray(value) ? value.map(String).join(", ") : "None"
}

function numericValue(
  source: Record<string, unknown> | undefined,
  key: string,
): number | null {
  const value = source?.[key]
  return typeof value === "number" && Number.isFinite(value) ? value : null
}

function scoreLabel(value: number | null, signed = false) {
  if (value === null) return "unavailable"
  const formatted = value.toFixed(3)
  return signed && value > 0 ? `+${formatted}` : formatted
}

function viewEventId(highlightId: string) {
  const key = `nightingale:priority-view:${highlightId}`
  try {
    const saved = window.sessionStorage.getItem(key)
    if (saved) return saved
    const generated = `priority:${crypto.randomUUID()}`
    window.sessionStorage.setItem(key, generated)
    return generated
  } catch {
    return `priority:${crypto.randomUUID()}`
  }
}

function ObservedPriority({
  card,
  rank,
  onImpression,
  children,
}: {
  card: ClinicalGlanceCard
  rank: number
  onImpression?: GlanceTopCardProps["onImpression"]
  children: React.ReactNode
}) {
  const ref = useRef<HTMLLIElement>(null)
  const recorded = useRef(false)

  useEffect(() => {
    if (!onImpression || !ref.current || recorded.current) return
    let timer: number | undefined
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.intersectionRatio < 0.5) {
          if (timer) window.clearTimeout(timer)
          timer = undefined
          return
        }
        if (!timer) {
          timer = window.setTimeout(() => {
            if (recorded.current) return
            recorded.current = true
            void onImpression(card, rank, viewEventId(card.highlight_id))
            observer.disconnect()
          }, 2_000)
        }
      },
      { threshold: [0.5] },
    )
    observer.observe(ref.current)
    return () => {
      observer.disconnect()
      if (timer) window.clearTimeout(timer)
    }
  }, [card, onImpression, rank])

  return <li ref={ref}>{children}</li>
}

function DecisionDetails({
  card,
  onExplain,
}: {
  card: ClinicalGlanceCard
  onExplain?: GlanceTopCardProps["onExplain"]
}) {
  const [details, setDetails] = useState<DecisionExplanationPublic | null>(null)
  const [loading, setLoading] = useState(false)
  const risk = (details?.risk ?? card.risk) as
    | Record<string, unknown>
    | undefined
  const confidence = (details?.confidence ?? card.confidence) as
    | Record<string, unknown>
    | undefined
  const importance = (details?.importance ?? card.importance) as
    | Record<string, unknown>
    | undefined
  const components = importance?.components as
    | Record<string, unknown>
    | undefined
  const learnedAdjustment = numericValue(components, "learned")
  const finalImportance =
    numericValue(components, "final") ?? numericValue(importance, "score")
  const ruleBasedImportance =
    finalImportance === null || learnedAdjustment === null
      ? null
      : finalImportance - learnedAdjustment

  return (
    <details
      className="group rounded-xl border border-border bg-muted/20 p-3 text-sm"
      onToggle={(event) => {
        if (!(event.currentTarget.open && onExplain && !details && !loading)) {
          return
        }
        setLoading(true)
        void onExplain(card)
          .then(setDetails)
          .finally(() => setLoading(false))
      }}
    >
      <summary className="flex cursor-pointer list-none items-center justify-between gap-2 font-semibold text-foreground">
        Why this decision?
        <ChevronDown className="size-4 transition group-open:rotate-180" />
      </summary>
      <div className="mt-3 space-y-3 leading-6 text-muted-foreground">
        <div>
          <p className="font-semibold text-foreground">What is it?</p>
          <p>
            Risk is {recordValue(risk, "effective", "standard")} with a rule
            floor of {recordValue(risk, "floor", "standard")}. Confidence is{" "}
            {recordValue(confidence, "band", "unavailable")}. Importance is{" "}
            {recordValue(importance, "score", "not scored")}.
          </p>
          <p className="mt-1">
            Ranking combines a rule-based score of{" "}
            {scoreLabel(ruleBasedImportance)} with a clinic feedback adjustment
            of {scoreLabel(learnedAdjustment, true)}. The adjustment is shared
            across this clinic; it is not a personal profile.
          </p>
        </div>
        <div>
          <p className="font-semibold text-foreground">
            How could it be wrong?
          </p>
          <p>
            Risk rules: {listValue(risk, "rule_ids")} (
            {recordValue(risk, "rule_version", "version unavailable")}).
            Calibration holdout lower bound:{" "}
            {recordValue(confidence, "lower_bound", "unavailable")}; samples:{" "}
            {recordValue(confidence, "sample_count", "unavailable")}
            {"; evaluation: "}
            {recordValue(confidence, "evaluation_set", "not qualified")}.
          </p>
        </div>
        <div>
          <p className="font-semibold text-foreground">
            What happens when it is wrong?
          </p>
          <p>
            {card.review_state === "ready"
              ? "The care team can trace the exact source and request another review."
              : "The system abstains, keeps this in clinical review, and blocks patient sharing."}
          </p>
          {card.abstention_reason && (
            <p className="font-medium text-warning-muted-foreground">
              Review reason: {card.abstention_reason.replace(/_/g, " ")}
            </p>
          )}
        </div>
        {loading && <p>Loading evaluation details…</p>}
      </div>
    </details>
  )
}

export function GlanceTopCard({
  cards,
  reviewCards = [],
  canReview = false,
  onSource,
  onAction,
  onDismiss,
  onRequestReview,
  onExplain,
  onImpression,
  busyHighlightId,
}: GlanceTopCardProps) {
  const visibleCards = cards.slice(0, 5)
  const [dismissFor, setDismissFor] = useState<string | null>(null)
  const [dismissReason, setDismissReason] =
    useState<DismissReason>("not_relevant")

  return (
    <div className="space-y-4">
      <Card className="gap-0 overflow-hidden border-border bg-gradient-to-b from-primary/10 via-card to-card py-0 shadow-sm shadow-primary/5">
        <CardHeader className="px-6 py-6">
          <div className="flex items-start justify-between gap-3">
            <div>
              <p className="mb-1 text-xs font-bold uppercase tracking-[0.18em] text-primary">
                Patient overview
              </p>
              <CardTitle className="font-serif text-2xl text-foreground">
                <h2>Current priorities</h2>
              </CardTitle>
            </div>
            <Badge className="bg-background text-primary shadow-none">
              {visibleCards.length}/5
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="p-0">
          {visibleCards.length === 0 ? (
            <div className="px-5 py-8 text-center">
              <ShieldCheck className="mx-auto mb-3 size-7 text-primary" />
              <p className="font-medium text-foreground">No ready priorities</p>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                Supported care information appears here after its source and
                decision checks pass.
              </p>
            </div>
          ) : (
            <ol
              aria-label="Top care highlights"
              className="divide-y divide-border"
            >
              {visibleCards.map((card, index) => {
                const isBusy = busyHighlightId === card.highlight_id
                return (
                  <ObservedPriority
                    card={card}
                    key={card.highlight_id}
                    onImpression={onImpression}
                    rank={index + 1}
                  >
                    <div className="space-y-3 p-4">
                      <div className="flex items-start gap-3">
                        <span className="grid size-7 shrink-0 place-items-center rounded-full bg-muted text-xs font-bold text-muted-foreground">
                          {index + 1}
                        </span>
                        <div className="min-w-0 flex-1">
                          <p className="font-medium leading-6 text-foreground">
                            {card.label}
                          </p>
                          <div className="mt-2 flex flex-wrap items-center gap-2">
                            <Badge
                              className={
                                card.critical
                                  ? "bg-critical-muted text-critical-muted-foreground"
                                  : "bg-muted text-muted-foreground"
                              }
                            >
                              {card.critical && (
                                <AlertTriangle className="mr-1 size-3" />
                              )}
                              {reasonLabel[card.risk_reason] ??
                                "Needs attention"}
                            </Badge>
                            <Badge className="bg-primary/10 text-primary">
                              {recordValue(
                                card.confidence,
                                "band",
                                "not applicable",
                              )}
                            </Badge>
                          </div>
                        </div>
                      </div>
                      <DecisionDetails card={card} onExplain={onExplain} />
                      <div className="flex flex-wrap gap-2">
                        <Button
                          onClick={() => onSource(card)}
                          size="sm"
                          variant="outline"
                        >
                          <Link2 /> View source
                        </Button>
                        {canReview && onAction && (
                          <>
                            <Button
                              disabled={isBusy}
                              onClick={() => onAction(card, "accept")}
                              size="sm"
                              variant="outline"
                            >
                              <Check /> Confirm
                            </Button>
                            <Button
                              aria-label={`Keep ${card.label} at top`}
                              disabled={isBusy}
                              onClick={() => onAction(card, "pin")}
                              size="sm"
                              variant="outline"
                            >
                              <Pin /> Keep at top
                            </Button>
                          </>
                        )}
                        {canReview && onDismiss && !card.critical && (
                          <Button
                            disabled={isBusy}
                            onClick={() => setDismissFor(card.highlight_id)}
                            size="sm"
                            variant="ghost"
                          >
                            Dismiss…
                          </Button>
                        )}
                      </div>
                      {dismissFor === card.highlight_id && (
                        <div className="flex flex-col gap-2 rounded-xl border bg-background p-3">
                          <label
                            className="text-sm font-medium"
                            htmlFor={`dismiss-${card.highlight_id}`}
                          >
                            Why are you dismissing this item?
                          </label>
                          <select
                            className="h-10 rounded-md border bg-background px-3 text-sm"
                            id={`dismiss-${card.highlight_id}`}
                            onChange={(event) =>
                              setDismissReason(
                                event.target.value as DismissReason,
                              )
                            }
                            value={dismissReason}
                          >
                            {dismissReasons.map((reason) => (
                              <option key={reason.value} value={reason.value}>
                                {reason.label}
                              </option>
                            ))}
                          </select>
                          <div className="flex gap-2">
                            <Button
                              onClick={() => {
                                if (onDismiss) {
                                  void onDismiss(card, dismissReason)
                                }
                                setDismissFor(null)
                              }}
                              size="sm"
                            >
                              Record dismissal
                            </Button>
                            <Button
                              onClick={() => setDismissFor(null)}
                              size="sm"
                              variant="ghost"
                            >
                              Cancel
                            </Button>
                          </div>
                        </div>
                      )}
                    </div>
                  </ObservedPriority>
                )
              })}
            </ol>
          )}
        </CardContent>
      </Card>

      {reviewCards.length > 0 && (
        <Card className="border-warning/40 bg-warning-muted/20">
          <CardHeader>
            <div className="flex items-center gap-2">
              <ShieldAlert className="text-warning-muted-foreground" />
              <CardTitle className="font-serif text-xl">
                <h2>Needs clinical review</h2>
              </CardTitle>
            </div>
            <p className="text-sm text-muted-foreground">
              Unverified items remain visible here but cannot be shared with the
              patient.
            </p>
          </CardHeader>
          <CardContent>
            <ol className="space-y-3">
              {reviewCards.map((card) => (
                <li
                  className="space-y-3 rounded-xl border bg-card p-4"
                  key={card.highlight_id}
                >
                  <div className="flex items-start justify-between gap-3">
                    <p className="font-medium">{card.label}</p>
                    <Badge
                      className={
                        card.critical
                          ? "bg-critical-muted text-critical-muted-foreground"
                          : "bg-warning-muted text-warning-muted-foreground"
                      }
                    >
                      {card.critical
                        ? "Critical · unverified"
                        : "Review required"}
                    </Badge>
                  </div>
                  <DecisionDetails card={card} onExplain={onExplain} />
                  <div className="flex flex-wrap gap-2">
                    <Button
                      onClick={() => onSource(card)}
                      size="sm"
                      variant="outline"
                    >
                      <Link2 /> View source
                    </Button>
                    {canReview && onRequestReview && (
                      <Button
                        onClick={() => onRequestReview(card)}
                        size="sm"
                        variant="outline"
                      >
                        Request clinician review
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
