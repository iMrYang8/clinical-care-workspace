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

import type {
  ClinicalGlanceCard,
  DecisionExplanationPublic,
  RiskReason,
} from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { DismissReason } from "@/features/api"

type HighlightAction = "accept" | "pin"

type GlanceTopCardProps = {
  cards: ClinicalGlanceCard[]
  reviewCards?: ClinicalGlanceCard[]
  freshnessState?: "fresh" | "stale" | "expired" | string
  ageSeconds?: number | null
  providerOutage?: boolean
  outageMessage?: string | null
  fallbackKind?: "stored" | "rule_derived" | null | string
  importanceMode?: "shadow" | "disabled" | "active" | string
  canReview?: boolean
  canResolveSupport?: boolean
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
  onResolveSupport?: (
    card: ClinicalGlanceCard,
    resolution: "reaffirm" | "supersede",
  ) => void | Promise<void>
  onExplain?: (card: ClinicalGlanceCard) => Promise<DecisionExplanationPublic>
  onImpression?: (
    card: ClinicalGlanceCard,
    rank: number,
    viewEventId: string,
    surface: "current_priorities" | "clinical_review",
  ) => void | Promise<void>
  busyHighlightId?: string | null
}

const reasonLabel = {
  critical: "Critical risk",
  unresolved: "Unresolved clinical issue",
  clinician_confirmed: "Confirmed by clinician",
  clinical_entity: "Clinical fact requiring attention",
  clinic_feedback: "Raised by clinic feedback",
  recency: "Recent information",
  clinician_accepted: "Accepted by clinician",
  care_plan_conflict: "Conflicting care plan",
  clinician_confirmed_follow_up: "Clinician-confirmed follow-up",
  medication_status_conflict: "Medication status conflict",
  open_medication_reconciliation: "Medication reconciliation open",
  scheduled_follow_up: "Scheduled follow-up",
  synthetic_dataset_recent_encounter: "Recent imported encounter",
  unavailable_review_required: "Reason unavailable · review required",
} satisfies Record<RiskReason, string>

export function riskReasonLabel(reason: RiskReason): string {
  return reasonLabel[reason]
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

function viewEventId(highlightId: string, surface: string) {
  const key = `nightingale:priority-view:${surface}:${highlightId}`
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
  surface,
  onImpression,
  children,
}: {
  card: ClinicalGlanceCard
  rank: number
  surface: "current_priorities" | "clinical_review"
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
            void onImpression(
              card,
              rank,
              viewEventId(card.highlight_id, surface),
              surface,
            )
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
  }, [card, onImpression, rank, surface])

  return <li ref={ref}>{children}</li>
}

function DecisionDetails({
  card,
  onExplain,
  importanceMode,
}: {
  card: ClinicalGlanceCard
  onExplain?: GlanceTopCardProps["onExplain"]
  importanceMode: GlanceTopCardProps["importanceMode"]
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
  const currentConfidenceState =
    details?.current_confidence_state ??
    (recordValue(confidence, "band", "unavailable") === "unavailable"
      ? "unavailable"
      : "qualified")

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
          {importanceMode === "shadow" ? (
            <p className="mt-1">
              Importance learning is in shadow mode. Candidate exposure and
              feedback are recorded for evaluation, but ranking weights are not
              changed. The displayed score remains rule-based and is not a
              personal profile.
            </p>
          ) : importanceMode === "active" ? (
            <p className="mt-1">
              Ranking combines a rule-based score of{" "}
              {scoreLabel(ruleBasedImportance)} with a clinic feedback
              adjustment of {scoreLabel(learnedAdjustment, true)}. The
              adjustment is shared across this clinic; it is not a personal
              profile.
            </p>
          ) : importanceMode === "disabled" ? (
            <p className="mt-1">
              Importance learning is disabled. Ranking uses rule-based scores;
              no feedback adjustment is applied.
            </p>
          ) : (
            <p className="mt-1 font-medium text-warning-muted-foreground">
              Importance mode is unavailable. Treat this ranking as review
              required until the clinic configuration can be verified.
            </p>
          )}
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
          {currentConfidenceState !== "qualified" && (
            <p className="font-medium text-warning-muted-foreground">
              Current confidence is {currentConfidenceState.replace(/_/g, " ")}.
              Requalification failed or is incomplete, so this item remains
              review required.
            </p>
          )}
          {(details?.confidence_qualification_reasons?.length ?? 0) > 0 && (
            <p className="text-xs text-warning-muted-foreground">
              Requalification:{" "}
              {details?.confidence_qualification_reasons
                ?.map((reason) => reason.replace(/_/g, " "))
                .join(", ")}
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
  freshnessState = "fresh",
  ageSeconds = null,
  providerOutage = false,
  outageMessage = null,
  fallbackKind = null,
  importanceMode,
  canReview = false,
  canResolveSupport = false,
  onSource,
  onAction,
  onDismiss,
  onRequestReview,
  onResolveSupport,
  onExplain,
  onImpression,
  busyHighlightId,
}: GlanceTopCardProps) {
  const needsProtectedReview = (card: ClinicalGlanceCard) =>
    card.critical ||
    card.review_state !== "ready" ||
    card.support_review_required === true ||
    card.current_priority_eligible === false ||
    (card.importance as Record<string, unknown> | undefined)?.protected === true
  const visibleCards = [
    ...new Map(
      cards
        .filter((card) => !needsProtectedReview(card))
        .map((card) => [card.highlight_id, card]),
    ).values(),
  ].slice(0, 5)
  const protectedReviewCards = [
    ...new Map(
      [...reviewCards, ...cards.filter(needsProtectedReview)].map((card) => [
        card.highlight_id,
        card,
      ]),
    ).values(),
  ]
  const [dismissFor, setDismissFor] = useState<string | null>(null)
  const [dismissReason, setDismissReason] =
    useState<DismissReason>("not_relevant")

  return (
    <div className="space-y-4">
      {(freshnessState !== "fresh" || providerOutage || fallbackKind) && (
        <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
          <AlertTriangle className="size-4" />
          <AlertDescription>
            {providerOutage
              ? (outageMessage ??
                "AI processing is unavailable. Stored priorities remain visible with their age.")
              : freshnessState === "expired" || freshnessState === "unavailable"
                ? "This overview is unavailable and must not be used without clinical review."
                : "This overview is stale; verify it against the current care record."}
            {typeof ageSeconds === "number"
              ? ` Last generated ${Math.max(0, Math.round(ageSeconds / 60))} minutes ago.`
              : ""}
            {fallbackKind === "rule_derived"
              ? " New suggestions are rule-derived, clearly marked, and cannot be auto-published."
              : ""}
          </AlertDescription>
        </Alert>
      )}
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
                    surface="current_priorities"
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
                              {riskReasonLabel(card.risk_reason)}
                            </Badge>
                            <Badge className="bg-primary/10 text-primary">
                              {recordValue(
                                card.confidence,
                                "band",
                                "not applicable",
                              )}
                            </Badge>
                            <Badge variant="outline">
                              Source {card.support_state ?? "current"}
                            </Badge>
                            {(card.fallback_kind ?? fallbackKind) ===
                              "rule_derived" && (
                              <Badge className="bg-warning-muted text-warning-muted-foreground">
                                Rule-derived · review required
                              </Badge>
                            )}
                            {card.support_review_required && (
                              <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                                Source changed · reaffirm support
                              </Badge>
                            )}
                          </div>
                        </div>
                      </div>
                      <DecisionDetails
                        card={card}
                        importanceMode={importanceMode}
                        onExplain={onExplain}
                      />
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
                          {dismissReason === "too_busy_to_review" && (
                            <p className="text-xs text-muted-foreground">
                              This fatigue event is operational telemetry only;
                              it changes no learned counters or ranking weights.
                            </p>
                          )}
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

      {protectedReviewCards.length > 0 && (
        <Card className="border-warning/40 bg-warning-muted/20">
          <CardHeader>
            <div className="flex items-center gap-2">
              <ShieldAlert className="text-warning-muted-foreground" />
              <CardTitle className="font-serif text-xl">
                <h2>Needs clinical review</h2>
              </CardTitle>
            </div>
            <p className="text-sm text-muted-foreground">
              Critical, unresolved, pending-safety, clinician-confirmed, and
              source-changed items remain visible in this protected queue. It is
              not limited to five and cannot be shared with the patient until
              review passes.
            </p>
          </CardHeader>
          <CardContent>
            <ol className="space-y-3">
              {protectedReviewCards.map((card, index) => (
                <ObservedPriority
                  card={card}
                  key={card.highlight_id}
                  onImpression={onImpression}
                  rank={index + 1}
                  surface="clinical_review"
                >
                  <div className="space-y-3 rounded-xl border bg-card p-4">
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
                      <Badge variant="outline">
                        Source {card.support_state ?? "current"}
                      </Badge>
                      {card.fallback_kind === "rule_derived" && (
                        <Badge className="bg-warning-muted text-warning-muted-foreground">
                          Rule-derived · review required
                        </Badge>
                      )}
                    </div>
                    <DecisionDetails
                      card={card}
                      importanceMode={importanceMode}
                      onExplain={onExplain}
                    />
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
                      {canResolveSupport &&
                        card.support_review_required &&
                        onResolveSupport && (
                          <>
                            <Button
                              disabled={busyHighlightId === card.highlight_id}
                              onClick={() => onResolveSupport(card, "reaffirm")}
                              size="sm"
                            >
                              Reaffirm historical support
                            </Button>
                            <Button
                              disabled={busyHighlightId === card.highlight_id}
                              onClick={() =>
                                onResolveSupport(card, "supersede")
                              }
                              size="sm"
                              variant="outline"
                            >
                              Supersede this support
                            </Button>
                          </>
                        )}
                    </div>
                    {card.support_review_required && (
                      <p className="text-xs font-medium text-warning-muted-foreground">
                        The source entry changed. Compare the immutable original
                        wording with the current note before choosing either
                        action.
                      </p>
                    )}
                  </div>
                </ObservedPriority>
              ))}
            </ol>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
