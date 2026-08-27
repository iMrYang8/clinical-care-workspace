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

const sessionStatusLabels: Record<string, string> = {
  created: "Ready to record",
  recording: "Recording",
  finalizing: "Saving recording",
  assembling: "Preparing audio",
  preprocessing: "Preparing audio",
  transcribing: "Creating transcript",
  redacting: "Protecting patient information",
  extracting: "Preparing clinical review",
  ready: "Ready for review",
  needs_review: "Review required",
  published: "Published",
  failed: "Processing issue",
}

const factStatusLabels: Record<string, string> = {
  proposed: "Suggested",
  accepted: "Confirmed",
  rejected: "Dismissed",
}

function clinicalLabel(value: string): string {
  return value
    .replace(/_/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase())
}

function speakerLabel(speakerId: string | null): string {
  if (!speakerId) return "Speaker not identified"
  const match = speakerId.match(/(?:SPEAKER[_ -]?)?(\d+)$/i)
  return match ? `Speaker ${Number(match[1]) + 1}` : "Speaker"
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
      <Badge className="bg-muted text-muted-foreground">
        {sessionStatusLabels[session.state] ?? "Processing"}
      </Badge>
      {transcript?.stale && (
        <Badge variant="destructive">Update required</Badge>
      )}
      {transcript?.needs_review && (
        <Badge className="bg-review-required-muted text-review-required-muted-foreground">
          Review required
        </Badge>
      )}
      {transcript?.fallback && (
        <Badge className="bg-ai-muted text-ai-muted-foreground">
          Limited processing
        </Badge>
      )}
      {session.state === "ready" && (
        <Badge className="bg-success-muted text-success-muted-foreground">
          Review available
        </Badge>
      )}
      {session.state === "published" && (
        <Badge className="bg-success-muted text-success-muted-foreground">
          Shared with care record
        </Badge>
      )}
    </div>
  )
}

function TranscriptPanel({
  transcript,
  lowConfidenceOnly,
  onSeek,
  layout,
}: {
  transcript: TranscriptRevisionPublic
  lowConfidenceOnly: boolean
  onSeek: (startMs: number) => void
  layout: "desktop" | "mobile"
}) {
  const calibratedConfidence = (segment: TranscriptSegmentPublic) =>
    segment.confidence_source?.startsWith("calibrated:")
      ? segment.confidence
      : null
  const segments = transcript.segments.filter(
    (segment) =>
      !lowConfidenceOnly ||
      calibratedConfidence(segment) === null ||
      (calibratedConfidence(segment) ?? 0) < 0.85 ||
      Boolean(segment.overlap_group_id),
  )
  return (
    <div
      className="space-y-3"
      data-testid={`transcript-panel-${layout}`}
      data-voice-layout={layout}
    >
      {segments.map((segment) => (
        <article
          id={`voice-segment-${layout}-${segment.id}`}
          key={segment.id}
          className="scroll-mt-24 rounded-lg border bg-card p-3 focus-within:ring-2 focus-within:ring-primary"
        >
          <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
            <Badge variant="outline">{speakerLabel(segment.speaker_id)}</Badge>
            <button
              type="button"
              className="min-h-11 rounded px-2 font-mono text-primary"
              aria-label={`Jump to ${time(segment.start_ms)}`}
              onClick={() => onSeek(segment.start_ms)}
            >
              {time(segment.start_ms)}–{time(segment.end_ms)}
            </button>
            {segment.detected_language && (
              <Badge variant="outline">{segment.detected_language}</Badge>
            )}
            {calibratedConfidence(segment) !== null && (
              <Badge
                className={
                  (calibratedConfidence(segment) ?? 0) < 0.85
                    ? "bg-review-required-muted text-review-required-muted-foreground"
                    : "bg-success-muted text-success-muted-foreground"
                }
              >
                {(calibratedConfidence(segment) ?? 0) >= 0.95
                  ? "High confidence"
                  : (calibratedConfidence(segment) ?? 0) >= 0.85
                    ? "Medium confidence"
                    : "Low confidence"}
              </Badge>
            )}
            {calibratedConfidence(segment) === null && (
              <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                Confidence unavailable
              </Badge>
            )}
            {segment.overlap_group_id && (
              <Badge variant="destructive">Overlapping speech</Badge>
            )}
          </div>
          <p className="leading-7 text-foreground">{segment.text}</p>
        </article>
      ))}
      {segments.length === 0 && (
        <p className="rounded border border-dashed p-4 text-sm text-muted-foreground">
          No uncertain or overlapping speech is shown.
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
      <p className="leading-7 text-foreground">
        {transcript.summary ?? "The clinical summary is being updated."}
      </p>
      {transcript.warning_codes.length > 0 && (
        <div className="space-y-2">
          {transcript.warning_codes.slice(0, 1).map((warning) => (
            <div
              className="flex gap-2 rounded bg-review-required-muted p-2 text-xs text-review-required-muted-foreground"
              key={warning}
            >
              <AlertTriangle className="size-4 shrink-0" /> Review the
              transcript carefully before publishing.
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
          className="min-h-11 w-full rounded-lg border bg-card p-3 text-left hover:border-primary/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          onClick={() => onJump(fact)}
        >
          <span className="flex items-center justify-between gap-2">
            <strong className="text-sm text-foreground">
              {clinicalLabel(fact.fact_type)}
            </strong>
            <Badge variant="outline">
              {factStatusLabels[fact.status] ?? "For review"}
            </Badge>
          </span>
          <span className="mt-1 block text-sm text-foreground/90">
            {fact.value}
          </span>
          <span className="mt-2 block text-xs text-primary">
            {time(fact.audio_start_ms)} · “{fact.exact_quote}”
          </span>
        </button>
      ))}
      {transcript.facts.length === 0 && (
        <p className="rounded border border-dashed p-4 text-sm text-muted-foreground">
          Clinical findings are not ready. Update the summary and findings
          before publishing.
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
          `${index + 1}\n${vttTime(segment.start_ms)} --> ${vttTime(segment.end_ms)}\n${speakerLabel(segment.speaker_id)}: ${segment.text}\n`,
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
    const audioState = sessionQuery.data?.state ?? "created"
    if (
      ![
        "transcribing",
        "redacting",
        "extracting",
        "ready",
        "needs_review",
        "published",
      ].includes(audioState)
    ) {
      setAudioUrl(undefined)
      return () => {
        cancelled = true
      }
    }
    void loadAuthorizedAudio(sessionId)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url)
          return
        }
        activeUrl = url
        setAudioUrl(url)
      })
      .catch(() => {
        if (!cancelled) setAudioUrl(undefined)
      })
    return () => {
      cancelled = true
      if (activeUrl) URL.revokeObjectURL(activeUrl)
    }
  }, [sessionId, sessionQuery.data?.state])

  const invalidate = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["voice-session", sessionId] }),
      queryClient.invalidateQueries({
        queryKey: ["voice-transcript", sessionId],
      }),
    ])
  }
  const correctMutation = useMutation({
    mutationFn: async () => {
      if (!transcript) throw new Error("Transcript is not loaded")
      return (
        await VoiceService.correct({
          path: { session_id: sessionId },
          body: {
            expected_revision_id: transcript.id,
            text: correction,
          },
        })
      ).data
    },
    onSuccess: async () => {
      setEditing(false)
      setMessage(
        "Transcript correction saved. Update the summary and findings.",
      )
      await invalidate()
    },
    onError: () =>
      setMessage(
        "The transcript correction could not be saved. Check the recording state and try again.",
      ),
  })
  const reanalyzeMutation = useMutation({
    mutationFn: async () => {
      if (!transcript) throw new Error("Transcript is not loaded")
      return (
        await VoiceService.reanalyze({
          path: { session_id: sessionId },
          headers: {
            "Idempotency-Key": `voice-reanalyze-${crypto.randomUUID()}`,
          },
          body: { expected_revision_id: transcript.id },
        })
      ).data
    },
    onSuccess: async () => {
      setMessage("The summary and clinical findings are being updated.")
      await invalidate()
    },
    onError: () =>
      setMessage(
        "The summary and clinical findings could not be updated. Please try again.",
      ),
  })
  const publishMutation = useMutation({
    mutationFn: async () => {
      if (!transcript) throw new Error("Transcript is not loaded")
      return (
        await VoiceService.publish({
          path: { session_id: sessionId },
          body: { expected_revision_id: transcript.id },
        })
      ).data
    },
    onSuccess: async () => {
      setMessage("The reviewed visit note was added to the care record.")
      await invalidate()
    },
    onError: () =>
      setMessage(
        "The reviewed visit note could not be published. Please try again.",
      ),
  })

  const jumpToFact = (fact: ClinicalFactPublic) => {
    if (!transcript) return
    const segment = segmentForFact(fact, transcript.segments)
    // Evidence jumps take precedence over a presentation filter. The target
    // must be rendered before scrolling, including on the mobile tab layout.
    setLowConfidenceOnly(false)
    setMobileTab("transcript")
    const layout = window.matchMedia?.("(min-width: 1024px)").matches
      ? "desktop"
      : "mobile"
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        document
          .getElementById(`voice-segment-${layout}-${segment?.id}`)
          ?.scrollIntoView({ behavior: "smooth", block: "center" })
      })
    })
    seekAudio(fact.audio_start_ms)
  }

  const seekAudio = (startMs: number) => {
    if (!audioRef.current) return
    audioRef.current.currentTime = startMs / 1_000
    void audioRef.current.play().catch(() => undefined)
  }

  const publishDisabled = useMemo(
    () =>
      membershipRole !== "clinician" ||
      !transcript ||
      transcript.stale ||
      transcript.facts.length === 0 ||
      !["ready", "needs_review"].includes(sessionQuery.data?.state ?? ""),
    [membershipRole, sessionQuery.data?.state, transcript],
  )
  const reviewMutationDisabled =
    membershipRole !== "clinician" ||
    !["ready", "needs_review"].includes(sessionQuery.data?.state ?? "")

  if (sessionQuery.isLoading) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  }
  if (sessionQuery.isError || !sessionQuery.data) {
    return (
      <p className="text-critical-muted-foreground">
        This visit recording could not be loaded. Return to the patient record
        and try again.
      </p>
    )
  }
  const session = sessionQuery.data

  return (
    <div className="space-y-5" data-testid="voice-review-mode">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-sm font-semibold uppercase tracking-widest text-primary">
            Visit recording
          </p>
          <h1 className="mt-1 text-3xl font-semibold text-foreground">
            Review visit recording
          </h1>
          <p className="mt-2 max-w-3xl text-sm text-muted-foreground">
            Review speakers, timing, summary, and clinical findings before
            adding this visit to the care record.
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
            <span className="flex min-h-11 items-center gap-2 text-sm text-muted-foreground">
              <AudioLines className="size-4" /> Visit audio is being prepared.
            </span>
          )}
          <Label className="flex min-h-11 items-center gap-2 rounded border px-3 text-sm">
            <input
              type="checkbox"
              checked={lowConfidenceOnly}
              onChange={(event) => setLowConfidenceOnly(event.target.checked)}
            />
            Show uncertain or overlapping speech only
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
                  onSeek={seekAudio}
                  layout="desktop"
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
                  <FileCheck2 className="size-4" /> Clinical findings
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
              <TabsTrigger value="facts">Clinical findings</TabsTrigger>
            </TabsList>
            <TabsContent value="transcript">
              <TranscriptPanel
                transcript={transcript}
                lowConfidenceOnly={lowConfidenceOnly}
                onSeek={seekAudio}
                layout="mobile"
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
                    disabled={
                      reviewMutationDisabled || correctMutation.isPending
                    }
                  >
                    <Save className="mr-2 size-4" />
                    {editing ? "Save correction" : "Correct transcript"}
                  </Button>
                )}
                {membershipRole === "clinician" && (
                  <Button
                    variant="outline"
                    className="min-h-11"
                    onClick={() => reanalyzeMutation.mutate()}
                    disabled={
                      reviewMutationDisabled || reanalyzeMutation.isPending
                    }
                  >
                    <RefreshCw className="mr-2 size-4" /> Update summary and
                    findings
                  </Button>
                )}
                <Button
                  className="min-h-11"
                  onClick={() => publishMutation.mutate()}
                  disabled={publishDisabled || publishMutation.isPending}
                >
                  <CheckCircle2 className="mr-2 size-4" /> Publish reviewed note
                </Button>
              </div>
              {transcript.stale && (
                <p className="text-sm font-medium text-critical-muted-foreground">
                  Update the summary and clinical findings before publishing.
                </p>
              )}
            </CardContent>
          </Card>
        </>
      ) : (
        <Card>
          <CardContent className="flex min-h-48 items-center justify-center pt-6 text-muted-foreground">
            {session.error_code ? (
              <span className="flex items-center gap-2">
                <AlertTriangle className="size-5 text-warning" />
                This recording needs attention. The audio remains available for
                review.
              </span>
            ) : (
              <span className="flex items-center gap-2">
                <LoaderCircle className="size-5 animate-spin" /> Preparing the
                visit for review…
              </span>
            )}
          </CardContent>
        </Card>
      )}

      {message && (
        <p className="rounded border border-primary/30 bg-primary/10 p-3 text-sm text-foreground">
          {message}
        </p>
      )}
    </div>
  )
}
