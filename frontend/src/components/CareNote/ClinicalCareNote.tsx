import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "@tanstack/react-router"
import {
  Activity,
  AlertTriangle,
  ArchiveRestore,
  ArrowLeft,
  CalendarDays,
  Database,
  FileSearch,
  Mic,
  RefreshCw,
  Send,
  ShieldCheck,
  Sparkles,
} from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  type ClinicalGlanceCard,
  DecayService,
  type MePublic,
  type ProvenanceResolved,
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
  type ClinicalTimelineEntry,
  clinicalApi,
  type DismissReason,
} from "@/features/api"
import { patientRouteReferenceFromId } from "@/features/routeReferences"
import { useDomainEvents } from "@/hooks/useDomainEvents"
import { formatSingaporeDate, formatSingaporeDateTime } from "@/lib/dateTime"
import { CommentsRail } from "./CommentsRail"
import { EntryComposer } from "./EntryComposer"
import { GlanceTopCard } from "./GlanceTopCard"
import { PatientSharingPanel } from "./PatientSharingPanel"
import { type SourceFocus, TimelineEntryCard } from "./TimelineEntryCard"
import { VersionHistoryDrawer } from "./VersionHistoryDrawer"

type ClinicalCareNoteProps = {
  patientId: string
  currentUser: MePublic
}

type EvidenceView = {
  authorId: string | null
  entryTitle: string
  entryContent: string
  entryOrigin: string
  entrySection: string
  isHistorical: boolean
  provenance: ProvenanceResolved
  sourceDate: string
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
        className="rounded bg-warning-muted px-0.5 text-warning-muted-foreground"
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
  const [portalEmail, setPortalEmail] = useState("")
  const [portalInviteOpen, setPortalInviteOpen] = useState(false)
  const [conflictResolution, setConflictResolution] = useState("")
  const [correctionEntryId, setCorrectionEntryId] = useState("")
  const [resolvingConflictId, setResolvingConflictId] = useState<string | null>(
    null,
  )

  const timelineQuery = useQuery({
    queryKey: ["patients", patientId, "clinical-timeline"],
    queryFn: () => clinicalApi.timeline(patientId),
  })
  const glanceQuery = useQuery({
    queryKey: ["patients", patientId, "glance"],
    queryFn: () => clinicalApi.glance(patientId),
  })
  const patientDetailQuery = useQuery({
    queryKey: ["patients", patientId, "detail"],
    queryFn: () => clinicalApi.patientDetail(patientId),
  })
  const conflictsQuery = useQuery({
    queryKey: ["patients", patientId, "conflicts"],
    queryFn: () => clinicalApi.patientConflicts(patientId),
  })
  const clinicalFactsQuery = useQuery({
    queryKey: ["patients", patientId, "clinical-facts"],
    queryFn: () => clinicalApi.patientClinicalFacts(patientId),
  })
  const retentionQuery = useQuery({
    queryKey: ["decay", "preview"],
    queryFn: async () => (await DecayService.preview()).data,
    enabled: currentUser.role === "clinician" || currentUser.role === "admin",
  })
  const resolveConflictMutation = useMutation({
    mutationFn: (conflictId: string) =>
      clinicalApi.resolveConflict(
        conflictId,
        correctionEntryId,
        conflictResolution,
      ),
    onSuccess: async () => {
      setConflictResolution("")
      setCorrectionEntryId("")
      setResolvingConflictId(null)
      await queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "conflicts"],
      })
      await queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "glance"],
      })
      await queryClient.invalidateQueries({ queryKey: ["decay", "preview"] })
    },
  })
  const inviteMutation = useMutation({
    mutationFn: () => clinicalApi.invitePatient(patientId, portalEmail),
    onSuccess: async () => {
      setPortalEmail("")
      setPortalInviteOpen(false)
      await queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "detail"],
      })
    },
  })
  const teamQuery = useQuery({
    queryKey: ["team", "members"],
    queryFn: clinicalApi.teamMembers,
    staleTime: 5 * 60 * 1000,
  })
  const patient = patientDetailQuery.data
  const timeline = timelineQuery.data ?? []
  const timelineGroups = useMemo(() => {
    const grouped = new Map<string, ClinicalTimelineEntry[]>()
    for (const entry of timeline) {
      const year = new Date(entry.occurred_at).getFullYear().toString()
      grouped.set(year, [...(grouped.get(year) ?? []), entry])
    }
    return [...grouped.entries()]
  }, [timeline])
  const aiEntryCount = timeline.filter((entry) => entry.origin === "ai").length
  const patientEntryIds = useMemo(
    () => new Set(timeline.map((entry) => entry.id)),
    [timeline],
  )
  const patientRetention =
    retentionQuery.data?.candidates.filter((candidate) =>
      patientEntryIds.has(candidate.entry_id),
    ) ?? []
  const recordStart = timeline[timeline.length - 1]?.occurred_at
  const recordEnd = timeline[0]?.occurred_at
  const recordYearSpan =
    recordStart && recordEnd
      ? new Date(recordEnd).getFullYear() - new Date(recordStart).getFullYear()
      : 0
  const patientInitials = patient?.display_name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase())
    .join("")
  const dateOfBirth = patientDetailQuery.data?.date_of_birth
  const patientAge = dateOfBirth
    ? Math.max(
        0,
        new Date().getFullYear() -
          new Date(`${dateOfBirth}T00:00:00`).getFullYear() -
          (new Date() <
          new Date(
            new Date().getFullYear(),
            new Date(`${dateOfBirth}T00:00:00`).getMonth(),
            new Date(`${dateOfBirth}T00:00:00`).getDate(),
          )
            ? 1
            : 0),
      )
    : null

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
      queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "clinical-facts"],
      }),
      queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "conflicts"],
      }),
      queryClient.invalidateQueries({ queryKey: ["decay", "preview"] }),
    ])
  }

  const highlightMutation = useMutation({
    mutationFn: ({
      card,
      action,
      reason,
    }: {
      card: ClinicalGlanceCard
      action: "accept" | "pin" | "dismiss" | "request_review"
      reason?: DismissReason
    }) => {
      if (action === "accept")
        return clinicalApi.acceptHighlight(card.highlight_id)
      if (action === "pin") return clinicalApi.pinHighlight(card.highlight_id)
      if (action === "dismiss")
        return clinicalApi.dismissHighlight(
          card.highlight_id,
          reason ?? "not_relevant",
        )
      return clinicalApi.requestHighlightReview(
        card.highlight_id,
        "Care team requested clinical review from Current priorities.",
      )
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

  const showPointer = async (pointerId: string) => {
    setLiveMessage("Opening source details…")
    try {
      const provenance = await clinicalApi.resolveProvenance(pointerId)
      let matchedEntry = timeline.find(
        (entry) => entry.version_id === provenance.entry_version_id,
      )
      let entryContent = matchedEntry?.content ?? ""
      let entryTitle = matchedEntry?.title ?? "Historical source"
      let authorId = matchedEntry?.author_id ?? null
      let sourceDate =
        matchedEntry?.created_at ?? matchedEntry?.occurred_at ?? ""

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
          authorId = historical.version.author_id
          sourceDate = historical.version.created_at
        }
      }
      if (!matchedEntry)
        throw new Error(
          "The source note is not available in this care timeline",
        )

      setSourceFocus({
        entryId: matchedEntry.id,
        entryVersionId: provenance.entry_version_id,
        startOffset: provenance.start_offset,
        endOffset: provenance.end_offset,
      })
      setEvidence({
        entryTitle,
        entryContent,
        authorId,
        entryOrigin: matchedEntry.origin,
        entrySection: matchedEntry.section,
        sourceDate,
        provenance,
        isHistorical: matchedEntry.version_id !== provenance.entry_version_id,
      })
      setLiveMessage(`Source opened: ${entryTitle}`)
    } catch (caught) {
      setLiveMessage(`Source could not be resolved: ${apiErrorMessage(caught)}`)
    }
  }
  const showSource = (card: ClinicalGlanceCard) =>
    showPointer(card.provenance_pointer_id)

  if (patientDetailQuery.isLoading || timelineQuery.isLoading) {
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

  if (patientDetailQuery.isError || timelineQuery.isError || !patient) {
    return (
      <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
        <AlertTitle>Care note did not load</AlertTitle>
        <AlertDescription>
          {apiErrorMessage(
            patientDetailQuery.error ??
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

      <Button asChild className="-mb-2 w-fit" size="sm" variant="ghost">
        <Link to="/patients">
          <ArrowLeft className="size-4" /> Back to patients
        </Link>
      </Button>

      <header
        className="scroll-mt-24 overflow-hidden rounded-2xl border border-border bg-card shadow-sm"
        id="patient-overview"
      >
        <div className="flex flex-col justify-between gap-5 p-5 sm:flex-row sm:items-center sm:p-6">
          <div className="flex min-w-0 items-center gap-4">
            <span
              aria-hidden="true"
              className="grid size-14 shrink-0 place-items-center rounded-2xl bg-primary/10 font-serif text-xl font-bold text-primary"
            >
              {patientInitials || "—"}
            </span>
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="truncate font-serif text-3xl font-semibold tracking-tight text-foreground">
                  {patient.display_name}
                </h1>
              </div>
              <p className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-sm text-muted-foreground">
                <span className="flex items-center gap-1">
                  <CalendarDays className="size-4" />
                  {dateOfBirth
                    ? `Age ${patientAge} · DOB ${formatSingaporeDate(dateOfBirth)}`
                    : "Date of birth pending"}
                </span>
                <span className="flex items-center gap-1">
                  <ShieldCheck className="size-4" />
                  {patientDetailQuery.data?.medical_record_number ?? "pending"}
                </span>
              </p>
              <p className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>
                  Record since{" "}
                  {recordStart ? new Date(recordStart).getFullYear() : "—"}
                  {recordYearSpan > 0
                    ? ` · ${recordYearSpan}-year history`
                    : ""}
                </span>
                <span>{timeline.length} care entries</span>
                <span>{aiEntryCount} AI-assisted notes</span>
                <span>
                  {conflictsQuery.data?.filter(
                    (item) => item.status === "unresolved",
                  ).length ?? 0}{" "}
                  unresolved conflicts
                </span>
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge className="bg-primary text-primary-foreground">
              {currentUser.role === "staff"
                ? "Care staff"
                : currentUser.role === "clinician"
                  ? "Clinician"
                  : "Clinic administrator"}
              {readOnlyOversight ? " · read-only oversight" : ""}
            </Badge>
            {canCollaborate && (
              <Button asChild className="min-h-11">
                <a
                  href={`/patients/${patientRouteReferenceFromId(patientId)}/voice/capture`}
                >
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
          className="min-w-0 scroll-mt-24 space-y-4"
          id="timeline"
        >
          <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-end">
            <div>
              <h2
                className="font-serif text-3xl font-semibold text-foreground"
                id="timeline-heading"
              >
                Longitudinal timeline
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                {timelineGroups.length} calendar years · human and AI-assisted
                records · newest first
              </p>
              <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                <span>
                  <strong className="text-foreground">Care staff note:</strong>{" "}
                  observations, handovers, and follow-up
                </span>
                <span>
                  <strong className="text-foreground">Clinical note:</strong>{" "}
                  assessment, diagnosis, and treatment plan
                </span>
              </div>
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
            <div className="rounded-2xl border border-dashed bg-card px-6 py-14 text-center">
              <Activity className="mx-auto mb-3 size-8 text-primary" />
              <h3 className="font-serif text-xl font-semibold text-foreground">
                Add the first care note
              </h3>
              <p className="mx-auto mt-2 max-w-lg text-sm leading-6 text-muted-foreground">
                Document an observation, decision, or follow-up for the care
                team.
              </p>
            </div>
          ) : (
            <div className="space-y-8">
              {timelineGroups.map(([year, entries]) => (
                <section aria-labelledby={`timeline-year-${year}`} key={year}>
                  <div className="mb-3 flex items-center gap-3">
                    <h3
                      className="font-serif text-xl font-semibold text-foreground"
                      id={`timeline-year-${year}`}
                    >
                      {year}
                    </h3>
                    <span className="h-px flex-1 bg-border" />
                    <Badge variant="outline">
                      {entries.length}{" "}
                      {entries.length === 1 ? "entry" : "entries"}
                    </Badge>
                  </div>
                  <ol className="relative space-y-4 border-l-2 border-primary/20 pl-5">
                    {entries.map((entry) => (
                      <li className="relative" key={entry.id}>
                        <span className="absolute -left-[1.7rem] top-8 size-3 rounded-full border-2 border-background bg-primary" />
                        <TimelineEntryCard
                          authorName={
                            teamQuery.data?.find(
                              (member) => member.user_id === entry.author_id,
                            )?.full_name ?? null
                          }
                          authorRole={
                            teamQuery.data?.find(
                              (member) => member.user_id === entry.author_id,
                            )?.role ?? null
                          }
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
                </section>
              ))}
            </div>
          )}
        </section>

        <aside className="flex flex-col gap-5 lg:sticky lg:top-24">
          <PatientSharingPanel
            currentUser={currentUser}
            onChanged={refreshPatient}
            patientId={patientId}
            timeline={timeline}
          />
          <Card className="order-3 scroll-mt-24" id="structured-context">
            <CardHeader className="pb-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-xs font-bold uppercase tracking-[0.16em] text-primary">
                    Source-linked data
                  </p>
                  <h2 className="mt-1 flex items-center gap-2 font-serif text-xl font-semibold leading-none tracking-tight">
                    <Database className="size-5 text-primary" /> Structured
                    clinical context
                  </h2>
                </div>
                <Badge variant="secondary">
                  {clinicalFactsQuery.data?.length ?? 0}
                </Badge>
              </div>
              <p className="text-sm text-muted-foreground">
                Normalized facts remain connected to the exact note wording that
                supports them.
              </p>
            </CardHeader>
            <CardContent className="space-y-3">
              {clinicalFactsQuery.isLoading && (
                <Skeleton className="h-24 rounded-xl" />
              )}
              {clinicalFactsQuery.data?.slice(0, 7).map((fact) => (
                <div
                  className="rounded-xl border border-border bg-muted/30 p-3"
                  key={fact.id}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <p className="font-medium capitalize text-foreground">
                      {fact.subject}
                    </p>
                    <Badge
                      className={
                        fact.origin === "ai"
                          ? "bg-ai-muted text-ai-muted-foreground"
                          : "bg-primary/10 text-primary"
                      }
                    >
                      {fact.origin === "ai" && (
                        <Sparkles className="mr-1 size-3" />
                      )}
                      {fact.fact_type.replace(/_/g, " ")}
                    </Badge>
                  </div>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {fact.normalized_value}
                  </p>
                  <Button
                    className="mt-2 h-8 px-2"
                    onClick={() => showPointer(fact.provenance_pointer_id)}
                    size="sm"
                    variant="ghost"
                  >
                    <FileSearch className="size-4" /> View exact source
                  </Button>
                </div>
              ))}
              {!clinicalFactsQuery.isLoading &&
                (clinicalFactsQuery.data?.length ?? 0) === 0 && (
                  <p className="text-sm text-muted-foreground">
                    Structured facts will appear after source validation.
                  </p>
                )}
            </CardContent>
          </Card>
          {(currentUser.role === "clinician" ||
            currentUser.role === "admin") && (
            <Card className="order-4">
              <CardHeader className="pb-3">
                <h2 className="flex items-center gap-2 font-serif text-xl font-semibold leading-none tracking-tight">
                  <ArchiveRestore className="size-5 text-primary" /> Historical
                  retention
                </h2>
                <p className="text-sm text-muted-foreground">
                  Older record bodies can move to encrypted archival storage;
                  their source links, checksums, and audit history remain.
                </p>
              </CardHeader>
              <CardContent className="grid grid-cols-3 gap-2 text-center">
                <div className="rounded-xl bg-muted/50 p-3">
                  <p className="text-xl font-semibold text-foreground">
                    {
                      patientRetention.filter(
                        (item) => item.storage_tier === "cold",
                      ).length
                    }
                  </p>
                  <p className="text-xs text-muted-foreground">Archived</p>
                </div>
                <div className="rounded-xl bg-muted/50 p-3">
                  <p className="text-xl font-semibold text-foreground">
                    {
                      patientRetention.filter(
                        (item) => item.protected_reasons.length > 0,
                      ).length
                    }
                  </p>
                  <p className="text-xs text-muted-foreground">Protected</p>
                </div>
                <div className="rounded-xl bg-muted/50 p-3">
                  <p className="text-xl font-semibold text-foreground">
                    {
                      patientRetention.filter((item) => item.eligible_for_cold)
                        .length
                    }
                  </p>
                  <p className="text-xs text-muted-foreground">Eligible</p>
                </div>
                {patientRetention.some(
                  (item) => item.protected_reasons.length > 0,
                ) && (
                  <p className="col-span-3 text-left text-xs leading-5 text-muted-foreground">
                    Current protection:{" "}
                    {[
                      ...new Set(
                        patientRetention.flatMap(
                          (item) => item.protected_reasons,
                        ),
                      ),
                    ]
                      .map((reason) => reason.replace(/_/g, " "))
                      .join(", ")}
                  </p>
                )}
              </CardContent>
            </Card>
          )}
          {(conflictsQuery.data?.length ?? 0) > 0 && (
            <Card
              className="order-1 scroll-mt-24 border-critical/40"
              id="clinical-conflicts"
            >
              <CardHeader className="pb-3">
                <h2 className="flex items-center gap-2 font-serif text-xl font-semibold leading-none tracking-tight">
                  <AlertTriangle className="size-5 text-critical" /> Clinical
                  conflicts
                </h2>
                <p className="text-sm text-muted-foreground">
                  Conflicting allergies, medicines, doses, routes, frequencies,
                  and care plans stay visible until a clinician records a
                  correction.
                </p>
              </CardHeader>
              <CardContent className="space-y-4">
                {conflictsQuery.data?.map((conflict) => (
                  <div
                    className="space-y-3 rounded-xl border border-critical/30 bg-critical-muted/30 p-3"
                    key={conflict.id}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <p className="font-semibold capitalize">
                        {conflict.normalized_key} {conflict.fact_type}
                      </p>
                      <Badge className="bg-critical-muted text-critical-muted-foreground">
                        {conflict.severity}
                      </Badge>
                    </div>
                    <p className="text-sm text-muted-foreground">
                      Status: {conflict.status}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      {conflict.left_pointer_id && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            showPointer(conflict.left_pointer_id ?? "")
                          }
                        >
                          View first source
                        </Button>
                      )}
                      {conflict.right_pointer_id && (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            showPointer(conflict.right_pointer_id ?? "")
                          }
                        >
                          View conflicting source
                        </Button>
                      )}
                    </div>
                    {conflict.status === "unresolved" &&
                      currentUser.role === "staff" && (
                        <p className="text-sm font-medium text-critical-muted-foreground">
                          High-risk conflicts cannot be dismissed. A clinician
                          must reconcile the two source-linked instructions.
                        </p>
                      )}
                    {conflict.status === "unresolved" &&
                      currentUser.role === "clinician" && (
                        <Button
                          onClick={() => setResolvingConflictId(conflict.id)}
                          size="sm"
                        >
                          Resolve conflict
                        </Button>
                      )}
                    {conflict.resolution && (
                      <p className="text-sm">
                        Resolution: {conflict.resolution}
                      </p>
                    )}
                  </div>
                ))}
                <Dialog
                  open={resolvingConflictId !== null}
                  onOpenChange={(open) => {
                    if (!open && !resolveConflictMutation.isPending) {
                      setResolvingConflictId(null)
                      setCorrectionEntryId("")
                      setConflictResolution("")
                    }
                  }}
                >
                  <DialogContent className="sm:max-w-lg">
                    <DialogHeader>
                      <DialogTitle className="font-serif text-2xl">
                        Resolve clinical conflict
                      </DialogTitle>
                      <DialogDescription>
                        Select the clinician-authored correction and document
                        why it resolves the conflicting instructions.
                      </DialogDescription>
                    </DialogHeader>
                    <form
                      className="space-y-4"
                      onSubmit={(event) => {
                        event.preventDefault()
                        if (resolvingConflictId)
                          resolveConflictMutation.mutate(resolvingConflictId)
                      }}
                    >
                      <div className="space-y-2">
                        <Label htmlFor="conflict-correction-entry">
                          Correction entry
                        </Label>
                        <select
                          className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                          id="conflict-correction-entry"
                          onChange={(event) =>
                            setCorrectionEntryId(event.target.value)
                          }
                          value={correctionEntryId}
                        >
                          <option value="">Select clinician correction</option>
                          {timeline
                            .filter(
                              (entry) =>
                                entry.section === "clinician" &&
                                entry.origin === "human",
                            )
                            .map((entry) => (
                              <option key={entry.id} value={entry.id}>
                                {entry.title}
                              </option>
                            ))}
                        </select>
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="conflict-resolution-reason">
                          Resolution reason
                        </Label>
                        <textarea
                          className="min-h-28 w-full rounded-md border bg-background p-3 text-sm"
                          id="conflict-resolution-reason"
                          onChange={(event) =>
                            setConflictResolution(event.target.value)
                          }
                          value={conflictResolution}
                        />
                      </div>
                      <DialogFooter>
                        <Button
                          disabled={resolveConflictMutation.isPending}
                          onClick={() => setResolvingConflictId(null)}
                          type="button"
                          variant="outline"
                        >
                          Cancel
                        </Button>
                        <Button
                          disabled={
                            !correctionEntryId ||
                            conflictResolution.trim().length < 3 ||
                            resolveConflictMutation.isPending
                          }
                          type="submit"
                        >
                          Resolve with correction
                        </Button>
                      </DialogFooter>
                    </form>
                  </DialogContent>
                </Dialog>
              </CardContent>
            </Card>
          )}
          {canCollaborate && (
            <Card className="order-5">
              <CardHeader className="pb-3">
                <CardTitle className="font-serif text-xl">
                  Portal access
                </CardTitle>
                <p className="text-sm text-muted-foreground">
                  Status:{" "}
                  {patientDetailQuery.data?.portal_access_state.replace(
                    "_",
                    " ",
                  ) ?? "checking"}
                </p>
              </CardHeader>
              <CardContent className="space-y-3">
                {patientDetailQuery.data?.portal_access_state ===
                  "not_invited" && (
                  <Button
                    className="w-full"
                    onClick={() => setPortalInviteOpen(true)}
                  >
                    <Send className="size-4" /> Invite patient
                  </Button>
                )}
                {patientDetailQuery.data?.portal_access_state === "pending" && (
                  <p className="text-sm text-muted-foreground">
                    Invitation pending for up to 24 hours.
                  </p>
                )}
                {patientDetailQuery.data?.portal_access_state === "active" && (
                  <p className="text-sm text-success-muted-foreground">
                    The patient can access approved information.
                  </p>
                )}
                {inviteMutation.isSuccess && (
                  <p className="text-sm text-success-muted-foreground">
                    Invitation sent.
                  </p>
                )}
                {inviteMutation.isError && (
                  <p className="text-sm text-critical-muted-foreground">
                    Invitation was not sent. Check the email and try again.
                  </p>
                )}
                <Dialog
                  open={portalInviteOpen}
                  onOpenChange={(open) =>
                    !inviteMutation.isPending && setPortalInviteOpen(open)
                  }
                >
                  <DialogContent className="sm:max-w-md">
                    <DialogHeader>
                      <DialogTitle className="font-serif text-2xl">
                        Invite patient to My Care
                      </DialogTitle>
                      <DialogDescription>
                        Send a secure, time-limited invitation to the patient’s
                        email address.
                      </DialogDescription>
                    </DialogHeader>
                    <form
                      className="space-y-4"
                      onSubmit={(event) => {
                        event.preventDefault()
                        inviteMutation.mutate()
                      }}
                    >
                      <div className="space-y-2">
                        <Label htmlFor="patient-portal-email">
                          Patient email
                        </Label>
                        <Input
                          autoFocus
                          id="patient-portal-email"
                          onChange={(event) =>
                            setPortalEmail(event.target.value)
                          }
                          required
                          type="email"
                          value={portalEmail}
                        />
                      </div>
                      <DialogFooter>
                        <Button
                          disabled={inviteMutation.isPending}
                          onClick={() => setPortalInviteOpen(false)}
                          type="button"
                          variant="outline"
                        >
                          Cancel
                        </Button>
                        <Button
                          disabled={
                            inviteMutation.isPending || !portalEmail.trim()
                          }
                          type="submit"
                        >
                          <Send className="size-4" /> Send invitation
                        </Button>
                      </DialogFooter>
                    </form>
                  </DialogContent>
                </Dialog>
              </CardContent>
            </Card>
          )}
          <div
            className="order-2 scroll-mt-24 space-y-3"
            id="current-priorities"
          >
            <GlanceTopCard
              busyHighlightId={
                highlightMutation.isPending
                  ? (highlightMutation.variables?.card.highlight_id ?? null)
                  : null
              }
              canReview={canCollaborate}
              cards={glanceQuery.data?.cards ?? []}
              reviewCards={glanceQuery.data?.review_cards ?? []}
              onAction={(card, action) =>
                highlightMutation.mutate({ card, action })
              }
              onDismiss={(card, reason) =>
                highlightMutation.mutate({ card, action: "dismiss", reason })
              }
              onExplain={(card) =>
                clinicalApi.decisionExplanation(card.highlight_id)
              }
              onImpression={(card, rank, viewEventId) =>
                clinicalApi.recordImportanceImpression({
                  highlightId: card.highlight_id,
                  viewEventId,
                  rank,
                  // Ranking is deterministic today. Record an honest exposure
                  // probability until a randomized exploration policy exists;
                  // telemetry must not imply that slot five was sampled.
                  exposureProbability: 1,
                  visibleRatio: 0.5,
                  visibleDurationMs: 2_000,
                })
              }
              onRequestReview={(card) =>
                highlightMutation.mutate({ card, action: "request_review" })
              }
              onSource={showSource}
            />
            {glanceQuery.isError && (
              <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
                <AlertDescription>
                  Current priorities are temporarily unavailable:{" "}
                  {apiErrorMessage(glanceQuery.error)}
                </AlertDescription>
              </Alert>
            )}
            {highlightMutation.isError && (
              <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
                <AlertDescription>
                  {apiErrorMessage(highlightMutation.error)}
                </AlertDescription>
              </Alert>
            )}
          </div>
          <div className="order-6 scroll-mt-24" id="team-discussion">
            <CommentsRail
              currentUser={currentUser}
              entryId={selectedEntry?.id ?? null}
              entryVersionId={selectedEntry?.version_id ?? null}
              readOnly={readOnlyOversight}
            />
          </div>
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
          entryOrigin={versionEntry.origin}
          entrySection={versionEntry.section}
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
              <FileSearch className="text-primary" /> Source details
            </DialogTitle>
            <DialogDescription>
              {evidence?.isHistorical
                ? "This priority came from an earlier version of the note. The original wording is shown below."
                : "The relevant wording is highlighted in the care timeline."}
            </DialogDescription>
          </DialogHeader>
          {evidence && (
            <div className="space-y-4">
              <div className="rounded-xl border bg-muted/40 p-4">
                <p className="font-semibold">{evidence.entryTitle}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {evidence.entryOrigin === "ai"
                    ? "AI-assisted draft"
                    : evidence.entryOrigin === "system"
                      ? "Care service"
                      : evidence.entrySection === "patient"
                        ? "Patient"
                        : (teamQuery.data?.find(
                            (member) => member.user_id === evidence.authorId,
                          )?.full_name ?? "Care team member")}
                  {evidence.sourceDate
                    ? ` · ${formatSingaporeDateTime(evidence.sourceDate)}`
                    : ""}
                </p>
                <p className="mt-2 whitespace-pre-wrap text-sm leading-6 text-foreground/90">
                  {highlightedEvidence(evidence)}
                </p>
              </div>
              <blockquote className="rounded-xl border-l-4 border-warning bg-warning-muted p-4 text-warning-muted-foreground">
                “{evidence.provenance.exact_quote}”
              </blockquote>
              <p className="text-xs text-muted-foreground">
                {evidence.isHistorical
                  ? "Source status: earlier note version"
                  : "Source status: current note version"}
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>
    </div>
  )
}
