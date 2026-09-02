import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  CheckCircle2,
  Clock3,
  Eye,
  LoaderCircle,
  Send,
  Undo2,
} from "lucide-react"
import { useMemo, useState } from "react"

import type { MePublic } from "@/client"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  type ClinicalFactAssertion,
  type ClinicalTimelineEntry,
  clinicalApi,
  type MedicationAssertion,
  type MedicationReviewInput,
  type PatientPublication,
  type PatientSharingRequest,
  patientSharingErrorMessage,
} from "@/features/api"
import { formatSingaporeDateTime } from "@/lib/dateTime"

type PatientSharingPanelProps = {
  patientId: string
  currentUser: MePublic
  timeline: ClinicalTimelineEntry[]
  clinicalFacts: ClinicalFactAssertion[]
  clinicalFactsReady: boolean
  onChanged: () => void | Promise<void>
}

function statusBadge(status: string) {
  if (status === "approved")
    return (
      <Badge className="bg-success-muted text-success-muted-foreground">
        Approved
      </Badge>
    )
  if (status === "withdrawn") return <Badge variant="outline">Withdrawn</Badge>
  if (status === "superseded")
    return <Badge variant="outline">Newer version submitted</Badge>
  if (status === "rejected")
    return <Badge variant="destructive">Not approved</Badge>
  return (
    <Badge className="bg-warning-muted text-warning-muted-foreground">
      Awaiting review
    </Badge>
  )
}

function medicationReviewPreview(
  facts: ClinicalFactAssertion[],
  versionId?: string,
): { assertions: MedicationAssertion[]; incomplete: boolean } {
  if (!versionId) return { assertions: [], incomplete: false }
  const regimenFacts = facts.filter(
    (fact) =>
      fact.source_entry_version_id === versionId &&
      fact.assertion_state !== "superseded" &&
      ["medication", "dose", "route", "frequency"].includes(fact.fact_type),
  )
  if (regimenFacts.length === 0) return { assertions: [], incomplete: false }
  const grouped = new Map<
    string,
    Partial<MedicationAssertion> & { assertion_id: string }
  >()
  let incomplete = false
  for (const fact of regimenFacts) {
    const medication = fact.medication?.trim()
    if (!medication) {
      incomplete = true
      continue
    }
    const key = medication.toLocaleLowerCase()
    const current = grouped.get(key) ?? {
      assertion_id: fact.id,
      medication,
    }
    if (fact.fact_type === "medication") current.assertion_id = fact.id
    if (fact.dose_value !== null && fact.dose_value !== undefined)
      current.dose_value = fact.dose_value
    if (fact.dose_unit) current.dose_unit = fact.dose_unit
    if (fact.route) current.route = fact.route
    if (fact.frequency) current.frequency = fact.frequency
    grouped.set(key, current)
  }
  const assertions: MedicationAssertion[] = []
  for (const regimen of grouped.values()) {
    if (
      !regimen.medication ||
      regimen.dose_value === null ||
      regimen.dose_value === undefined ||
      !regimen.dose_unit ||
      !regimen.route ||
      !regimen.frequency
    ) {
      incomplete = true
      continue
    }
    assertions.push(regimen as MedicationAssertion)
  }
  return { assertions, incomplete }
}

function confirmedReviews(
  assertions: MedicationAssertion[],
  confirmed: Set<string>,
): MedicationReviewInput[] {
  return assertions
    .filter((assertion) => confirmed.has(assertion.assertion_id))
    .map((assertion) => ({ ...assertion, confirmed: true as const }))
}

function MedicationReviewChecklist({
  assertions,
  confirmed,
  onChange,
  idPrefix,
}: {
  assertions: MedicationAssertion[]
  confirmed: Set<string>
  onChange: (next: Set<string>) => void
  idPrefix: string
}) {
  if (assertions.length === 0)
    return (
      <p className="rounded-xl border bg-muted/30 p-3 text-sm text-muted-foreground">
        The backend reports no structured medication assertions for this exact
        version. An empty medication review is valid only in this case.
      </p>
    )
  return (
    <fieldset className="space-y-3 rounded-xl border border-warning/40 bg-warning-muted/20 p-3">
      <legend className="px-1 text-sm font-semibold">
        Confirm every medication field
      </legend>
      {assertions.map((assertion) => {
        const id = `${idPrefix}-${assertion.assertion_id}`
        return (
          <label
            className="flex items-start gap-3 rounded-lg border bg-card p-3 text-sm"
            htmlFor={id}
            key={assertion.assertion_id}
          >
            <input
              checked={confirmed.has(assertion.assertion_id)}
              className="mt-1 size-4"
              id={id}
              onChange={(event) => {
                const next = new Set(confirmed)
                if (event.target.checked) next.add(assertion.assertion_id)
                else next.delete(assertion.assertion_id)
                onChange(next)
              }}
              type="checkbox"
            />
            <span>
              <strong>{assertion.medication}</strong>
              <span className="mt-1 block leading-6 text-muted-foreground">
                Dose {assertion.dose_value} {assertion.dose_unit} · route{" "}
                {assertion.route} · frequency {assertion.frequency}
              </span>
              <span className="mt-1 block text-xs text-warning-muted-foreground">
                I reviewed medication, dose, unit, route, and frequency against
                the exact source.
              </span>
            </span>
          </label>
        )
      })}
    </fieldset>
  )
}

export function PatientSharingPanel({
  patientId,
  currentUser,
  timeline,
  clinicalFacts,
  clinicalFactsReady,
  onChanged,
}: PatientSharingPanelProps) {
  const queryClient = useQueryClient()
  const [selectedEntryId, setSelectedEntryId] = useState("")
  const [reviewing, setReviewing] = useState<PatientSharingRequest | null>(null)
  const [withdrawing, setWithdrawing] = useState<PatientPublication | null>(
    null,
  )
  const [correcting, setCorrecting] = useState<PatientPublication | null>(null)
  const [correctionIdempotencyKey, setCorrectionIdempotencyKey] = useState("")
  const [replacementVersionId, setReplacementVersionId] = useState("")
  const [confirmedMedicationIds, setConfirmedMedicationIds] = useState(
    new Set<string>(),
  )
  const [error, setError] = useState<string | null>(null)
  const [correctionNotice, setCorrectionNotice] = useState<string | null>(null)

  const requestsQuery = useQuery({
    queryKey: ["patients", patientId, "patient-sharing-requests"],
    queryFn: () => clinicalApi.patientSharingRequests(patientId),
  })
  const publicationsQuery = useQuery({
    queryKey: ["patients", patientId, "patient-publications"],
    queryFn: () => clinicalApi.patientPublications(patientId),
  })
  const requestedVersionQuery = useQuery({
    queryKey: ["entries", reviewing?.entry_id, "versions"],
    queryFn: () => clinicalApi.versions(reviewing?.entry_id ?? ""),
    enabled: reviewing !== null,
  })

  const pendingRequests = useMemo(
    () =>
      (requestsQuery.data ?? []).filter(
        (request) => request.status === "pending",
      ),
    [requestsQuery.data],
  )
  const activePublications = useMemo(
    () =>
      (publicationsQuery.data ?? []).filter(
        (publication) => publication.withdrawn_at === null,
      ),
    [publicationsQuery.data],
  )
  const eligibleEntries = useMemo(() => {
    const pendingVersions = new Set(
      pendingRequests.map((request) => request.entry_version_id),
    )
    const publishedVersions = new Set(
      activePublications.map((publication) => publication.entry_version_id),
    )
    return timeline.filter(
      (entry) =>
        entry.origin === "human" &&
        entry.section === "staff" &&
        !pendingVersions.has(entry.version_id) &&
        !publishedVersions.has(entry.version_id),
    )
  }, [activePublications, pendingRequests, timeline])
  const requestedVersion = requestedVersionQuery.data?.find(
    (version) => version.id === reviewing?.entry_version_id,
  )
  const currentEntry = timeline.find(
    (entry) => entry.id === reviewing?.entry_id,
  )
  const requestIsCurrent =
    reviewing !== null &&
    currentEntry?.version_id === reviewing.entry_version_id
  const reviewMedicationPreview = medicationReviewPreview(
    clinicalFacts,
    reviewing?.entry_version_id,
  )
  const reviewMedicationAssertions = reviewMedicationPreview.assertions
  const reviewMedicationIncomplete =
    !clinicalFactsReady || reviewMedicationPreview.incomplete
  const correctionMedicationPreview = medicationReviewPreview(
    clinicalFacts,
    replacementVersionId,
  )
  const correctionMedicationAssertions = correctionMedicationPreview.assertions
  const correctionMedicationIncomplete =
    !clinicalFactsReady || correctionMedicationPreview.incomplete
  const replacementCandidates = correcting
    ? timeline.filter(
        (entry) =>
          entry.id === correcting.entry_id &&
          entry.version_id !== correcting.entry_version_id,
      )
    : []
  const sharingStateReady =
    requestsQuery.isSuccess && publicationsQuery.isSuccess
  const sharingStateLoading =
    requestsQuery.isLoading || publicationsQuery.isLoading

  const refresh = async () => {
    setError(null)
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "patient-sharing-requests"],
      }),
      queryClient.invalidateQueries({
        queryKey: ["patients", patientId, "patient-publications"],
      }),
      onChanged(),
    ])
  }

  const requestMutation = useMutation({
    mutationFn: async () => {
      const entry = timeline.find((item) => item.id === selectedEntryId)
      if (!entry) throw new Error("Selected note is unavailable")
      return clinicalApi.requestPatientSharing(entry.id, entry.version_id)
    },
    onSuccess: async () => {
      setSelectedEntryId("")
      await refresh()
    },
    onError: (caught) => setError(patientSharingErrorMessage(caught)),
  })
  const approveMutation = useMutation({
    mutationFn: (requestId: string) =>
      clinicalApi.approvePatientSharing(
        requestId,
        confirmedReviews(reviewMedicationAssertions, confirmedMedicationIds),
      ),
    onSuccess: async () => {
      setReviewing(null)
      setConfirmedMedicationIds(new Set())
      await refresh()
    },
    onError: (caught) => setError(patientSharingErrorMessage(caught)),
  })
  const withdrawMutation = useMutation({
    mutationFn: (publicationId: string) =>
      clinicalApi.withdrawPatientPublication(publicationId),
    onSuccess: async () => {
      setWithdrawing(null)
      await refresh()
    },
    onError: (caught) => setError(patientSharingErrorMessage(caught)),
  })
  const correctionMutation = useMutation({
    mutationFn: () => {
      if (!correcting || !replacementVersionId || !correctionIdempotencyKey)
        throw new Error("Correction selection is incomplete")
      return clinicalApi.correctPatientPublication(
        correcting.id,
        {
          replacement_entry_version_id: replacementVersionId,
          medication_reviews: confirmedReviews(
            correctionMedicationAssertions,
            confirmedMedicationIds,
          ),
          outreach_required: true,
        },
        correctionIdempotencyKey,
      )
    },
    onSuccess: async (publication) => {
      if (publication.delivery_warning === "notification_queue_failed") {
        setCorrectionNotice(
          "Correction published, but outreach could not be queued. The outreach work item remains pending for follow-up.",
        )
      } else if (
        publication.delivery_warning === "notification_delivery_failed"
      ) {
        setCorrectionNotice(
          "Correction published, but delivery failed. The notification remains visible for retry and outreach follow-up.",
        )
      } else if (publication.delivery_warning === "notification_revoked") {
        setCorrectionNotice(
          "Correction published, but the notification was revoked. Direct outreach remains required.",
        )
      } else {
        setCorrectionNotice(
          `Correction published. Outreach is ${publication.notification_state ?? "pending"}.`,
        )
      }
      setCorrecting(null)
      setCorrectionIdempotencyKey("")
      setReplacementVersionId("")
      setConfirmedMedicationIds(new Set())
      await refresh()
    },
    onError: (caught) => setError(patientSharingErrorMessage(caught)),
  })

  return (
    <>
      <Card className="order-3 scroll-mt-24" id="patient-sharing">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase tracking-[0.16em] text-primary">
                Patient portal
              </p>
              <h2 className="mt-1 font-serif text-xl font-semibold leading-none tracking-tight">
                Patient sharing
              </h2>
            </div>
            <Badge variant="secondary">{pendingRequests.length} pending</Badge>
          </div>
          <p className="text-sm leading-6 text-muted-foreground">
            Care staff request sharing. A clinician reviews the exact saved
            version before it appears in the patient portal.
          </p>
        </CardHeader>
        <CardContent className="space-y-4">
          {currentUser.role === "staff" && sharingStateReady && (
            <div className="space-y-2 rounded-xl border bg-muted/30 p-3">
              <label className="text-sm font-medium" htmlFor="sharing-entry">
                Care staff note
              </label>
              <select
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                id="sharing-entry"
                onChange={(event) => setSelectedEntryId(event.target.value)}
                value={selectedEntryId}
              >
                <option value="">Select a note to share</option>
                {eligibleEntries.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.title}
                  </option>
                ))}
              </select>
              <Button
                className="w-full"
                data-testid="request-patient-sharing"
                disabled={!selectedEntryId || requestMutation.isPending}
                onClick={() => requestMutation.mutate()}
                size="sm"
              >
                {requestMutation.isPending ? (
                  <LoaderCircle className="animate-spin" />
                ) : (
                  <Send />
                )}
                Request clinician review
              </Button>
              {eligibleEntries.length === 0 && (
                <p className="text-xs leading-5 text-muted-foreground">
                  All current care staff notes are already under review or
                  shared.
                </p>
              )}
            </div>
          )}

          {sharingStateLoading && (
            <p className="text-sm text-muted-foreground">
              Loading patient sharing…
            </p>
          )}
          {requestsQuery.isError && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>
                Patient sharing requests could not be loaded. Try again before
                reviewing or submitting a note.
              </AlertDescription>
            </Alert>
          )}
          {publicationsQuery.isError && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>
                Currently shared notes could not be loaded. Try again before
                changing patient access.
              </AlertDescription>
            </Alert>
          )}
          {(requestsQuery.data ?? []).map((request) => (
            <div
              className="space-y-2 rounded-xl border border-border p-3"
              data-testid={`sharing-request-${request.id}`}
              key={request.id}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="font-medium text-foreground">
                    {request.entry_title}
                  </p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    Requested by {request.requested_by_name} ·{" "}
                    {formatSingaporeDateTime(request.created_at)}
                  </p>
                </div>
                {statusBadge(request.status)}
              </div>
              {request.reviewed_by_name && request.reviewed_at && (
                <p className="flex items-center gap-1 text-xs text-muted-foreground">
                  <CheckCircle2 className="size-3" /> Reviewed by{" "}
                  {request.reviewed_by_name} ·{" "}
                  {formatSingaporeDateTime(request.reviewed_at)}
                </p>
              )}
              {currentUser.role === "clinician" &&
                request.status === "pending" && (
                  <Button
                    className="w-full"
                    onClick={() => {
                      setError(null)
                      setConfirmedMedicationIds(new Set())
                      setReviewing(request)
                    }}
                    size="sm"
                    variant="outline"
                  >
                    <Eye /> Review exact version
                  </Button>
                )}
            </div>
          ))}
          {requestsQuery.isSuccess &&
            (requestsQuery.data?.length ?? 0) === 0 && (
              <p className="text-sm text-muted-foreground">
                No patient sharing requests yet.
              </p>
            )}

          {activePublications.length > 0 && (
            <div className="space-y-2 border-t pt-4">
              <p className="text-sm font-semibold text-foreground">
                Currently shared
              </p>
              {activePublications.map((publication) => (
                <div
                  className="rounded-xl border bg-success-muted/30 p-3"
                  key={publication.id}
                >
                  <p className="font-medium">{publication.entry_title}</p>
                  <p className="mt-1 text-xs leading-5 text-muted-foreground">
                    Approved by {publication.approved_by_name} ·{" "}
                    {formatSingaporeDateTime(publication.approved_at)}
                  </p>
                  {currentUser.role === "clinician" && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {timeline.some(
                        (entry) =>
                          entry.id === publication.entry_id &&
                          entry.version_id !== publication.entry_version_id,
                      ) && (
                        <Button
                          onClick={() => {
                            setError(null)
                            setCorrectionNotice(null)
                            setConfirmedMedicationIds(new Set())
                            setReplacementVersionId("")
                            setCorrectionIdempotencyKey(crypto.randomUUID())
                            setCorrecting(publication)
                          }}
                          size="sm"
                        >
                          Publish linked correction
                        </Button>
                      )}
                      <Button
                        onClick={() => {
                          setError(null)
                          setWithdrawing(publication)
                        }}
                        size="sm"
                        variant="outline"
                      >
                        <Undo2 /> Withdraw from patient portal
                      </Button>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {error && (
            <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          {correctionNotice && (
            <Alert
              aria-live="polite"
              className="border-warning/40 bg-warning-muted text-warning-muted-foreground"
            >
              <AlertDescription>{correctionNotice}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={reviewing !== null}
        onOpenChange={(open) =>
          !open && !approveMutation.isPending && setReviewing(null)
        }
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="font-serif text-2xl">
              Review patient sharing
            </DialogTitle>
            <DialogDescription>
              Confirm the exact immutable version and its clinical safety before
              publishing it to the patient portal.
            </DialogDescription>
          </DialogHeader>
          {reviewing && (
            <div className="space-y-4">
              <div className="rounded-xl border bg-muted/30 p-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="font-serif text-lg font-semibold">
                    {requestedVersion?.title ?? reviewing.entry_title}
                  </p>
                  <Badge variant="outline">Saved version</Badge>
                </div>
                {requestedVersionQuery.isLoading ? (
                  <p className="mt-3 text-sm text-muted-foreground">
                    Loading exact source…
                  </p>
                ) : (
                  <p className="mt-3 whitespace-pre-wrap text-sm leading-6 text-foreground/90">
                    {requestedVersion?.content ??
                      "The requested source version is no longer available."}
                  </p>
                )}
              </div>
              {!requestIsCurrent && (
                <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
                  <Clock3 className="size-4" />
                  <AlertDescription>
                    This note changed after the request. Submit its latest saved
                    version for a new review.
                  </AlertDescription>
                </Alert>
              )}
              <p className="text-sm leading-6 text-muted-foreground">
                Publishing also verifies redaction status, exact source binding,
                and unresolved high-risk conflicts. A failed safety gate keeps
                the note internal.
              </p>
              <MedicationReviewChecklist
                assertions={reviewMedicationAssertions}
                confirmed={confirmedMedicationIds}
                idPrefix="sharing-medication"
                onChange={setConfirmedMedicationIds}
              />
              {reviewMedicationIncomplete && (
                <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
                  <AlertDescription>
                    Structured medication review data for this exact version is
                    missing or incomplete. Publication remains blocked.
                  </AlertDescription>
                </Alert>
              )}
            </div>
          )}
          <DialogFooter>
            <Button
              disabled={approveMutation.isPending}
              onClick={() => setReviewing(null)}
              variant="outline"
            >
              Cancel
            </Button>
            <Button
              data-testid="approve-patient-sharing"
              disabled={
                !reviewing ||
                !requestedVersion ||
                !requestIsCurrent ||
                confirmedMedicationIds.size !==
                  reviewMedicationAssertions.length ||
                reviewMedicationIncomplete ||
                approveMutation.isPending
              }
              onClick={() => reviewing && approveMutation.mutate(reviewing.id)}
            >
              {approveMutation.isPending ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <CheckCircle2 />
              )}
              Approve and publish
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={correcting !== null}
        onOpenChange={(open) => {
          if (!open && !correctionMutation.isPending) {
            setCorrecting(null)
            setCorrectionIdempotencyKey("")
            setReplacementVersionId("")
            setConfirmedMedicationIds(new Set())
          }
        }}
      >
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="font-serif text-2xl">
              Publish linked correction
            </DialogTitle>
            <DialogDescription>
              This atomically withdraws the incorrect publication, publishes the
              selected same-entry replacement, clears open patient views, and
              queues correction outreach. The original receipt remains.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label
                className="text-sm font-medium"
                htmlFor="replacement-version"
              >
                Replacement version
              </label>
              <select
                className="h-10 w-full rounded-md border bg-background px-3 text-sm"
                id="replacement-version"
                onChange={(event) =>
                  setReplacementVersionId(event.target.value)
                }
                value={replacementVersionId}
              >
                <option value="">Select reviewed replacement</option>
                {replacementCandidates.map((entry) => (
                  <option key={entry.version_id} value={entry.version_id}>
                    v{entry.version_no} · {entry.title}
                  </option>
                ))}
              </select>
            </div>
            <MedicationReviewChecklist
              assertions={correctionMedicationAssertions}
              confirmed={confirmedMedicationIds}
              idPrefix="correction-medication"
              onChange={setConfirmedMedicationIds}
            />
            {correctionMedicationIncomplete && replacementVersionId && (
              <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
                <AlertDescription>
                  The replacement version has incomplete structured medication
                  data. This correction remains blocked until it can be reviewed
                  exactly.
                </AlertDescription>
              </Alert>
            )}
            <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
              <AlertDescription>
                Direct outreach is required for this correction. The patient
                will be asked to acknowledge the replacement.
              </AlertDescription>
            </Alert>
          </div>
          <DialogFooter>
            <Button
              disabled={correctionMutation.isPending}
              onClick={() => setCorrecting(null)}
              variant="outline"
            >
              Cancel
            </Button>
            <Button
              disabled={
                !replacementVersionId ||
                confirmedMedicationIds.size !==
                  correctionMedicationAssertions.length ||
                correctionMedicationIncomplete ||
                correctionMutation.isPending
              }
              onClick={() => correctionMutation.mutate()}
            >
              {correctionMutation.isPending && (
                <LoaderCircle className="animate-spin" />
              )}
              Withdraw, replace, and notify
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={withdrawing !== null}
        onOpenChange={(open) =>
          !open && !withdrawMutation.isPending && setWithdrawing(null)
        }
      >
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle className="font-serif text-2xl">
              Withdraw patient sharing?
            </DialogTitle>
            <DialogDescription>
              The note will disappear from the patient portal. The approved
              version, receipt, and audit history remain available to the care
              team.
            </DialogDescription>
          </DialogHeader>
          <p className="rounded-xl border bg-muted/30 p-3 font-medium">
            {withdrawing?.entry_title}
          </p>
          <DialogFooter>
            <Button
              disabled={withdrawMutation.isPending}
              onClick={() => setWithdrawing(null)}
              variant="outline"
            >
              Keep shared
            </Button>
            <Button
              data-testid="withdraw-patient-sharing"
              disabled={!withdrawing || withdrawMutation.isPending}
              onClick={() =>
                withdrawing && withdrawMutation.mutate(withdrawing.id)
              }
              variant="destructive"
            >
              {withdrawMutation.isPending ? (
                <LoaderCircle className="animate-spin" />
              ) : (
                <Undo2 />
              )}
              Withdraw
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
