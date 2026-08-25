import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  AudioLines,
  CheckCircle2,
  FileCheck2,
  LoaderCircle,
  RefreshCw,
  Save,
  ScrollText,
  Sparkles,
} from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"
import type {
  ClinicalFactPublic,
  MePublic,
  TranscriptRevisionPublic,
  TranscriptSegmentPublic,
  VoiceSessionPublic,
} from "@/client"
import { VoiceService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { apiErrorMessage } from "@/features/api"
import {
  loadAuthorizedAudio,
  voiceSession,
  voiceTranscript,
} from "@/features/voice/voiceApi"

function time(ms: number): string {
  const seconds = Math.floor(ms / 1_000)
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`
}

function vttTime(ms: number): string {
  const hours = Math.floor(ms / 3_600_000)
  const minutes = Math.floor((ms % 3_600_000) / 60_000)
  const seconds = Math.floor((ms % 60_000) / 1_000)
  const millis = ms % 1_000
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`
}

export function segmentForFact(
  fact: ClinicalFactPublic,
  segments: TranscriptSegmentPublic[],
): TranscriptSegmentPublic | undefined {
  return segments.find(
    (segment) =>
      segment.text_start <= fact.transcript_start &&
      segment.text_end >= fact.transcript_end,
  )
}

function StatusBadges({
  session,
  transcript,
}: {
  session: VoiceSessionPublic
  transcript?: TranscriptRevisionPublic
}) {
  return (
    <div className="flex flex-wrap gap-2">
      <Badge className="bg-slate-100 text-slate-800">{session.state}</Badge>
      {transcript?.stale && <Badge variant="destructive">stale</Badge>}
      {transcript?.needs_review && (
        <Badge className="bg-amber-100 text-amber-900">needs review</Badge>
      )}
      {transcript?.fallback && (
        <Badge className="bg-purple-100 text-purple-900">fallback</Badge>
      )}
      {session.state === "ready" && (
        <Badge className="bg-emerald-100 text-emerald-900">ready</Badge>
      )}
      {session.state === "published" && (
        <Badge className="bg-blue-100 text-blue-900">published</Badge>
      )}
    </div>
  )
}

function TranscriptPanel({
  transcript,
  lowConfidenceOnly,
}: {
  transcript: TranscriptRevisionPublic
  lowConfidenceOnly: boolean
}) {
  const segments = transcript.segments.filter(
    (segment) =>
      !lowConfidenceOnly ||
      segment.confidence === null ||
      segment.confidence < 0.75 ||
      Boolean(segment.overlap_group_id),
  )
  return (
    <div className="space-y-3" data-testid="transcript-panel">
      {segments.map((segment) => (
        <article
          id={`voice-segment-${segment.id}`}
          key={segment.id}
          className="scroll-mt-24 rounded-lg border bg-white p-3 focus-within:ring-2 focus-within:ring-teal-500"
        >
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <Badge variant="outline">
              {segment.speaker_id ?? "Speaker unknown"}
            </Badge>
            <button
              type="button"
              className="min-h-11 rounded px-2 font-mono text-teal-800"
              aria-label={`Jump to ${time(segment.start_ms)}`}
            >
              {time(segment.start_ms)}–{time(segment.end_ms)}
            </button>
            {segment.detected_language && (
              <Badge variant="outline">{segment.detected_language}</Badge>
            )}
            {segment.confidence !== null && (
              <Badge
                className={
                  segment.confidence < 0.75
                    ? "bg-amber-100 text-amber-900"
                    : "bg-emerald-100 text-emerald-900"
                }
              >
                {Math.round(segment.confidence * 100)}%
              </Badge>
            )}
            {segment.overlap_group_id && (
              <Badge variant="destructive">overlap</Badge>
            )}
          </div>
          <p className="leading-7 text-slate-800">{segment.text}</p>
        </article>
      ))}
      {segments.length === 0 && (
        <p className="rounded border border-dashed p-4 text-sm text-slate-500">
          No segments match the low-confidence filter.
        </p>
      )}
    </div>
  )
}

function SummaryPanel({
  transcript,
}: {
  transcript: TranscriptRevisionPublic
}) {
  return (
    <div className="space-y-3" data-testid="summary-panel">
      <p className="leading-7 text-slate-800">
        {transcript.summary ?? "Summary pending clinician reanalysis."}
      </p>
      {transcript.warning_codes.length > 0 && (
        <div className="space-y-2">
          {transcript.warning_codes.map((warning) => (
            <div
              className="flex gap-2 rounded bg-amber-50 p-2 text-xs text-amber-950"
              key={warning}
            >
              <AlertTriangle className="size-4 shrink-0" /> {warning}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function FactsPanel({
  transcript,
  onJump,
}: {
  transcript: TranscriptRevisionPublic
  onJump: (fact: ClinicalFactPublic) => void
}) {
  return (
    <div className="space-y-3" data-testid="facts-panel">
      {transcript.facts.map((fact) => (
        <button
          type="button"
          key={fact.id}
          className="min-h-11 w-full rounded-lg border bg-white p-3 text-left hover:border-teal-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-500"
          onClick={() => onJump(fact)}
        >
          <span className="flex items-center justify-between gap-2">
            <strong className="text-sm text-slate-900">{fact.fact_type}</strong>
            <Badge variant="outline">{fact.status}</Badge>
          </span>
          <span className="mt-1 block text-sm text-slate-700">
            {fact.value}
          </span>
          <span className="mt-2 block text-xs text-teal-800">
            {time(fact.audio_start_ms)} · “{fact.exact_quote}”
          </span>
        </button>
      ))}
      {transcript.facts.length === 0 && (
        <p className="rounded border border-dashed p-4 text-sm text-slate-500">
          No evidence-bound facts. Reanalysis is required before publication.
        </p>
      )}
    </div>
  )
}

export function VoiceReviewMode({
  sessionId,
  membershipRole,
}: {
  sessionId: string
  membershipRole: MePublic["role"]
}) {
  const queryClient = useQueryClient()
  const audioRef = useRef<HTMLAudioElement>(null)
  const [audioUrl, setAudioUrl] = useState<string>()
  const [lowConfidenceOnly, setLowConfidenceOnly] = useState(false)
  const [mobileTab, setMobileTab] = useState("transcript")
  const [editing, setEditing] = useState(false)
  const [correction, setCorrection] = useState("")
  const [message, setMessage] = useState<string>()
  const sessionQuery = useQuery({
    queryKey: ["voice-session", sessionId],
    queryFn: () => voiceSession(sessionId),
    refetchInterval: (query) =>
      ["ready", "needs_review", "published"].includes(
        query.state.data?.state ?? "",
      )
        ? false
        : 2_000,
  })
  const transcriptQuery = useQuery({
    queryKey: ["voice-transcript", sessionId],
    queryFn: () => voiceTranscript(sessionId),
    enabled: ["ready", "needs_review", "published"].includes(
      sessionQuery.data?.state ?? "",
    ),
    retry: false,
  })
  const transcript = transcriptQuery.data
  const captionsUrl = useMemo(() => {
    if (!transcript) return undefined
    const cues = transcript.segments
      .map(
        (segment, index) =>
          `${index + 1}\n${vttTime(segment.start_ms)} --> ${vttTime(segment.end_ms)}\n${segment.speaker_id ? `${segment.speaker_id}: ` : ""}${segment.text}\n`,
      )
      .join("\n")
    return `data:text/vtt;charset=utf-8,${encodeURIComponent(`WEBVTT\n\n${cues}`)}`
  }, [transcript])

  useEffect(() => {
    if (transcript && !editing) setCorrection(transcript.text)
  }, [editing, transcript])

  useEffect(() => {
    let cancelled = false
    let activeUrl: string | undefined
    void loadAuthorizedAudio(sessionId)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        activeUrl = url
        setAudioUrl(url)
      })
      .catch(() => setAudioUrl(undefined))
    return () => {
      cancelled = true
      if (activeUrl) URL.revokeObjectURL(activeUrl)
    }
  }, [sessionId])

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["voice-session", sessionId] }),
      queryClient.invalidateQueries({
        queryKey: ["voice-transcript", sessionId],
      }),
    ])
  }
  const correctMutation = useMutation({
    mutationFn: async () =>
      (
        await VoiceService.correct({
          path: { session_id: sessionId },
          body: { text: correction },
        })
      ).data,
    onSuccess: async () => {
      setEditing(false)
      setMessage(
        "Correction saved as a new immutable revision. Results are stale.",
      )
      await invalidate()
    },
    onError: (error) => setMessage(apiErrorMessage(error)),
  })
  const reanalyzeMutation = useMutation({
    mutationFn: async () =>
      (
        await VoiceService.reanalyze({
          path: { session_id: sessionId },
          headers: {
            "Idempotency-Key": `voice-reanalyze-${crypto.randomUUID()}`,
          },
        })
      ).data,
    onSuccess: async () => {
      setMessage(
        "Reanalysis queued. Publication remains disabled until it completes.",
      )
      await invalidate()
    },
    onError: (error) => setMessage(apiErrorMessage(error)),
  })
  const publishMutation = useMutation({
    mutationFn: async () =>
      (await VoiceService.publish({ path: { session_id: sessionId } })).data,
    onSuccess: async () => {
      setMessage(
        "Clinician-reviewed result published as an immutable derived entry.",
      )
      await invalidate()
    },
    onError: (error) => setMessage(apiErrorMessage(error)),
  })

  const jumpToFact = (fact: ClinicalFactPublic) => {
    if (!transcript) return
    const segment = segmentForFact(fact, transcript.segments)
    setMobileTab("transcript")
    requestAnimationFrame(() => {
      document
        .getElementById(`voice-segment-${segment?.id}`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" })
    })
    if (audioRef.current) {
      audioRef.current.currentTime = fact.audio_start_ms / 1_000
      void audioRef.current.play().catch(() => undefined)
    }
  }

  const publishDisabled = useMemo(
    () =>
      membershipRole !== "clinician" ||
      !transcript ||
      transcript.stale ||
      transcript.facts.length === 0 ||
      sessionQuery.data?.state === "published",
    [membershipRole, sessionQuery.data?.state, transcript],
  )

  if (sessionQuery.isLoading) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-teal-700" />
  }
  if (sessionQuery.isError || !sessionQuery.data) {
    return (
      <p className="text-rose-700">{apiErrorMessage(sessionQuery.error)}</p>
    )
  }
  const session = sessionQuery.data

  return (
    <div className="space-y-5" data-testid="voice-review-mode">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm font-semibold uppercase tracking-widest text-teal-700">
            Voice review mode
          </p>
          <h1 className="mt-1 text-3xl font-semibold text-slate-950">
            Transcript, summary & evidence
          </h1>
          <p className="mt-2 max-w-3xl text-sm text-slate-600">
            Final speaker labels and timestamps replace provisional captions.
            Overlap is preserved for review; it is not claimed as perfect source
            separation.
          </p>
        </div>
        <StatusBadges session={session} transcript={transcript} />
      </header>

      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 pt-6">
          {audioUrl ? (
            <audio
              ref={audioRef}
              src={audioUrl}
              controls
              className="min-h-11 flex-1"
            >
              <track
                default
                kind="captions"
                src={captionsUrl ?? "data:text/vtt;charset=utf-8,WEBVTT"}
                srcLang="en"
                label="Transcript"
              />
            </audio>
          ) : (
            <span className="flex min-h-11 items-center gap-2 text-sm text-slate-500">
              <AudioLines className="size-4" /> Authorized audio is pending.
            </span>
          )}
          <Label className="flex min-h-11 items-center gap-2 rounded border px-3 text-sm">
            <input
              type="checkbox"
              checked={lowConfidenceOnly}
              onChange={(event) => setLowConfidenceOnly(event.target.checked)}
            />
            Low confidence / overlap only
          </Label>
        </CardContent>
      </Card>

      {transcript ? (
        <>
          <div className="hidden gap-4 lg:grid lg:grid-cols-3">
            <Card className="min-w-0">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <ScrollText className="size-4" /> Transcript
                </CardTitle>
              </CardHeader>
              <CardContent className="max-h-[62vh] overflow-y-auto">
                <TranscriptPanel
                  transcript={transcript}
                  lowConfidenceOnly={lowConfidenceOnly}
                />
              </CardContent>
            </Card>
            <Card className="min-w-0">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <Sparkles className="size-4" /> Summary
                </CardTitle>
              </CardHeader>
              <CardContent>
                <SummaryPanel transcript={transcript} />
              </CardContent>
            </Card>
            <Card className="min-w-0">
              <CardHeader>
                <CardTitle className="flex items-center gap-2 text-base">
                  <FileCheck2 className="size-4" /> Structured facts
                </CardTitle>
              </CardHeader>
              <CardContent>
                <FactsPanel transcript={transcript} onJump={jumpToFact} />
              </CardContent>
            </Card>
          </div>

          <Tabs
            value={mobileTab}
            onValueChange={setMobileTab}
            className="lg:hidden"
          >
            <TabsList className="grid h-auto min-h-11 w-full grid-cols-3">
              <TabsTrigger value="transcript">Transcript</TabsTrigger>
              <TabsTrigger value="summary">Summary</TabsTrigger>
              <TabsTrigger value="facts">Facts</TabsTrigger>
            </TabsList>
            <TabsContent value="transcript">
              <TranscriptPanel
                transcript={transcript}
                lowConfidenceOnly={lowConfidenceOnly}
              />
            </TabsContent>
            <TabsContent value="summary">
              <SummaryPanel transcript={transcript} />
            </TabsContent>
            <TabsContent value="facts">
              <FactsPanel transcript={transcript} onJump={jumpToFact} />
            </TabsContent>
          </Tabs>

          <Card>
            <CardHeader>
              <CardTitle className="text-base">
                Clinician review controls
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {editing ? (
                <textarea
                  className="min-h-40 w-full rounded-md border p-3 text-sm"
                  value={correction}
                  onChange={(event) => setCorrection(event.target.value)}
                  aria-label="Correct transcript"
                />
              ) : null}
              <div className="flex flex-wrap gap-2">
                {membershipRole === "clinician" && (
                  <Button
                    variant="outline"
                    className="min-h-11"
                    onClick={() =>
                      editing ? correctMutation.mutate() : setEditing(true)
                    }
                    disabled={correctMutation.isPending}
                  >
                    <Save className="mr-2 size-4" />
                    {editing ? "Save new revision" : "Correct transcript"}
                  </Button>
                )}
                {membershipRole === "clinician" && (
                  <Button
                    variant="outline"
                    className="min-h-11"
                    onClick={() => reanalyzeMutation.mutate()}
                    disabled={reanalyzeMutation.isPending}
                  >
                    <RefreshCw className="mr-2 size-4" /> Reanalyze
                  </Button>
                )}
                <Button
                  className="min-h-11 bg-teal-700"
                  onClick={() => publishMutation.mutate()}
                  disabled={publishDisabled || publishMutation.isPending}
                >
                  <CheckCircle2 className="mr-2 size-4" /> Publish reviewed
                  result
                </Button>
              </div>
              {transcript.stale && (
                <p className="text-sm font-medium text-rose-700">
                  Publication disabled: transcript changed after downstream
                  analysis.
                </p>
              )}
            </CardContent>
          </Card>
        </>
      ) : (
        <Card>
          <CardContent className="flex min-h-48 items-center justify-center pt-6 text-slate-600">
            {session.error_code ? (
              <span className="flex items-center gap-2">
                <AlertTriangle className="size-5 text-amber-700" />
                {session.error_code}: encrypted audio is retained; no transcript
                was fabricated.
              </span>
            ) : (
              <span className="flex items-center gap-2">
                <LoaderCircle className="size-5 animate-spin" /> Processing
                persisted audio…
              </span>
            )}
          </CardContent>
        </Card>
      )}

      {message && (
        <p className="rounded border border-teal-100 bg-teal-50 p-3 text-sm text-teal-950">
          {message}
        </p>
      )}
    </div>
  )
}
