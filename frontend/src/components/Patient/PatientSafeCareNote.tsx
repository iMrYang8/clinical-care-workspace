import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  HeartHandshake,
  Link2,
  LoaderCircle,
  Plus,
  ShieldCheck,
  UserRound,
} from "lucide-react"
import { useEffect, useState } from "react"

import type {
  GlancePublic,
  PatientPublic,
  PatientTimelineEntry,
  ProvenanceResolved,
} from "@/client"
import { GlanceTopCard } from "@/components/CareNote/GlanceTopCard"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { apiErrorMessage, patientSafeApi } from "@/features/api"

export type PatientSafeApi = {
  patients: () => Promise<PatientPublic[]>
  timeline: (patientId: string) => Promise<PatientTimelineEntry[]>
  glance: (patientId: string) => Promise<GlancePublic>
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
  })
  const glanceQuery = useQuery({
    queryKey: ["patient-safe", patientId, "glance"],
    queryFn: () => api.glance(patientId!),
    enabled: Boolean(patientId),
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
      <Alert className="border-red-200 bg-red-50 text-red-900">
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
      <header className="rounded-2xl border border-amber-100 bg-gradient-to-br from-amber-50 to-white p-6 shadow-sm">
        <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-center">
          <div className="flex items-center gap-4">
            <span className="grid size-14 place-items-center rounded-2xl bg-amber-200 text-amber-900">
              <UserRound />
            </span>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="font-serif text-3xl font-semibold text-slate-950">
                  My Care · {patient.display_name}
                </h1>
                <Badge className="bg-amber-100 text-amber-800">
                  Synthetic data
                </Badge>
              </div>
              <p className="mt-1 text-sm text-slate-600">
                A patient-safe view with published entries and approved sources
                only.
              </p>
            </div>
          </div>
          <Badge className="w-fit bg-emerald-100 text-emerald-800">
            <ShieldCheck /> Patient-facing
          </Badge>
        </div>
      </header>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(19rem,1fr)]">
        <section aria-labelledby="my-timeline-heading" className="space-y-4">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <p className="text-xs font-bold uppercase tracking-[0.18em] text-amber-700">
                Shared with me
              </p>
              <h2
                className="font-serif text-3xl font-semibold"
                id="my-timeline-heading"
              >
                My timeline
              </h2>
            </div>
            <Button onClick={() => setComposerOpen((open) => !open)}>
              <Plus /> Add my insight
            </Button>
          </div>

          {composerOpen && (
            <Card className="border-amber-200 bg-amber-50/60">
              <CardHeader>
                <CardTitle className="font-serif">A note from me</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid gap-2">
                  <Label htmlFor="patient-insight-title">Title</Label>
                  <Input
                    id="patient-insight-title"
                    onChange={(event) => setTitle(event.target.value)}
                    placeholder="What would you like the care team to know?"
                    value={title}
                  />
                </div>
                <div className="grid gap-2">
                  <Label htmlFor="patient-insight-content">My insight</Label>
                  <textarea
                    className="min-h-32 rounded-xl border bg-white p-3 text-sm leading-6 outline-none focus:ring-2 focus:ring-amber-500"
                    id="patient-insight-content"
                    onChange={(event) => setContent(event.target.value)}
                    value={content}
                  />
                </div>
                <Button
                  disabled={
                    !title.trim() ||
                    !content.trim() ||
                    insightMutation.isPending
                  }
                  onClick={() => insightMutation.mutate()}
                >
                  {insightMutation.isPending ? (
                    <LoaderCircle className="animate-spin" />
                  ) : (
                    <Plus />
                  )}
                  Add to My Care
                </Button>
              </CardContent>
            </Card>
          )}

          {timelineQuery.isLoading && (
            <LoaderCircle className="animate-spin text-amber-600" />
          )}
          {timelineQuery.data?.length === 0 && (
            <div className="rounded-2xl border border-dashed bg-white px-6 py-12 text-center">
              <HeartHandshake className="mx-auto mb-3 size-8 text-amber-600" />
              <p className="font-medium text-slate-800">
                No published entries yet
              </p>
              <p className="mt-1 text-sm text-slate-500">
                You can still add your own insight above.
              </p>
            </div>
          )}
          <ol className="space-y-4">
            {timelineQuery.data?.map((entry) => (
              <li key={entry.id}>
                <article
                  className="scroll-mt-24 rounded-2xl border bg-white p-5 outline-none focus-visible:ring-2 focus-visible:ring-amber-500"
                  data-patient-version-id={entry.version_id}
                  tabIndex={-1}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge className="bg-amber-100 text-amber-900">
                      Patient-safe entry
                    </Badge>
                    <Badge variant="outline">v{entry.version_no}</Badge>
                  </div>
                  <h3 className="mt-3 font-serif text-xl font-semibold">
                    {entry.title}
                  </h3>
                  <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-slate-700">
                    {entry.content}
                  </p>
                  <time
                    className="mt-3 block text-xs text-slate-500"
                    dateTime={entry.created_at}
                  >
                    {new Date(entry.created_at).toLocaleString()}
                  </time>
                </article>
              </li>
            ))}
          </ol>
        </section>

        <aside className="space-y-4 lg:sticky lg:top-24">
          <GlanceTopCard
            cards={glanceQuery.data?.cards ?? []}
            onSource={(card) => showSource(card.provenance_pointer_id)}
          />
          {source && (
            <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4">
              <p className="flex items-center gap-2 font-semibold text-amber-950">
                <Link2 /> Approved source
              </p>
              <blockquote className="mt-2 text-sm leading-6 text-amber-900">
                “{source.exact_quote}”
              </blockquote>
            </div>
          )}
          {(timelineQuery.isError ||
            glanceQuery.isError ||
            insightMutation.isError) && (
            <Alert className="border-red-200 bg-red-50 text-red-900">
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
