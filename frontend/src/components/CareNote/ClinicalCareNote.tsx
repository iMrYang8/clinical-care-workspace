import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Activity,
  CalendarDays,
  FileSearch,
  Mic,
  RefreshCw,
  ShieldCheck,
  UserRound,
} from "lucide-react"
import { useEffect, useState } from "react"

import type { ClinicalGlanceCard, MePublic, ProvenanceResolved } from "@/client"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import {
  apiErrorMessage,
  type ClinicalTimelineEntry,
  clinicalApi,
} from "@/features/api"
import { useDomainEvents } from "@/hooks/useDomainEvents"
import { CommentsRail } from "./CommentsRail"
import { EntryComposer } from "./EntryComposer"
import { GlanceTopCard } from "./GlanceTopCard"
import { type SourceFocus, TimelineEntryCard } from "./TimelineEntryCard"
import { VersionHistoryDrawer } from "./VersionHistoryDrawer"

type ClinicalCareNoteProps = {
  patientId: string
  currentUser: MePublic
}

type EvidenceView = {
  entryTitle: string
  entryContent: string
  isHistorical: boolean
  provenance: ProvenanceResolved
}

function highlightedEvidence(evidence: EvidenceView) {
  const points = Array.from(evidence.entryContent)
  const {
    start_offset: start,
    end_offset: end,
    exact_quote: quote,
  } = evidence.provenance
  if (points.slice(start, end).join("") !== quote) return evidence.entryContent
  return (
    <>
      {points.slice(0, start).join("")}
      <mark
        className="rounded bg-amber-200 px-0.5 text-slate-950"
        data-source-span
      >
        {points.slice(start, end).join("")}
      </mark>
      {points.slice(end).join("")}
    </>
  )
}

export function ClinicalCareNote({
  patientId,
  currentUser,
}: ClinicalCareNoteProps) {
  const canCollaborate =
    currentUser.role === "staff" || currentUser.role === "clinician"
  const readOnlyOversight = currentUser.role === "admin"
  const queryClient = useQueryClient()
  useDomainEvents(canCollaborate, currentUser.clinic_id)
  const [selectedEntry, setSelectedEntry] =
    useState<ClinicalTimelineEntry | null>(null)
  const [versionEntry, setVersionEntry] =
    useState<ClinicalTimelineEntry | null>(null)
  const [sourceFocus, setSourceFocus] = useState<SourceFocus | null>(null)
  const [evidence, setEvidence] = useState<EvidenceView | null>(null)
  const [liveMessage, setLiveMessage] = useState("")

  const patientsQuery = useQuery({
    queryKey: ["patients"],
    queryFn: clinicalApi.patients,
  })
  const timelineQuery = useQuery({
    queryKey: ["patients", patientId, "clinical-timeline"],
    queryFn: () => clinicalApi.timeline(patientId),
  })
  const glanceQuery = useQuery({
    queryKey: ["patients", patientId, "glance"],
    queryFn: () => clinicalApi.glance(patientId),
  })
  const patient = patientsQuery.data?.find(
    (candidate) => candidate.id === patientId,
  )
  const timeline = timelineQuery.data ?? []

  useEffect(() => {
    if (!selectedEntry && timeline[0]) setSelectedEntry(timeline[0])
    if (selectedEntry) {
      const refreshed = timeline.find((entry) => entry.id === selectedEntry.id)
      if (refreshed && refreshed.version_id !== selectedEntry.version_id) {
        setSelectedEntry(refreshed)
      }
    }
  }, [selectedEntry, timeline])

  const refreshPatient = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "clinical-timeline"],
      }),
      queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "glance"],
      }),
    ])
  }

  const highlightMutation = useMutation({
    mutationFn: ({
      card,
      action,
    }: {
      card: ClinicalGlanceCard
      action: "accept" | "reject" | "pin"
    }) => {
      if (action === "accept")
        return clinicalApi.acceptHighlight(card.highlight_id)
      if (action === "reject")
        return clinicalApi.rejectHighlight(card.highlight_id)
      return clinicalApi.pinHighlight(card.highlight_id)
    },
    onSuccess: async (_, variables) => {
      setLiveMessage(
        `${variables.action} completed for ${variables.card.label}`,
      )
      await queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "glance"],
      })
    },
  })

  const showSource = async (card: ClinicalGlanceCard) => {
    setLiveMessage("Resolving immutable source…")
    try {
      const provenance = await clinicalApi.resolveProvenance(
        card.provenance_pointer_id,
      )
      let matchedEntry = timeline.find(
        (entry) => entry.version_id === provenance.entry_version_id,
      )
      let entryContent = matchedEntry?.content ?? ""
      let entryTitle = matchedEntry?.title ?? "Historical source"

      if (!matchedEntry) {
        const histories = await Promise.all(
          timeline.map(async (entry) => ({
            entry,
            versions: await clinicalApi.versions(entry.id),
          })),
        )
        const historical = histories
          .flatMap(({ entry, versions }) =>
            versions.map((version) => ({ entry, version })),
          )
          .find(({ version }) => version.id === provenance.entry_version_id)
        if (historical) {
          matchedEntry = historical.entry
          entryContent = historical.version.content
          entryTitle = historical.version.title
        }
      }
      if (!matchedEntry)
        throw new Error("Immutable source entry is not in this timeline")

      setSourceFocus({
        entryId: matchedEntry.id,
        entryVersionId: provenance.entry_version_id,
        startOffset: provenance.start_offset,
        endOffset: provenance.end_offset,
      })
      setEvidence({
        entryTitle,
        entryContent,
        provenance,
        isHistorical: matchedEntry.version_id !== provenance.entry_version_id,
      })
      setLiveMessage(
        `Source focused: ${entryTitle}, exact quote ${provenance.exact_quote}`,
      )
    } catch (caught) {
      setLiveMessage(`Source could not be resolved: ${apiErrorMessage(caught)}`)
    }
  }

  if (patientsQuery.isLoading || timelineQuery.isLoading) {
    return (
      <div aria-label="Loading care note" className="space-y-6" role="status">
        <Skeleton className="h-28 rounded-2xl" />
        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(19rem,1fr)]">
          <Skeleton className="h-96 rounded-2xl" />
          <Skeleton className="h-80 rounded-2xl" />
        </div>
      </div>
    )
  }

  if (patientsQuery.isError || timelineQuery.isError || !patient) {
    return (
      <Alert className="border-red-200 bg-red-50 text-red-900">
        <AlertTitle>Care note did not load</AlertTitle>
        <AlertDescription>
          {apiErrorMessage(
            patientsQuery.error ??
              timelineQuery.error ??
              new Error("Patient not found"),
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

      <header className="overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
        <div className="flex flex-col justify-between gap-5 p-5 sm:flex-row sm:items-center sm:p-6">
          <div className="flex min-w-0 items-center gap-4">
            <span className="grid size-14 shrink-0 place-items-center rounded-2xl bg-teal-100 text-teal-800">
              <UserRound className="size-6" />
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate font-serif text-3xl font-semibold tracking-tight text-slate-950">
                  {patient.display_name}
                </h1>
                <Badge className="bg-amber-100 text-amber-800">
                  Synthetic data
                </Badge>
              </div>
              <p className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-slate-500">
                <span className="flex items-center gap-1">
                  <ShieldCheck className="size-4" /> Clinic scoped
                </span>
                <span className="flex items-center gap-1">
                  <CalendarDays className="size-4" /> Longitudinal care note
                </span>
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge className="bg-teal-700 text-white">
              {currentUser.role}
              {readOnlyOversight ? " · read-only oversight" : ""}
            </Badge>
            {canCollaborate && (
              <Button asChild className="min-h-11 bg-teal-700">
                <a href={`/patients/${patientId}/voice/capture`}>
                  <Mic /> Record visit
                </a>
              </Button>
            )}
            <Button onClick={refreshPatient} variant="outline">
              <RefreshCw /> Refresh
            </Button>
          </div>
        </div>
      </header>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(19rem,1fr)]">
        <section
          aria-labelledby="timeline-heading"
          className="min-w-0 space-y-4"
        >
          <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
            <div>
              <p className="text-xs font-bold uppercase tracking-[0.18em] text-slate-500">
                Full history
              </p>
              <h2
                className="font-serif text-3xl font-semibold text-slate-950"
                id="timeline-heading"
              >
                Timeline
              </h2>
            </div>
            {canCollaborate && (
              <EntryComposer
                currentUser={currentUser}
                onCreated={refreshPatient}
                patientId={patientId}
              />
            )}
          </div>

          {timeline.length === 0 ? (
            <div className="rounded-2xl border border-dashed bg-white px-6 py-14 text-center">
              <Activity className="mx-auto mb-3 size-8 text-teal-600" />
              <h3 className="font-serif text-xl font-semibold text-slate-900">
                The timeline is ready for its first entry
              </h3>
              <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-slate-500">
                This is the real empty synthetic record. Add a{" "}
                {currentUser.role}
                section entry to begin the versioned care history.
              </p>
            </div>
          ) : (
            <ol className="space-y-4">
              {timeline.map((entry) => (
                <li key={entry.id}>
                  <TimelineEntryCard
                    currentUser={currentUser}
                    entry={entry}
                    onCreateComment={async (entryId, body) => {
                      const comment = await clinicalApi.createComment(
                        entryId,
                        body,
                      )
                      await queryClient.invalidateQueries({
                        queryKey: ["entries", entryId, "comments"],
                      })
                      setSelectedEntry(entry)
                      return comment
                    }}
                    onOpenComments={setSelectedEntry}
                    onOpenVersions={setVersionEntry}
                    onSave={async (target, draft) => {
                      await clinicalApi.patchEntry(
                        target.id,
                        target.version_id,
                        draft,
                      )
                      await refreshPatient()
                    }}
                    sourceFocus={sourceFocus}
                  />
                </li>
              ))}
            </ol>
          )}
        </section>

        <aside className="space-y-5 lg:sticky lg:top-24">
          <GlanceTopCard
            busyHighlightId={
              highlightMutation.isPending
                ? (highlightMutation.variables?.card.highlight_id ?? null)
                : null
            }
            canReview={canCollaborate}
            cards={glanceQuery.data?.cards ?? []}
            onAction={(card, action) =>
              highlightMutation.mutate({ card, action })
            }
            onSource={showSource}
          />
          {glanceQuery.isError && (
            <Alert className="border-amber-200 bg-amber-50 text-amber-950">
              <AlertDescription>
                Glance is not ready: {apiErrorMessage(glanceQuery.error)}
              </AlertDescription>
            </Alert>
          )}
          {highlightMutation.isError && (
            <Alert className="border-red-200 bg-red-50 text-red-900">
              <AlertDescription>
                {apiErrorMessage(highlightMutation.error)}
              </AlertDescription>
            </Alert>
          )}
          <CommentsRail
            currentUser={currentUser}
            entryId={selectedEntry?.id ?? null}
            entryVersionId={selectedEntry?.version_id ?? null}
            readOnly={readOnlyOversight}
          />
        </aside>
      </div>

      {versionEntry && (
        <VersionHistoryDrawer
          canRevert={
            versionEntry.origin === "human" &&
            versionEntry.section === currentUser.role
          }
          currentVersionId={versionEntry.version_id}
          entryId={versionEntry.id}
          onOpenChange={(open) => !open && setVersionEntry(null)}
          onReverted={async () => {
            await refreshPatient()
            setVersionEntry(null)
          }}
          open
        />
      )}

      <Dialog
        onOpenChange={(open) => !open && setEvidence(null)}
        open={Boolean(evidence)}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 font-serif text-2xl">
              <FileSearch className="text-teal-700" /> Immutable source
            </DialogTitle>
            <DialogDescription>
              {evidence?.isHistorical
                ? "This highlight points to a historical version; the current entry was focused and the original snapshot is shown below."
                : "The exact span is highlighted in the focused timeline entry."}
            </DialogDescription>
          </DialogHeader>
          {evidence && (
            <div className="space-y-4">
              <div className="rounded-xl border bg-slate-50 p-4">
                <p className="font-semibold">{evidence.entryTitle}</p>
                <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-700">
                  {highlightedEvidence(evidence)}
                </p>
              </div>
              <blockquote className="rounded-xl border-l-4 border-amber-400 bg-amber-50 p-4 text-amber-950">
                “{evidence.provenance.exact_quote}”
              </blockquote>
              <dl className="grid gap-2 text-xs text-slate-500 sm:grid-cols-2">
                <div>
                  <dt>Immutable version</dt>
                  <dd className="font-mono text-slate-700">
                    {evidence.provenance.entry_version_id}
                  </dd>
                </div>
                <div>
                  <dt>Quote SHA-256</dt>
                  <dd className="break-all font-mono text-slate-700">
                    {evidence.provenance.quote_sha256}
                  </dd>
                </div>
              </dl>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
