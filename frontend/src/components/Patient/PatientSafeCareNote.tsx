import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  HeartHandshake,
  History,
  Link2,
  LoaderCircle,
  Mic,
  Plus,
  ShieldCheck,
  UserRound,
} from "lucide-react"
import { useEffect, useRef, useState } from "react"

import type {
  PatientPublic,
  PatientTimelineEntry,
  ProvenanceResolved,
} from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import {
  apiErrorMessage,
  type PatientPortalEvent,
  type PatientPublicationAcknowledgement,
  type PatientPublicationReceipt,
  type PatientSafeGlance,
  patientSafeApi,
} from "@/features/api"
import { formatSingaporeDateTime } from "@/lib/dateTime"

export type PatientSafeApi = {
  patients: () => Promise<PatientPublic[]>
  timeline: (patientId: string) => Promise<PatientTimelineEntry[]>
  glance: (patientId: string) => Promise<PatientSafeGlance>
  publicationReceipts?: (
    patientId: string,
  ) => Promise<PatientPublicationReceipt[]>
  acknowledgePublication?: (
    publicationId: string,
  ) => Promise<PatientPublicationAcknowledgement>
  portalEvents?: (
    patientId: string,
    since?: string,
  ) => Promise<PatientPortalEvent[]>
  streamEvents?: typeof patientSafeApi.streamEvents
  resolveProvenance: (pointerId: string) => Promise<ProvenanceResolved>
  createInsight: (
    patientId: string,
    title: string,
    content: string,
  ) => Promise<PatientTimelineEntry>
}

type PatientSafeCareNoteProps = {
  api?: PatientSafeApi
}

export function PatientSafeCareNote({
  api = patientSafeApi,
}: PatientSafeCareNoteProps) {
  const queryClient = useQueryClient()
  const [patientId, setPatientId] = useState<string | null>(null)
  const [source, setSource] = useState<ProvenanceResolved | null>(null)
  const [title, setTitle] = useState("")
  const [content, setContent] = useState("")
  const [composerOpen, setComposerOpen] = useState(false)
  const [liveMessage, setLiveMessage] = useState("")
  const lastPortalEventId = useRef<string | undefined>(undefined)

  const patientsQuery = useQuery({
    queryKey: ["patient-safe", "patients"],
    queryFn: api.patients,
  })
  useEffect(() => {
    if (!patientId && patientsQuery.data?.[0]) {
      setPatientId(patientsQuery.data[0].id)
    }
  }, [patientId, patientsQuery.data])

  const timelineQuery = useQuery({
    queryKey: ["patient-safe", patientId, "timeline"],
    queryFn: () => api.timeline(patientId!),
    enabled: Boolean(patientId),
    refetchInterval: 15_000,
  })
  const glanceQuery = useQuery({
    queryKey: ["patient-safe", patientId, "glance"],
    queryFn: () => api.glance(patientId!),
    enabled: Boolean(patientId),
    refetchInterval: 15_000,
  })
  const publicationReceiptsQuery = useQuery({
    queryKey: ["patient-safe", patientId, "publication-receipts"],
    queryFn: () => api.publicationReceipts?.(patientId!) ?? Promise.resolve([]),
    enabled: Boolean(patientId && api.publicationReceipts),
    refetchInterval: 15_000,
  })
  const portalEventsQuery = useQuery({
    queryKey: ["patient-safe", patientId, "portal-events"],
    queryFn: () => api.portalEvents?.(patientId!) ?? Promise.resolve([]),
    enabled: Boolean(patientId && api.portalEvents),
    refetchInterval: 15_000,
  })
  const patient = patientsQuery.data?.find((item) => item.id === patientId)

  const insightMutation = useMutation({
    mutationFn: () =>
      api.createInsight(patientId!, title.trim(), content.trim()),
    onSuccess: async () => {
      setTitle("")
      setContent("")
      setComposerOpen(false)
      await queryClient.invalidateQueries({
        queryKey: ["patient-safe", patientId, "timeline"],
      })
      setLiveMessage("Your insight was added to the patient-facing timeline.")
    },
  })
  const acknowledgeMutation = useMutation({
    mutationFn: (publicationId: string) => {
      if (!api.acknowledgePublication)
        throw new Error("Acknowledgement is unavailable")
      return api.acknowledgePublication(publicationId)
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ["patient-safe", patientId, "publication-receipts"],
      })
      setLiveMessage("Sharing correction acknowledged.")
    },
  })

  useEffect(() => {
    const events = portalEventsQuery.data ?? []
    const newest = events[events.length - 1]
    if (!newest || newest.id === lastPortalEventId.current) return
    const isInitialLoad = lastPortalEventId.current === undefined
    lastPortalEventId.current = newest.id
    if (isInitialLoad) return
    void Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["patient-safe", patientId, "timeline"],
      }),
      queryClient.invalidateQueries({
        queryKey: ["patient-safe", patientId, "glance"],
      }),
      queryClient.invalidateQueries({
        queryKey: ["patient-safe", patientId, "publication-receipts"],
      }),
    ])
    setLiveMessage(
      "Your care team changed shared information. Polling refreshed this open view.",
    )
  }, [patientId, portalEventsQuery.data, queryClient])

  useEffect(() => {
    if (!patientId || !api.streamEvents) return
    const controller = new AbortController()
    void api
      .streamEvents(
        patientId,
        (event) => {
          if (
            !event.event.startsWith("patient_publication.") &&
            !event.event.startsWith("publication.") &&
            !event.event.startsWith("notification.")
          )
            return
          void Promise.all([
            queryClient.invalidateQueries({
              queryKey: ["patient-safe", patientId, "timeline"],
            }),
            queryClient.invalidateQueries({
              queryKey: ["patient-safe", patientId, "glance"],
            }),
            queryClient.invalidateQueries({
              queryKey: ["patient-safe", patientId, "publication-receipts"],
            }),
          ])
          setLiveMessage(
            "Your care team changed shared information. The open view has been refreshed.",
          )
        },
        { signal: controller.signal },
      )
      .catch(() => {
        // Fifteen-second query polling remains the explicit fallback when SSE
        // is interrupted or unsupported by an intermediary.
      })
    return () => controller.abort()
  }, [api, patientId, queryClient])

  const showSource = async (pointerId: string) => {
    try {
      const resolved = await api.resolveProvenance(pointerId)
      setSource(resolved)
      const target = document.querySelector<HTMLElement>(
        `[data-patient-version-id="${resolved.entry_version_id}"]`,
      )
      target?.scrollIntoView({ behavior: "smooth", block: "center" })
      target?.focus({ preventScroll: true })
      setLiveMessage(`Source focused: ${resolved.exact_quote}`)
    } catch (caught) {
      setLiveMessage(`Source unavailable: ${apiErrorMessage(caught)}`)
    }
  }

  if (patientsQuery.isLoading) {
    return (
      <div className="space-y-5">
        <Skeleton className="h-28 rounded-2xl" />
        <Skeleton className="h-80 rounded-2xl" />
      </div>
    )
  }

  if (patientsQuery.isError || !patient) {
    return (
      <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
        <AlertTitle>My Care did not load</AlertTitle>
        <AlertDescription>
          {apiErrorMessage(
            patientsQuery.error ?? new Error("No linked patient record"),
          )}
        </AlertDescription>
      </Alert>
    )
  }

  return (
    <div className="space-y-6">
      <div aria-atomic="true" aria-live="polite" className="sr-only">
        {liveMessage}
      </div>
      <header className="rounded-2xl border border-primary/30 bg-gradient-to-br from-primary/10 to-card p-6 shadow-sm">
        <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-center">
          <div className="flex items-center gap-4">
            <span className="grid size-14 place-items-center rounded-2xl bg-primary/10 text-primary">
              <UserRound />
            </span>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="font-serif text-3xl font-semibold text-foreground">
                  My Care · {patient.display_name}
                </h1>
              </div>
              <p className="mt-1 text-sm text-muted-foreground">
                Review information shared by your care team and add an update of
                your own.
              </p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button asChild className="min-h-11">
              <a href="/patient/my-care/voice/capture">
                <Mic /> Add a recording
              </a>
            </Button>
            <Badge className="w-fit bg-success-muted text-success-muted-foreground">
              <ShieldCheck /> Shared with you
            </Badge>
          </div>
        </div>
      </header>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(19rem,1fr)]">
        <section aria-labelledby="my-timeline-heading" className="space-y-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
                Shared with me
              </p>
              <h2
                className="font-serif text-3xl font-semibold"
                id="my-timeline-heading"
              >
                My timeline
              </h2>
            </div>
            <Button onClick={() => setComposerOpen(true)}>
              <Plus /> Add my insight
            </Button>
          </div>

          <Dialog
            open={composerOpen}
            onOpenChange={(open) =>
              !insightMutation.isPending && setComposerOpen(open)
            }
          >
            <DialogContent className="sm:max-w-lg">
              <DialogHeader>
                <DialogTitle className="font-serif text-2xl">
                  Add my insight
                </DialogTitle>
                <DialogDescription>
                  Share an update with your care team.
                </DialogDescription>
              </DialogHeader>
              <form
                className="space-y-4"
                onSubmit={(event) => {
                  event.preventDefault()
                  insightMutation.mutate()
                }}
              >
                <div className="grid gap-2">
                  <Label htmlFor="patient-insight-title">Title</Label>
                  <Input
                    autoFocus
                    id="patient-insight-title"
                    onChange={(event) => setTitle(event.target.value)}
                    placeholder="What would you like the care team to know?"
                    value={title}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="patient-insight-content">My insight</Label>
                  <textarea
                    className="min-h-32 rounded-xl border bg-background p-3 text-sm leading-6 text-foreground outline-none focus:ring-2 focus:ring-primary"
                    id="patient-insight-content"
                    onChange={(event) => setContent(event.target.value)}
                    value={content}
                  />
                </div>
                <DialogFooter>
                  <Button
                    disabled={insightMutation.isPending}
                    onClick={() => setComposerOpen(false)}
                    type="button"
                    variant="outline"
                  >
                    Cancel
                  </Button>
                  <Button
                    disabled={
                      !title.trim() ||
                      !content.trim() ||
                      insightMutation.isPending
                    }
                    type="submit"
                  >
                    {insightMutation.isPending ? (
                      <LoaderCircle className="animate-spin" />
                    ) : (
                      <Plus />
                    )}
                    Add to My Care
                  </Button>
                </DialogFooter>
              </form>
            </DialogContent>
          </Dialog>

          {timelineQuery.isLoading && (
            <LoaderCircle className="animate-spin text-primary" />
          )}
          {timelineQuery.data?.length === 0 && (
            <div className="rounded-2xl border border-dashed bg-card px-6 py-12 text-center">
              <HeartHandshake className="mx-auto mb-3 size-8 text-primary" />
              <p className="font-medium text-foreground">
                No published entries yet
              </p>
              <p className="mt-1 text-sm text-muted-foreground">
                You can still add your own insight above.
              </p>
            </div>
          )}
          <ol className="space-y-4">
            {timelineQuery.data?.map((entry) => (
              <li key={entry.id}>
                <article
                  className="scroll-mt-24 rounded-2xl border bg-card p-5 outline-none focus-visible:ring-2 focus-visible:ring-primary"
                  data-patient-version-id={entry.version_id}
                  tabIndex={-1}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge className="bg-primary/10 text-primary">
                      Shared care note
                    </Badge>
                    <Badge variant="outline">v{entry.version_no}</Badge>
                  </div>
                  <h3 className="mt-3 font-serif text-xl font-semibold">
                    {entry.title}
                  </h3>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-foreground/90">
                    {entry.content}
                  </p>
                  <time
                    className="mt-3 block text-xs text-muted-foreground"
                    dateTime={entry.created_at}
                  >
                    {formatSingaporeDateTime(entry.created_at)}
                  </time>
                  {entry.approval_receipt && (
                    <div className="mt-4 rounded-xl border border-success/30 bg-success-muted/30 p-3 text-sm leading-6">
                      <p className="font-semibold text-success-muted-foreground">
                        Reviewed for sharing
                      </p>
                      <p className="text-muted-foreground">
                        Approved by{" "}
                        {String(
                          entry.approval_receipt.approved_by ??
                            "your clinician",
                        )}
                        {entry.approval_receipt.approved_at
                          ? ` on ${formatSingaporeDateTime(String(entry.approval_receipt.approved_at))}`
                          : ""}
                        .
                      </p>
                      <p className="text-muted-foreground">
                        Source:{" "}
                        {String(
                          entry.approval_receipt.source_title ?? entry.title,
                        )}{" "}
                        · {String(entry.approval_receipt.status ?? "active")}
                      </p>
                    </div>
                  )}
                </article>
              </li>
            ))}
          </ol>
        </section>

        <aside className="space-y-4 lg:sticky lg:top-24">
          <Card className="gap-0 overflow-hidden border-primary/30 bg-gradient-to-b from-primary/10 via-card to-card py-0 shadow-sm">
            <CardHeader className="px-6 py-6">
              <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
                Care overview
              </p>
              <CardTitle className="font-serif text-2xl">
                Current priorities
              </CardTitle>
            </CardHeader>
            <CardContent className="p-0">
              {(glanceQuery.data?.cards.length ?? 0) === 0 ? (
                <div className="px-5 py-8 text-center">
                  <ShieldCheck className="mx-auto mb-3 size-7 text-primary" />
                  <p className="font-medium text-foreground">
                    No shared highlights yet
                  </p>
                  <p className="mt-1 text-sm leading-6 text-muted-foreground">
                    Care-team approved, source-linked facts will appear here.
                  </p>
                </div>
              ) : (
                <ol
                  aria-label="Patient-facing care highlights"
                  className="divide-y divide-border"
                >
                  {glanceQuery.data?.cards.slice(0, 5).map((card, index) => (
                    <li className="space-y-3 p-4" key={card.highlight_id}>
                      <div className="flex items-start gap-3">
                        <span className="grid size-7 shrink-0 place-items-center rounded-full bg-primary/10 text-xs font-bold text-primary">
                          {index + 1}
                        </span>
                        <p className="min-w-0 flex-1 font-medium leading-6 text-foreground">
                          {card.label}
                        </p>
                      </div>
                      <Button
                        className="ml-10 min-h-11"
                        onClick={() => showSource(card.provenance_pointer_id)}
                        size="sm"
                        variant="outline"
                      >
                        <Link2 aria-hidden="true" /> View approved source
                      </Button>
                    </li>
                  ))}
                </ol>
              )}
            </CardContent>
          </Card>
          {(publicationReceiptsQuery.data ?? []).some(
            (receipt) =>
              receipt.status !== "active" ||
              receipt.acknowledgement_state === "pending",
          ) && (
            <Card>
              <CardHeader className="pb-3">
                <CardTitle className="flex items-center gap-2 font-serif text-xl">
                  <History className="size-5 text-primary" /> Sharing updates
                </CardTitle>
                <p className="text-sm leading-6 text-muted-foreground">
                  Withdrawn or corrected updates are removed from your open
                  timeline immediately. Their immutable receipt and any linked
                  replacement remain here for acknowledgement.
                </p>
              </CardHeader>
              <CardContent className="space-y-3">
                {publicationReceiptsQuery.data
                  ?.filter(
                    (receipt) =>
                      receipt.status !== "active" ||
                      receipt.acknowledgement_state === "pending",
                  )
                  .map((receipt) => (
                    <div
                      className="rounded-xl border bg-muted/30 p-3"
                      key={`${receipt.entry_title}-${receipt.approved_at}`}
                    >
                      <div className="flex items-start justify-between gap-2">
                        <p className="font-medium">{receipt.entry_title}</p>
                        <Badge variant="outline">
                          {receipt.replacement_publication_id
                            ? "Corrected"
                            : "Withdrawn"}
                        </Badge>
                      </div>
                      <p className="mt-1 text-xs leading-5 text-muted-foreground">
                        Previously approved by {receipt.approved_by_name} ·
                        withdrawn{" "}
                        {receipt.withdrawn_at
                          ? formatSingaporeDateTime(receipt.withdrawn_at)
                          : "recently"}
                      </p>
                      {receipt.replacement_entry_title && (
                        <p className="mt-1 text-xs leading-5 text-muted-foreground">
                          Replacement: {receipt.replacement_entry_title}
                        </p>
                      )}
                      {receipt.outreach_required === true && (
                        <p className="mt-2 text-xs font-medium text-warning-muted-foreground">
                          Your care team marked this correction for direct
                          outreach.
                        </p>
                      )}
                      {receipt.acknowledgement_state === "pending" &&
                        api.acknowledgePublication &&
                        receipt.publication_id && (
                          <Button
                            className="mt-2"
                            disabled={acknowledgeMutation.isPending}
                            onClick={() =>
                              acknowledgeMutation.mutate(receipt.publication_id)
                            }
                            size="sm"
                          >
                            {acknowledgeMutation.isPending && (
                              <LoaderCircle className="animate-spin" />
                            )}
                            Acknowledge correction
                          </Button>
                        )}
                    </div>
                  ))}
              </CardContent>
            </Card>
          )}
          {publicationReceiptsQuery.isError && (
            <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
              <History className="size-4" />
              <AlertTitle>Sharing history unavailable</AlertTitle>
              <AlertDescription>
                Approval and withdrawal history did not load. Your shared care
                information remains available; try again shortly.
              </AlertDescription>
            </Alert>
          )}
          {source && (
            <div className="rounded-2xl border border-warning/40 bg-warning-muted p-4">
              <p className="flex items-center gap-2 font-semibold text-warning-muted-foreground">
                <Link2 /> Source details
              </p>
              <blockquote className="mt-2 text-sm leading-6 text-warning-muted-foreground">
                “{source.exact_quote}”
              </blockquote>
            </div>
          )}
          {(timelineQuery.isError ||
            glanceQuery.isError ||
            insightMutation.isError) && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>
                {apiErrorMessage(
                  timelineQuery.error ??
                    glanceQuery.error ??
                    insightMutation.error,
                )}
              </AlertDescription>
            </Alert>
          )}
        </aside>
      </div>
    </div>
  )
}
