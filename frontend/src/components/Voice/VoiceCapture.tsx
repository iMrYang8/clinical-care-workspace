import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Link2,
  LoaderCircle,
  Mic,
  Radio,
  Square,
  UploadCloud,
  WifiOff,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import type { MePublic, VoiceSessionCreate, VoiceSessionPublic } from "@/client"
import { VoiceService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { clinicalApi, type ProvisionalSafetyAlert } from "@/features/api"
import {
  recordingCodeFromSessionId,
  sessionIdFromRecordingCode,
} from "@/features/routeReferences"
import {
  connectLiveTranscript,
  type LiveCaptionStatus,
  type LiveTranscriptControl,
} from "@/features/voice/liveTranscript"
import {
  createLocalCapture,
  enqueueEncryptedChunk,
  recoverableCaptures,
  type VoiceCaptureOwner,
} from "@/features/voice/offlineQueue"
import {
  analyzeInputLevel,
  preferredRecorderMimeType,
  VOICE_CHUNK_INTERVAL_MS,
} from "@/features/voice/recorder"
import {
  abandonEmptyCapture,
  finalizeCapture,
  uploadPendingChunks,
} from "@/features/voice/voiceApi"

export {
  recordingCodeFromSessionId,
  sessionIdFromRecordingCode,
} from "@/features/routeReferences"

type CaptureKind = "patient" | "clinical"

type VoiceCaptureProps = {
  patientId: string
  captureKind: CaptureKind
  role: MePublic["role"]
  owner: VoiceCaptureOwner
  onFinalized?: (sessionId: string) => void
}

export const LIVE_SAFETY_ALERT_POLL_MS = 2_000

export function ProvisionalSafetyAlertPanel({
  alerts,
  viewerRole,
  busyAlertId,
  onReview,
}: {
  alerts: ProvisionalSafetyAlert[]
  viewerRole: MePublic["role"]
  busyAlertId?: string
  onReview: (
    alert: ProvisionalSafetyAlert,
    action: "confirm" | "dismiss",
  ) => void | Promise<void>
}) {
  const pending = alerts.filter((alert) => alert.state === "pending")
  if (pending.length === 0) return null
  return (
    <section
      aria-labelledby="live-allergy-alerts"
      className="space-y-3 rounded-xl border-2 border-critical/50 bg-critical-muted/30 p-4"
    >
      <div>
        <h2
          className="flex items-center gap-2 font-serif text-lg font-semibold text-critical-muted-foreground"
          id="live-allergy-alerts"
        >
          <AlertTriangle /> Provisional live allergy alerts
        </h2>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          Detected from a completed live segment. This remains a provisional
          alert until a clinician confirms it; uncertain or unsupported language
          never becomes “no allergy.”
        </p>
      </div>
      {pending.map((alert) => (
        <article
          className="space-y-2 rounded-lg border bg-card p-3"
          data-testid={`live-safety-alert-${alert.id}`}
          key={alert.id}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p className="font-semibold">
                {alert.concept_code.replace(/^allergy:/, "") ||
                  "Possible allergy statement"}
              </p>
              <p className="mt-1 text-sm leading-6 text-muted-foreground">
                Completed segment {alert.source_event_id} · source span{" "}
                {alert.source_start_offset}–{alert.source_end_offset}
              </p>
            </div>
            <div className="flex flex-wrap gap-1">
              <Badge variant="destructive">{alert.severity}</Badge>
              <Badge variant="outline">{alert.source_language}</Badge>
              <Badge variant="outline">
                {alert.polarity} · {alert.assertion_scope.replace(/_/g, " ")}
              </Badge>
            </div>
          </div>
          {viewerRole === "clinician" ? (
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={busyAlertId === alert.id}
                onClick={() => onReview(alert, "confirm")}
                size="sm"
              >
                <CheckCircle2 /> Confirm clinical fact
              </Button>
              <Button
                disabled={busyAlertId === alert.id}
                onClick={() => onReview(alert, "dismiss")}
                size="sm"
                variant="outline"
              >
                Dismiss provisional alert
              </Button>
            </div>
          ) : (
            <p className="text-sm font-medium text-critical-muted-foreground">
              A clinician must confirm or dismiss this alert.
            </p>
          )}
        </article>
      ))}
    </section>
  )
}

function browserDeviceId(): string {
  const key = "nightingale_voice_browser_device_id"
  const existing = localStorage.getItem(key)
  if (existing) return existing
  const created = crypto.randomUUID()
  localStorage.setItem(key, created)
  return created
}

function stopMediaStream(stream: MediaStream): void {
  stream.getTracks().forEach((track) => {
    track.stop()
  })
}

export function VoiceCapture({
  patientId,
  captureKind,
  role,
  owner,
  onFinalized,
}: VoiceCaptureProps) {
  const { userId, membershipId, clinicId } = owner
  const [state, setState] = useState<
    "idle" | "requesting" | "recording" | "uploading" | "finalizing" | "queued"
  >("idle")
  const [existingSessionId, setExistingSessionId] = useState("")
  const [activeSessionId, setActiveSessionId] = useState<string>()
  const [remoteAudioConsent, setRemoteAudioConsent] = useState(false)
  const [sessionAudioConsent, setSessionAudioConsent] =
    useState<VoiceSessionPublic>()
  const [captureId, setCaptureId] = useState<string>()
  const [permission, setPermission] = useState<
    "unknown" | "granted" | "denied"
  >("unknown")
  const [inputLabel, setInputLabel] = useState("Default microphone")
  const [inputLevel, setInputLevel] = useState(0)
  const [clipping, setClipping] = useState(false)
  const [noise, setNoise] = useState(false)
  const [offline, setOffline] = useState(!navigator.onLine)
  const [capturedChunks, setCapturedChunks] = useState(0)
  const [uploadedChunks, setUploadedChunks] = useState(0)
  const [recoverable, setRecoverable] = useState<
    Array<{ id: string; empty: boolean }>
  >([])
  const [message, setMessage] = useState<string>()
  const [liveStatus, setLiveStatus] = useState<LiveCaptionStatus>("not_started")
  const [provisionalTranscript, setProvisionalTranscript] = useState("")
  const [safetyAlerts, setSafetyAlerts] = useState<ProvisionalSafetyAlert[]>([])
  const [busyAlertId, setBusyAlertId] = useState<string>()
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyzerFrameRef = useRef<number | null>(null)
  const chunkStartedAtRef = useRef(0)
  const writeChainRef = useRef(Promise.resolve())
  const writeErrorRef = useRef<Error | null>(null)
  const uploadChainRef = useRef<Promise<void>>(Promise.resolve())
  const mountedRef = useRef(true)
  const startGenerationRef = useRef(0)
  const liveTranscriptRef = useRef<LiveTranscriptControl | null>(null)

  const refreshRecovery = useCallback(
    async (excludeCaptureId?: string) => {
      const captures = await recoverableCaptures({
        userId,
        membershipId,
        clinicId,
      })
      if (!mountedRef.current) return
      setRecoverable(
        captures
          .filter(
            (capture) =>
              capture.patientId === patientId &&
              capture.id !== excludeCaptureId,
          )
          .map((capture) => ({
            id: capture.id,
            empty: capture.nextChunkIndex === 0,
          })),
      )
    },
    [clinicId, membershipId, patientId, userId],
  )

  const flush = useCallback(
    async (localId: string) => {
      const activelyRecording = recorderRef.current?.state === "recording"
      if (!navigator.onLine) {
        if (mountedRef.current) {
          if (!activelyRecording) setState("queued")
          setMessage(
            "Offline: this recording remains securely saved on this device.",
          )
        }
        return false
      }
      if (!activelyRecording && mountedRef.current) setState("uploading")
      try {
        const result = await uploadPendingChunks(localId)
        if (mountedRef.current) {
          setUploadedChunks((count) => count + result.uploaded)
          if (result.remaining === 0)
            setMessage("Recording data is securely uploaded.")
        }
        // A periodically acknowledged chunk is not a stopped capture. Keep the
        // active recorder out of the recovery/finalization path until Stop has
        // fired and every dataavailable callback is durably persisted.
        await refreshRecovery(activelyRecording ? localId : undefined)
        return result.remaining === 0
      } catch {
        if (mountedRef.current) {
          if (!activelyRecording) setState("queued")
          setMessage(
            "Upload paused. This recording remains securely saved on this device.",
          )
        }
        return false
      }
    },
    [refreshRecovery],
  )

  const scheduleFlush = useCallback(
    (localId: string): Promise<boolean> => {
      const scheduled = uploadChainRef.current.then(() => flush(localId))
      uploadChainRef.current = scheduled.then(
        () => undefined,
        () => undefined,
      )
      return scheduled
    },
    [flush],
  )

  const releaseCaptureHardware = useCallback(async () => {
    if (streamRef.current) stopMediaStream(streamRef.current)
    streamRef.current = null
    if (analyzerFrameRef.current !== null) {
      cancelAnimationFrame(analyzerFrameRef.current)
      analyzerFrameRef.current = null
    }
    const context = audioContextRef.current
    audioContextRef.current = null
    if (context) await context.close().catch(() => undefined)
  }, [])

  const closeLiveTranscript = useCallback(async () => {
    const live = liveTranscriptRef.current
    liveTranscriptRef.current = null
    await live?.close()
  }, [])

  const failLocalWrite = useCallback(
    (error: unknown) => {
      if (writeErrorRef.current) return
      const failure =
        error instanceof Error
          ? error
          : new Error("Encrypted local storage rejected an audio chunk")
      writeErrorRef.current = failure
      const recorder = recorderRef.current
      if (recorder?.state === "recording") {
        try {
          recorder.stop()
        } catch {
          // Hardware cleanup and the durable-error UI below remain authoritative.
        }
      }
      void closeLiveTranscript()
      void releaseCaptureHardware()
      if (!mountedRef.current) return
      setState("queued")
      setMessage(
        "Secure local storage failed, so recording stopped. Previously saved recording data can still be recovered.",
      )
      void refreshRecovery()
    },
    [closeLiveTranscript, refreshRecovery, releaseCaptureHardware],
  )

  useEffect(() => {
    void refreshRecovery()
    const online = () => {
      setOffline(false)
      if (captureId) void scheduleFlush(captureId)
    }
    const offlineEvent = () => setOffline(true)
    window.addEventListener("online", online)
    window.addEventListener("offline", offlineEvent)
    return () => {
      window.removeEventListener("online", online)
      window.removeEventListener("offline", offlineEvent)
    }
  }, [captureId, refreshRecovery, scheduleFlush])

  const refreshSafetyAlerts = useCallback(async (sessionId: string) => {
    try {
      const alerts = await clinicalApi.liveSafetyAlerts(sessionId)
      if (mountedRef.current) setSafetyAlerts(alerts)
    } catch {
      // Live safety detection is an additional review channel. Recording and
      // the durable post-visit analysis continue if this poll is interrupted.
    }
  }, [])

  useEffect(() => {
    if (!activeSessionId || (state !== "recording" && state !== "finalizing"))
      return
    void refreshSafetyAlerts(activeSessionId)
    const timer = window.setInterval(
      () => void refreshSafetyAlerts(activeSessionId),
      LIVE_SAFETY_ALERT_POLL_MS,
    )
    return () => window.clearInterval(timer)
  }, [activeSessionId, refreshSafetyAlerts, state])

  const reviewSafetyAlert = async (
    alert: ProvisionalSafetyAlert,
    action: "confirm" | "dismiss",
  ) => {
    if (role !== "clinician") return
    setBusyAlertId(alert.id)
    try {
      if (action === "confirm")
        await clinicalApi.confirmLiveSafetyAlert(alert.id)
      else await clinicalApi.dismissLiveSafetyAlert(alert.id)
      if (activeSessionId) await refreshSafetyAlerts(activeSessionId)
      setMessage(
        action === "confirm"
          ? "Allergy alert confirmed as a clinical fact."
          : "Provisional allergy alert dismissed with its source retained.",
      )
    } catch {
      setMessage("The provisional allergy alert could not be reviewed.")
    } finally {
      setBusyAlertId(undefined)
    }
  }

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      startGenerationRef.current += 1
      const recorder = recorderRef.current
      recorderRef.current = null
      if (recorder?.state === "recording") {
        try {
          recorder.stop()
        } catch {
          // The stream is still stopped synchronously below.
        }
      }
      void closeLiveTranscript()
      void releaseCaptureHardware()
    }
  }, [closeLiveTranscript, releaseCaptureHardware])

  const startLevelMeter = (stream: MediaStream) => {
    const context = new AudioContext()
    const analyser = context.createAnalyser()
    analyser.fftSize = 512
    context.createMediaStreamSource(stream).connect(analyser)
    audioContextRef.current = context
    const samples = new Uint8Array(analyser.fftSize)
    const sample = () => {
      if (!mountedRef.current) return
      analyser.getByteTimeDomainData(samples)
      const result = analyzeInputLevel(samples)
      setInputLevel(result.level)
      setClipping(result.clipping)
      setNoise(result.noise)
      analyzerFrameRef.current = requestAnimationFrame(sample)
    }
    sample()
  }

  const start = async () => {
    const startGeneration = startGenerationRef.current + 1
    startGenerationRef.current = startGeneration
    const isStale = () =>
      !mountedRef.current || startGenerationRef.current !== startGeneration
    setState("requesting")
    setMessage(undefined)
    setLiveStatus("not_started")
    setProvisionalTranscript("")
    setSessionAudioConsent(undefined)
    let joinedSessionId: string | undefined
    let joinedDeviceId: string | undefined
    let localCapturePersisted = false
    try {
      const requestedSessionId = sessionIdFromRecordingCode(existingSessionId)
      let serverSessionId: string | undefined
      if (requestedSessionId) {
        const existing = (
          await VoiceService.sessionStatus({
            path: { session_id: requestedSessionId },
          })
        ).data
        if (isStale()) return
        if (
          existing.patient_id !== patientId ||
          existing.capture_kind !== captureKind
        ) {
          throw new Error(
            "The joined session belongs to a different patient or capture context.",
          )
        }
        setSessionAudioConsent(existing)
        serverSessionId = existing.id
      }
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      if (isStale()) {
        stopMediaStream(stream)
        return
      }
      streamRef.current = stream
      setPermission("granted")
      const track = stream.getAudioTracks()[0]
      setInputLabel(track?.label || "Microphone permission granted")
      if (!serverSessionId) {
        const body: VoiceSessionCreate = {
          patient_id: patientId,
          capture_kind: captureKind,
          synthetic_fixture: false,
          fixture_id: null,
          remote_audio_consent: remoteAudioConsent,
        }
        const created = (
          await VoiceService.createSession({
            body,
          })
        ).data
        serverSessionId = created.id
        setSessionAudioConsent(created)
        if (isStale()) {
          await releaseCaptureHardware()
          return
        }
      }
      const joined = (
        await VoiceService.joinDevice({
          path: { session_id: serverSessionId },
          body: {
            client_device_id: browserDeviceId(),
            capture_role:
              role === "patient" || role === "staff" || role === "clinician"
                ? role
                : "staff",
            expected_patient_id: patientId,
            expected_capture_kind: captureKind,
          },
        })
      ).data
      joinedSessionId = serverSessionId
      joinedDeviceId = joined.id
      setActiveSessionId(serverSessionId)
      if (isStale()) {
        await releaseCaptureHardware()
        await VoiceService.abandonDevice({
          path: {
            session_id: joinedSessionId,
            device_id: joinedDeviceId,
          },
        }).catch(() => undefined)
        return
      }
      const mediaType = preferredRecorderMimeType()
      const recorder = mediaType
        ? new MediaRecorder(stream, { mimeType: mediaType })
        : new MediaRecorder(stream)
      recorderRef.current = recorder
      const local = await createLocalCapture({
        serverSessionId,
        serverDeviceId: joined.id,
        patientId,
        userId,
        membershipId,
        clinicId,
        mediaType: recorder.mimeType || mediaType || "audio/webm",
      })
      localCapturePersisted = true
      if (isStale()) {
        await releaseCaptureHardware()
        recorderRef.current = null
        // The local helper removes both the zero-chunk IndexedDB row and the
        // joined server track. If the network is down, it intentionally keeps
        // the row so the next mounted capture screen can recover it.
        await abandonEmptyCapture(local.id).catch(() => undefined)
        return
      }
      setCaptureId(local.id)
      chunkStartedAtRef.current = 0
      writeErrorRef.current = null
      writeChainRef.current = Promise.resolve()
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size === 0) return
        const startMs = chunkStartedAtRef.current
        const endMs = startMs + VOICE_CHUNK_INTERVAL_MS
        chunkStartedAtRef.current = endMs
        writeChainRef.current = writeChainRef.current
          .then(async () => {
            if (writeErrorRef.current) return
            await enqueueEncryptedChunk(local.id, event.data, startMs, endMs)
            if (mountedRef.current) setCapturedChunks((count) => count + 1)
            if (navigator.onLine) void scheduleFlush(local.id)
          })
          .catch(failLocalWrite)
      })
      recorder.start(VOICE_CHUNK_INTERVAL_MS)
      startLevelMeter(stream)
      setState("recording")
      void connectLiveTranscript({
        sessionId: serverSessionId,
        stream,
        onStatus: (event) => {
          if (isStale()) return
          setLiveStatus(event.status)
        },
        onDelta: (text) => {
          if (!isStale()) setProvisionalTranscript((current) => current + text)
        },
        onCompleted: (text) => {
          if (!isStale()) {
            setProvisionalTranscript(text)
            void refreshSafetyAlerts(serverSessionId)
          }
        },
      }).then((control) => {
        if (isStale()) {
          void control?.close()
          return
        }
        liveTranscriptRef.current = control ?? null
      })
    } catch (error) {
      // Release the microphone synchronously before any compensating network
      // request. Cleanup may be slow or offline, but capture hardware must not
      // remain live while it is attempted.
      await releaseCaptureHardware()
      await closeLiveTranscript()
      recorderRef.current = null
      setActiveSessionId(undefined)
      // Joining creates a server-side track that participates in the
      // multi-device seal barrier. If recorder construction or the first
      // durable IndexedDB write fails, compensate immediately: without a
      // local recovery row the user would otherwise have no device id with
      // which to remove that empty track after a reload.
      if (joinedSessionId && joinedDeviceId && !localCapturePersisted) {
        await VoiceService.abandonDevice({
          path: {
            session_id: joinedSessionId,
            device_id: joinedDeviceId,
          },
        }).catch(() => undefined)
      }
      if (isStale()) return
      if (error instanceof DOMException && error.name === "NotAllowedError") {
        setPermission("denied")
      }
      setState("idle")
      setMessage(
        error instanceof DOMException && error.name === "NotAllowedError"
          ? "Microphone access was not granted. Allow access and try again."
          : "Recording could not start. Check the recording code and connection, then try again.",
      )
      await refreshRecovery()
    }
  }

  const stop = async () => {
    const recorder = recorderRef.current
    if (!recorder || !captureId) return
    if (mountedRef.current) {
      setState("finalizing")
      // Provisional text is intentionally ephemeral. Stop displaying it as
      // soon as the durable finalize path takes ownership of the recording.
      setLiveStatus("not_started")
      setProvisionalTranscript("")
    }
    try {
      if (recorder.state === "recording") {
        await new Promise<void>((resolve) => {
          recorder.addEventListener("stop", () => resolve(), { once: true })
          recorder.stop()
        })
      }
      const live = liveTranscriptRef.current
      liveTranscriptRef.current = null
      await live?.commit()
      if (activeSessionId) await refreshSafetyAlerts(activeSessionId)
      // commit() waits for the post-commit completed turn (or its bounded
      // timeout). Only then invalidate the capture callbacks; otherwise the
      // final completed segment and its safety alert disappear at Stop.
      startGenerationRef.current += 1
      await releaseCaptureHardware()
      await writeChainRef.current
      if (writeErrorRef.current) throw writeErrorRef.current
      const uploaded = await scheduleFlush(captureId)
      if (!uploaded) return
      const result = await finalizeCapture(captureId)
      if (mountedRef.current) {
        setCaptureId(undefined)
        setActiveSessionId(undefined)
        setState("idle")
        setMessage(
          "Recording accepted and is being prepared for clinical review. Live captions remain temporary until review is complete.",
        )
      }
      await refreshRecovery()
      if (mountedRef.current) onFinalized?.(result.session_id)
    } catch {
      if (mountedRef.current) {
        setState("queued")
        const storageFailure = writeErrorRef.current
        setMessage(
          storageFailure
            ? "Secure local storage failed, so recording stopped. Previously saved recording data can still be recovered."
            : "Review preparation paused. This recording remains securely saved and can be retried.",
        )
      }
      await refreshRecovery()
    } finally {
      await closeLiveTranscript()
      await releaseCaptureHardware()
      recorderRef.current = null
    }
  }

  const resume = async (localId: string) => {
    if (recorderRef.current?.state === "recording") {
      setMessage("Stop the active recording before recovering another upload.")
      return
    }
    try {
      setCaptureId(localId)
      const complete = await scheduleFlush(localId)
      if (complete) {
        const result = await finalizeCapture(localId)
        if (mountedRef.current) {
          setCaptureId(undefined)
          setActiveSessionId(undefined)
          onFinalized?.(result.session_id)
          setMessage("Recording uploaded and sent for clinical review.")
          setState("idle")
        }
        await refreshRecovery()
      }
    } catch {
      if (mountedRef.current) {
        setState("queued")
        setMessage(
          "Upload recovery paused. The recording remains securely saved.",
        )
      }
    }
  }

  const abandon = async (localId: string) => {
    if (recorderRef.current?.state === "recording") {
      setMessage("Stop the active recording before abandoning another device.")
      return
    }
    setState("uploading")
    try {
      await abandonEmptyCapture(localId)
      if (mountedRef.current) {
        setState("idle")
        setMessage(
          "Empty recording removed. Other participants can now finish.",
        )
      }
      await refreshRecovery()
    } catch {
      if (mountedRef.current) {
        setState("queued")
        setMessage("Empty recording removal paused. Please try again.")
      }
    }
  }

  const busy = ["requesting", "uploading", "finalizing"].includes(state)

  return (
    <div className="space-y-4" data-testid="voice-capture">
      <Card className="border-primary/20 bg-card shadow-sm">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Mic className="text-primary" />
              <h1>Record visit</h1>
            </CardTitle>
            <div className="flex gap-2">
              <Badge variant="outline">
                {captureKind === "clinical"
                  ? "Clinical recording"
                  : "Patient update"}
              </Badge>
              {offline ? (
                <Badge className="bg-warning-muted text-warning-muted-foreground">
                  <WifiOff className="mr-1 size-3" /> Offline
                </Badge>
              ) : (
                <Badge className="bg-success-muted text-success-muted-foreground">
                  <UploadCloud className="mr-1 size-3" /> Online
                </Badge>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="max-w-xl space-y-2">
            <div className="space-y-2">
              <Label htmlFor="existing-session">
                Join an existing visit recording (optional)
              </Label>
              <Input
                id="existing-session"
                value={existingSessionId}
                onChange={(event) => {
                  const value = event.target.value
                  setExistingSessionId(value)
                  if (value.trim()) setRemoteAudioConsent(false)
                }}
                placeholder="Enter the recording code"
                disabled={state !== "idle"}
              />
              <p className="text-xs text-muted-foreground">
                <Link2 className="mr-1 inline size-3" /> Use the code shared by
                the first device to record the same visit together.
              </p>
            </div>
          </div>

          <section
            aria-labelledby="remote-audio-egress-heading"
            className="space-y-3 rounded-lg border border-ai/30 bg-ai-muted/20 p-4"
          >
            <div className="flex items-start gap-3">
              <Checkbox
                aria-describedby="remote-audio-egress-description"
                checked={remoteAudioConsent}
                disabled={state !== "idle" || Boolean(existingSessionId.trim())}
                id="remote-audio-egress-consent"
                onCheckedChange={(checked) =>
                  setRemoteAudioConsent(checked === true)
                }
              />
              <div className="space-y-1">
                <Label
                  className="font-medium leading-5"
                  htmlFor="remote-audio-egress-consent"
                  id="remote-audio-egress-heading"
                >
                  Allow remote audio processing for this recording
                </Label>
                <p
                  className="text-sm leading-6 text-muted-foreground"
                  id="remote-audio-egress-description"
                >
                  Optional PHI-bearing egress. Local ASR remains the default and
                  does not require this consent. Remote audio is used only when
                  this session consent and clinic policy are both enabled.
                </p>
              </div>
            </div>
            <div aria-live="polite" className="flex flex-wrap gap-2">
              {sessionAudioConsent?.remote_audio_consent_recorded ? (
                <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                  Consent recorded · clinic policy still controls remote use
                </Badge>
              ) : existingSessionId.trim() && !sessionAudioConsent ? (
                <Badge variant="outline">
                  Existing session policy will be checked when you join
                </Badge>
              ) : remoteAudioConsent ? (
                <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                  Consent selected · clinic policy is still required
                </Badge>
              ) : (
                <Badge variant="outline">
                  Local ASR only · remote audio consent not recorded
                </Badge>
              )}
              {sessionAudioConsent?.remote_audio_consent_at && (
                <Badge variant="outline">
                  Consent recorded{" "}
                  {new Date(
                    sessionAudioConsent.remote_audio_consent_at,
                  ).toLocaleString()}
                </Badge>
              )}
            </div>
          </section>

          <div className="rounded-lg border bg-muted/40 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="font-medium">{inputLabel}</p>
                <p className="text-xs text-muted-foreground">
                  Microphone access:{" "}
                  {permission === "granted"
                    ? "Ready"
                    : permission === "denied"
                      ? "Blocked"
                      : "Not requested"}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                {clipping && <Badge variant="destructive">Clipping</Badge>}
                {noise && (
                  <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                    Low signal
                  </Badge>
                )}
                {state === "recording" && (
                  <Badge className="bg-critical-muted text-critical-muted-foreground">
                    <Radio className="mr-1 size-3 animate-pulse" /> Recording
                  </Badge>
                )}
                {liveStatus === "available" && (
                  <Badge className="bg-ai-muted text-ai-muted-foreground">
                    Live captions · provisional
                  </Badge>
                )}
                {liveStatus === "unavailable" && (
                  <Badge className="bg-muted text-muted-foreground">
                    Live captions unavailable
                  </Badge>
                )}
                {liveStatus === "needs_review" && (
                  <Badge className="bg-review-required-muted text-review-required-muted-foreground">
                    Live captions interrupted · review
                  </Badge>
                )}
              </div>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded bg-muted">
              <div
                className="h-full bg-primary transition-[width]"
                style={{ width: `${Math.min(100, inputLevel * 220)}%` }}
              />
            </div>
          </div>

          {liveStatus !== "not_started" && (
            <div
              className="rounded-lg border border-ai/40 bg-ai-muted/50 p-4"
              aria-live="polite"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium text-ai-muted-foreground">
                  Live captions
                </p>
                <span className="text-xs text-ai-muted-foreground">
                  Temporary text · review the final recording
                </span>
              </div>
              {provisionalTranscript ? (
                <p className="mt-2 whitespace-pre-wrap text-sm text-foreground">
                  {provisionalTranscript}
                </p>
              ) : (
                <p className="mt-2 text-sm text-muted-foreground">
                  {liveStatus === "connecting"
                    ? "Connecting to the secure caption service…"
                    : liveStatus === "available"
                      ? "Listening for provisional speech text…"
                      : "Recording continues securely without live text."}
                </p>
              )}
            </div>
          )}

          <ProvisionalSafetyAlertPanel
            alerts={safetyAlerts}
            busyAlertId={busyAlertId}
            onReview={reviewSafetyAlert}
            viewerRole={role}
          />

          <div className="flex flex-wrap items-center gap-3">
            {state === "recording" ? (
              <Button
                className="min-h-11 min-w-32"
                onClick={stop}
                variant="destructive"
              >
                <Square className="mr-2 size-4" /> Stop & finalize
              </Button>
            ) : (
              <Button
                className="min-h-11 min-w-32"
                onClick={start}
                disabled={busy}
              >
                {busy ? (
                  <LoaderCircle className="mr-2 size-4 animate-spin" />
                ) : (
                  <Mic className="mr-2 size-4" />
                )}
                Start recording
              </Button>
            )}
            <span className="text-sm text-muted-foreground">
              {state === "recording"
                ? "Recording and uploading securely"
                : capturedChunks > 0 && uploadedChunks >= capturedChunks
                  ? "Recording data uploaded"
                  : "Ready to record"}
            </span>
          </div>

          {activeSessionId && state === "recording" && (
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-card p-3">
              <div>
                <p className="text-sm font-medium text-foreground">
                  Recording code
                </p>
                <p className="font-mono text-sm tracking-wide text-muted-foreground">
                  {recordingCodeFromSessionId(activeSessionId)}
                </p>
              </div>
              <Button
                onClick={() => {
                  void navigator.clipboard
                    .writeText(recordingCodeFromSessionId(activeSessionId))
                    .then(() => setMessage("Recording code copied."))
                    .catch(() =>
                      setMessage("Select the recording code and copy it."),
                    )
                }}
                type="button"
                variant="outline"
              >
                <Copy /> Copy code
              </Button>
            </div>
          )}

          {message && (
            <div className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning-muted p-3 text-sm text-warning-muted-foreground">
              {state === "idle" ? (
                <CheckCircle2 className="mt-0.5 size-4 shrink-0" />
              ) : (
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
              )}
              {message}
            </div>
          )}
        </CardContent>
      </Card>

      {recoverable.length > 0 && state !== "recording" && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              <h2>Recordings waiting to upload</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {recoverable.map((capture) => (
              <div
                className="flex flex-wrap items-center justify-between gap-2 rounded border p-3"
                key={capture.id}
              >
                <span className="text-sm">
                  Interrupted recording
                  {capture.empty && (
                    <span className="ml-2 text-warning-muted-foreground">
                      no audio stored
                    </span>
                  )}
                </span>
                <Button
                  variant="outline"
                  className="min-h-11"
                  onClick={() =>
                    void (capture.empty
                      ? abandon(capture.id)
                      : resume(capture.id))
                  }
                >
                  {capture.empty ? "Abandon empty device" : "Resume upload"}
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
