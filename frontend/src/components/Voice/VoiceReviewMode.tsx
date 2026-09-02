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
  JobPublic,
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
import type { MedicationAssertion, MedicationReviewInput } from "@/features/api"
import {
  loadAuthorizedAudio,
  publishReviewedVoice,
  voiceJob,
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
  extracting: "Preparing clinical review",
  delayed: "Provider delayed · manual review available",
  retrying: "Retry scheduled · manual review available",
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

export function voiceJobPollInterval(job: JobPublic | undefined): 2000 | false {
  if (!job) return 2000
  if (
    ["queued", "running", "delayed"].includes(job.visible_state ?? "") ||
    (job.next_run_at !== null && job.attempt_count < job.max_attempts)
  ) {
    return 2000
  }
  return false
}

function segmentSpeakerLabel(segment: TranscriptSegmentPublic): string {
  const speakerIds = (
    segment as TranscriptSegmentPublic & { speaker_ids?: string[] }
  ).speaker_ids
  if (!speakerIds || speakerIds.length < 2)
    return speakerLabel(segment.speaker_id)
  return speakerIds.map((speakerId) => speakerLabel(speakerId)).join(" + ")
}

const supportedLanguageLabel: Record<string, string> = {
  en: "English",
  ms: "Malay",
  nan: "Hokkien / Southern Min",
  zh: "Chinese",
  cmn: "Mandarin Chinese",
}

export function sourceLanguageLabel(code: string | null): string | null {
  if (!code) return null
  return supportedLanguageLabel[code]
    ? `${supportedLanguageLabel[code]} (${code})`
    : `${code} · review required`
}

type AddressableLanguageSpan = {
  start_offset: number
  end_offset: number
  language_code: "en" | "ms" | "nan" | "zh" | "cmn" | "und"
  confidence: number | null
  detection_source: string
  review_required: boolean
}

function segmentLanguageSpans(
  segment: TranscriptSegmentPublic,
): AddressableLanguageSpan[] {
  return (
    (
      segment as TranscriptSegmentPublic & {
        language_spans?: AddressableLanguageSpan[]
      }
    ).language_spans ?? []
  )
}

function sourceLanguage(segment: TranscriptSegmentPublic): string | null {
  const code =
    (segment as TranscriptSegmentPublic & { source_language?: string })
      .source_language ?? segment.detected_language
  return sourceLanguageLabel(code)
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
          Rule-derived fallback · review required
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
            <Badge variant="outline">{segmentSpeakerLabel(segment)}</Badge>
            <button
              type="button"
              className="min-h-11 rounded px-2 font-mono text-primary"
              aria-label={`Jump to ${time(segment.start_ms)}`}
              onClick={() => onSeek(segment.start_ms)}
            >
              {time(segment.start_ms)}–{time(segment.end_ms)}
            </button>
            {sourceLanguage(segment) && (
              <Badge variant="outline">{sourceLanguage(segment)}</Badge>
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
          {segmentLanguageSpans(segment).length > 0 && (
            <ul
              aria-label={`Addressable language spans for segment ${segment.ordinal + 1}`}
              className="mt-2 space-y-1 text-xs text-muted-foreground"
            >
              {segmentLanguageSpans(segment).map((span) => (
                <li
                  className="rounded border border-dashed px-2 py-1"
                  key={`${span.start_offset}-${span.end_offset}-${span.language_code}`}
                >
                  {sourceLanguageLabel(span.language_code)} · characters{" "}
                  {span.start_offset}–{span.end_offset} ·{" "}
                  {span.detection_source.replace(/_/g, " ")}
                  {span.confidence === null
                    ? " · confidence unavailable"
                    : ` · ${Math.round(span.confidence * 100)}% confidence`}
                  {span.review_required ? " · review required" : ""}
                </li>
              ))}
            </ul>
          )}
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
  const warningLabels: Record<string, string> = {
    CLIPPING_REVIEW: "Clipped audio may have hidden words.",
    CLINICAL_LANGUAGE_OR_CONCEPT_REVIEW_REQUIRED:
      "A clinical term or language span needs manual review.",
    CONFIDENCE_UNAVAILABLE: "Current calibrated confidence is unavailable.",
    DOWNSTREAM_RESULTS_STALE:
      "Derived results are stale after a transcript edit.",
    INVALID_FACT_EVIDENCE:
      "A proposed fact is not fully supported by its source span.",
    LOW_CONFIDENCE_REVIEW:
      "Transcription confidence is below the qualified threshold.",
    LOW_SIGNAL_REVIEW:
      "The recording signal is too quiet for reliable interpretation.",
    MULTI_DEVICE_OVERLAP_REVIEW:
      "Overlapping capture sources need manual review.",
    NOISE_REVIEW:
      "Low signal-to-noise audio was denoised and still needs review.",
    NO_STRUCTURED_FACTS: "No publishable structured facts were extracted.",
    OVERLAP_REVIEW: "Overlapping speakers need manual attribution.",
    SILENCE_REVIEW: "The recording contains substantial silence.",
    TRANSCRIPT_PENDING: "The transcript is still pending.",
  }
  return (
    <div className="space-y-3" data-testid="summary-panel">
      <p className="leading-7 text-foreground">
        {transcript.summary ?? "The clinical summary is being updated."}
      </p>
      {transcript.warning_codes.length > 0 && (
        <div className="space-y-2">
          {[...new Set(transcript.warning_codes)].map((warning) => (
            <div
              className="flex gap-2 rounded bg-review-required-muted p-2 text-xs text-review-required-muted-foreground"
              key={warning}
            >
              <AlertTriangle className="size-4 shrink-0" />
              <span>
                {warningLabels[warning] ??
                  `Review required (${warning.toLowerCase().replace(/_/g, " ")}).`}
              </span>
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

export function voiceMedicationAssertions(
  facts: ClinicalFactPublic[],
): MedicationAssertion[] {
  return facts.flatMap((fact) => {
    if (
      fact.fact_type !== "medication" ||
      !fact.medication ||
      fact.dose_value === null ||
      fact.dose_value === undefined ||
      !fact.dose_unit ||
      !fact.route ||
      !fact.frequency
    )
      return []
    return [
      {
        assertion_id: fact.id,
        medication: fact.medication,
        dose_value: fact.dose_value,
        dose_unit: fact.dose_unit,
        route: fact.route,
        frequency: fact.frequency,
      },
    ]
  })
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
  const [confirmedMedicationIds, setConfirmedMedicationIds] = useState(
    new Set<string>(),
  )
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
  const jobQuery = useQuery({
    queryKey: ["voice-session", sessionId, "job"],
    queryFn: () => voiceJob(sessionId),
    enabled: !["created", "recording"].includes(
      sessionQuery.data?.state ?? "created",
    ),
    refetchInterval: (query) => voiceJobPollInterval(query.state.data),
    retry: false,
  })
  const transcript = transcriptQuery.data
  const medicationAssertions = transcript
    ? voiceMedicationAssertions(transcript.facts)
    : []
  const hasIncompleteMedicationAssertion = Boolean(
    transcript?.facts.some((fact) => fact.fact_type === "medication") &&
      medicationAssertions.length !==
        transcript?.facts.filter((fact) => fact.fact_type === "medication")
          .length,
  )
  const medicationReviews: MedicationReviewInput[] = medicationAssertions
    .filter((assertion) => confirmedMedicationIds.has(assertion.assertion_id))
    .map((assertion) => ({ ...assertion, confirmed: true as const }))
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
      return publishReviewedVoice(sessionId, transcript.id, medicationReviews)
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
      medicationReviews.length !== medicationAssertions.length ||
      hasIncompleteMedicationAssertion ||
      !["ready", "needs_review"].includes(sessionQuery.data?.state ?? ""),
    [
      membershipRole,
      medicationAssertions.length,
      medicationReviews.length,
      hasIncompleteMedicationAssertion,
      sessionQuery.data?.state,
      transcript,
    ],
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
  const reliableJob = jobQuery.data
  const ruleFallback = reliableJob?.ai_run?.status === "fallback"

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

      {(reliableJob?.delayed_at ||
        reliableJob?.next_run_at ||
        reliableJob?.provider_outage ||
        reliableJob?.timed_out_at ||
        ruleFallback) && (
        <div className="rounded-xl border border-warning/40 bg-warning-muted p-4 text-sm leading-6 text-warning-muted-foreground">
          <p className="font-semibold">Remote processing is delayed</p>
          <p>
            {reliableJob?.error_class
              ? `Classified as ${reliableJob.error_class.replace(/_/g, " ")}. `
              : ""}
            The recording and manual clinical workflow remain available. A
            delayed provider never blocks manual documentation.
          </p>
          {reliableJob?.next_run_at && (
            <p>
              Retry attempt {reliableJob.attempt_count + 1} of{" "}
              {reliableJob.max_attempts} is scheduled for{" "}
              {new Date(reliableJob.next_run_at).toLocaleString()}.
            </p>
          )}
          {reliableJob?.timed_out_at && (
            <p>
              The bounded request timed out at{" "}
              {new Date(reliableJob.timed_out_at).toLocaleString()}.
            </p>
          )}
          {reliableJob?.provider_outage && (
            <p>
              Provider outage recorded. Stored results remain available with
              their age; new output must pass independent review.
            </p>
          )}
          {ruleFallback && (
            <p>
              Rule-derived review suggestion
              {reliableJob?.ai_run?.fallback_reason
                ? ` (${reliableJob.ai_run.fallback_reason.replace(/_/g, " ")})`
                : ""}
              . It is never auto-published.
            </p>
          )}
          {(reliableJob?.retry_history?.length ?? 0) > 0 && (
            <details className="mt-2">
              <summary className="cursor-pointer font-medium">
                {reliableJob?.retry_history?.length} recorded attempt
                {reliableJob?.retry_history?.length === 1 ? "" : "s"}
              </summary>
              <ol className="mt-1 list-decimal pl-5 text-xs">
                {reliableJob?.retry_history?.map((attempt, index) => (
                  <li
                    key={`${String(attempt.attempted_at ?? attempt.attempt ?? index)}`}
                  >
                    {String(attempt.error_class ?? attempt.status ?? "attempt")}
                    {attempt.next_retry_at
                      ? ` · retry ${new Date(String(attempt.next_retry_at)).toLocaleString()}`
                      : ""}
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
      )}

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
              {medicationAssertions.length > 0 ? (
                <fieldset className="space-y-2 rounded-xl border border-warning/40 bg-warning-muted/20 p-3">
                  <legend className="px-1 text-sm font-semibold">
                    Medication safety review
                  </legend>
                  {medicationAssertions.map((assertion) => (
                    <label
                      className="flex items-start gap-3 rounded-lg border bg-card p-3 text-sm"
                      key={assertion.assertion_id}
                    >
                      <input
                        checked={confirmedMedicationIds.has(
                          assertion.assertion_id,
                        )}
                        className="mt-1 size-4"
                        onChange={(event) => {
                          const next = new Set(confirmedMedicationIds)
                          if (event.target.checked)
                            next.add(assertion.assertion_id)
                          else next.delete(assertion.assertion_id)
                          setConfirmedMedicationIds(next)
                        }}
                        type="checkbox"
                      />
                      <span>
                        <strong>{assertion.medication}</strong>
                        <span className="mt-1 block leading-6 text-muted-foreground">
                          {assertion.dose_value} {assertion.dose_unit} ·{" "}
                          {assertion.route} · {assertion.frequency}
                        </span>
                        <span className="mt-1 block text-xs text-warning-muted-foreground">
                          Confirm medication, dose, unit, route, and frequency
                          against the source audio and transcript.
                        </span>
                      </span>
                    </label>
                  ))}
                </fieldset>
              ) : hasIncompleteMedicationAssertion ? (
                <p className="rounded-xl border border-critical/40 bg-critical-muted p-3 text-sm text-critical-muted-foreground">
                  A medication assertion is missing dose, unit, route, or
                  frequency. Publishing remains blocked until structured review
                  data is complete.
                </p>
              ) : (
                <p className="rounded-xl border bg-muted/30 p-3 text-sm text-muted-foreground">
                  No structured medication assertions were reported for this
                  transcript; an empty medication review is valid.
                </p>
              )}
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
              {medicationReviews.length !== medicationAssertions.length && (
                <p className="text-sm font-medium text-critical-muted-foreground">
                  Confirm every structured medication field before publishing.
                </p>
              )}
              {hasIncompleteMedicationAssertion && (
                <p className="text-sm font-medium text-critical-muted-foreground">
                  Incomplete structured medication review data blocks
                  publication.
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
