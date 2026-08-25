import { AlertTriangle, Check, Link2, Pin, ShieldCheck, X } from "lucide-react"

import type { GlanceCard } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"

type HighlightAction = "accept" | "reject" | "pin"

type GlanceTopCardProps = {
  cards: GlanceCard[]
  canReview?: boolean
  onSource: (card: GlanceCard) => void | Promise<void>
  onAction?: (card: GlanceCard, action: HighlightAction) => void | Promise<void>
  busyHighlightId?: string | null
}

const reasonLabel: Record<string, string> = {
  critical: "Critical risk",
  pinned: "Pinned by care team",
  clinician_accepted: "Clinician accepted",
}

export function GlanceTopCard({
  cards,
  canReview = false,
  onSource,
  onAction,
  busyHighlightId,
}: GlanceTopCardProps) {
  const visibleCards = cards.slice(0, 5)

  return (
    <Card className="overflow-hidden border-teal-100 bg-white shadow-sm shadow-teal-900/5">
      <CardHeader className="border-b border-teal-100 bg-gradient-to-br from-teal-50 to-white pb-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="mb-1 text-xs font-bold uppercase tracking-[0.18em] text-teal-700">
              At a glance
            </p>
            <CardTitle
              aria-level={2}
              className="font-serif text-2xl text-slate-950"
              role="heading"
            >
              What matters now
            </CardTitle>
          </div>
          <Badge className="bg-white text-teal-800 shadow-none">
            {visibleCards.length}/5
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        {visibleCards.length === 0 ? (
          <div className="px-5 py-8 text-center">
            <ShieldCheck className="mx-auto mb-3 size-7 text-teal-600" />
            <p className="font-medium text-slate-800">
              No promoted highlights yet
            </p>
            <p className="mt-1 text-sm leading-6 text-slate-500">
              Accepted or pinned source-linked facts will appear here.
            </p>
          </div>
        ) : (
          <ol
            aria-label="Top care highlights"
            className="divide-y divide-slate-100"
          >
            {visibleCards.map((card, index) => {
              const isBusy = busyHighlightId === card.highlight_id
              return (
                <li className="space-y-3 p-4" key={card.highlight_id}>
                  <div className="flex items-start gap-3">
                    <span className="grid size-7 shrink-0 place-items-center rounded-full bg-slate-100 text-xs font-bold text-slate-600">
                      {index + 1}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="font-medium leading-6 text-slate-900">
                        {card.label}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <Badge
                          className={
                            card.critical
                              ? "bg-red-100 text-red-800 hover:bg-red-100"
                              : "bg-slate-100 text-slate-700 hover:bg-slate-100"
                          }
                        >
                          {card.critical && (
                            <AlertTriangle
                              aria-hidden="true"
                              className="mr-1 size-3"
                            />
                          )}
                          {reasonLabel[card.risk_reason] ?? card.risk_reason}
                        </Badge>
                        <Badge
                          className="bg-teal-50 text-teal-800 hover:bg-teal-50"
                          variant="secondary"
                        >
                          {card.pinned ? "Pinned" : "Accepted"}
                        </Badge>
                      </div>
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2 pl-10">
                    <Button
                      className="min-h-11"
                      onClick={() => onSource(card)}
                      size="sm"
                      variant="outline"
                    >
                      <Link2 aria-hidden="true" /> View source
                    </Button>
                    {canReview && onAction && (
                      <>
                        <Button
                          aria-label={`Accept ${card.label}`}
                          className="min-h-11"
                          disabled={isBusy}
                          onClick={() => onAction(card, "accept")}
                          size="icon"
                          variant="ghost"
                        >
                          <Check aria-hidden="true" />
                        </Button>
                        <Button
                          aria-label={`Reject ${card.label}`}
                          className="min-h-11"
                          disabled={isBusy}
                          onClick={() => onAction(card, "reject")}
                          size="icon"
                          variant="ghost"
                        >
                          <X aria-hidden="true" />
                        </Button>
                        <Button
                          aria-label={`Pin ${card.label}`}
                          className="min-h-11"
                          disabled={isBusy}
                          onClick={() => onAction(card, "pin")}
                          size="icon"
                          variant="ghost"
                        >
                          <Pin aria-hidden="true" />
                        </Button>
                      </>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        )}
      </CardContent>
    </Card>
  )
}
