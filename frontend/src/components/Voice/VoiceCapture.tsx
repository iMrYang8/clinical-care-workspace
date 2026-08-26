import {
  AlertTriangle,
  CheckCircle2,
  Link2,
  LoaderCircle,
  Mic,
  Radio,
  Square,
  UploadCloud,
  WifiOff,
} from "lucide-react"
import { useCallback, useEffect, useRef, useState } from "react"
import type { MePublic } from "@/client"
import { VoiceService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
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

type CaptureKind = "patient" | "clinical"

type VoiceCaptureProps = {
  patientId: string
  captureKind: CaptureKind
  role: MePublic["role"]
  owner: VoiceCaptureOwner
  onFinalized?: (sessionId: string) => void
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
  const [syntheticFixture, setSyntheticFixture] = useState(false)
  const [activeSessionId, setActiveSessionId] = useState<string>()
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
  const [liveReasonCode, setLiveReasonCode] = useState<string>()
  const [liveProvider, setLiveProvider] = useState<string>()
  const [provisionalTranscript, setProvisionalTranscript] = useState("")
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
          setMessage("Offline: encrypted chunks remain on this device.")
        }
        return false
      }
      if (!activelyRecording && mountedRef.current) setState("uploading")
      try {
        const result = await uploadPendingChunks(localId)
        if (mountedRef.current) {
          setUploadedChunks((count) => count + result.uploaded)
          if (result.remaining === 0)
            setMessage("All encrypted queue items acknowledged.")
        }
        // A periodically acknowledged chunk is not a stopped capture. Keep the
        // active recorder out of the recovery/finalization path until Stop has
        // fired and every dataavailable callback is durably persisted.
        await refreshRecovery(activelyRecording ? localId : undefined)
        return result.remaining === 0
      } catch (error) {
        if (mountedRef.current) {
          if (!activelyRecording) setState("queued")
          setMessage(error instanceof Error ? error.message : "Upload paused")
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
        `Local encrypted storage failed; recording stopped. Previously persisted chunks remain recoverable. ${failure.message}`,
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
    setLiveReasonCode(undefined)
    setLiveProvider(undefined)
    setProvisionalTranscript("")
    let joinedSessionId: string | undefined
    let joinedDeviceId: string | undefined
    let localCapturePersisted = false
    try {
      const requestedSessionId = existingSessionId.trim()
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
        serverSessionId = (
          await VoiceService.createSession({
            body: {
              patient_id: patientId,
              capture_kind: captureKind,
              synthetic_fixture: syntheticFixture,
              fixture_id: syntheticFixture ? "code-switch-overlap-v1" : null,
            },
          })
        ).data.id
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
      setActiveSessionId(serverSessionId)
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
          setLiveReasonCode(event.reasonCode)
          setLiveProvider(
            event.provider && event.model
              ? `${event.provider} · ${event.model}`
              : event.provider,
          )
        },
        onDelta: (text) => {
          if (!isStale()) setProvisionalTranscript((current) => current + text)
        },
        onCompleted: (text) => {
          if (!isStale()) setProvisionalTranscript(text)
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
        error instanceof Error ? error.message : "Recording could not start",
      )
      await refreshRecovery()
    }
  }

  const stop = async () => {
    const recorder = recorderRef.current
    if (!recorder || !captureId) return
    startGenerationRef.current += 1
    if (mountedRef.current) {
      setState("finalizing")
      // Provisional text is intentionally ephemeral. Stop displaying it as
      // soon as the durable finalize path takes ownership of the recording.
      setLiveStatus("not_started")
      setLiveReasonCode(undefined)
      setLiveProvider(undefined)
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
      await releaseCaptureHardware()
      await writeChainRef.current
      if (writeErrorRef.current) throw writeErrorRef.current
      const uploaded = await scheduleFlush(captureId)
      if (!uploaded) return
      const result = await finalizeCapture(captureId)
      if (mountedRef.current) {
        setCaptureId(undefined)
        setState("idle")
        setMessage(
          `Recording accepted. Processing is ${result.state}; any live captions were provisional and the final transcript will replace them.`,
        )
      }
      await refreshRecovery()
      if (mountedRef.current) onFinalized?.(result.session_id)
    } catch (error) {
      if (mountedRef.current) {
        setState("queued")
        const storageFailure = writeErrorRef.current
        setMessage(
          storageFailure
            ? `Local encrypted storage failed; recording stopped. Previously persisted chunks remain recoverable. ${storageFailure.message}`
            : error instanceof Error
              ? error.message
              : "Finalization paused",
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
          onFinalized?.(result.session_id)
          setMessage("Recovered queue uploaded and finalized.")
          setState("idle")
        }
        await refreshRecovery()
      }
    } catch (error) {
      if (mountedRef.current) {
        setState("queued")
        setMessage(error instanceof Error ? error.message : "Recovery paused")
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
          "Empty device removed; other joined tracks can now finalize.",
        )
      }
      await refreshRecovery()
    } catch (error) {
      if (mountedRef.current) {
        setState("queued")
        setMessage(
          error instanceof Error
            ? error.message
            : "Empty device removal paused",
        )
      }
    }
  }

  const busy = ["requesting", "uploading", "finalizing"].includes(state)

  return (
    <div className="space-y-4" data-testid="voice-capture">
      <Card className="border-teal-100 shadow-sm">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Mic className="text-teal-700" />
              <h1>Secure voice capture</h1>
            </CardTitle>
            <div className="flex gap-2">
              <Badge variant="outline">{captureKind}</Badge>
              {offline ? (
                <Badge className="bg-amber-100 text-amber-900">
                  <WifiOff className="mr-1 size-3" /> Offline
                </Badge>
              ) : (
                <Badge className="bg-emerald-100 text-emerald-900">
                  <UploadCloud className="mr-1 size-3" /> Online
                </Badge>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="grid gap-4 md:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="existing-session">
                Join a second device (optional)
              </Label>
              <Input
                id="existing-session"
                value={existingSessionId}
                onChange={(event) => setExistingSessionId(event.target.value)}
                placeholder="Paste voice session ID"
                disabled={state !== "idle"}
              />
              <p className="text-xs text-slate-500">
                <Link2 className="mr-1 inline size-3" /> Both devices upload
                separate tracks. Alignment is track-start only and overlap
                remains reviewable.
              </p>
            </div>
            <div className="space-y-2">
              <Label className="flex min-h-11 items-center gap-3 rounded-md border px-3">
                <input
                  type="checkbox"
                  checked={syntheticFixture}
                  onChange={(event) =>
                    setSyntheticFixture(event.target.checked)
                  }
                  disabled={state !== "idle" || Boolean(existingSessionId)}
                />
                Synthetic fixture transcript (local demo only)
              </Label>
              <p className="text-xs text-slate-500">
                Ordinary audio never receives a fixture transcript when ASR is
                unavailable. Record at least 11 seconds so every fixed fixture
                timestamp and evidence range fits the assembled audio.
              </p>
            </div>
          </div>

          <div className="rounded-lg border bg-slate-50 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <p className="font-medium">{inputLabel}</p>
                <p className="text-xs text-slate-500">
                  Permission: {permission} · session:{" "}
                  {activeSessionId ?? "not started"}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                {clipping && <Badge variant="destructive">Clipping</Badge>}
                {noise && (
                  <Badge className="bg-amber-100 text-amber-900">
                    Low signal
                  </Badge>
                )}
                {state === "recording" && (
                  <Badge className="bg-rose-100 text-rose-900">
                    <Radio className="mr-1 size-3 animate-pulse" /> Recording
                  </Badge>
                )}
                {liveStatus === "available" && (
                  <Badge className="bg-sky-100 text-sky-900">
                    Live captions · provisional
                  </Badge>
                )}
                {liveStatus === "unavailable" && (
                  <Badge className="bg-slate-200 text-slate-800">
                    Live captions unavailable
                  </Badge>
                )}
                {liveStatus === "needs_review" && (
                  <Badge className="bg-amber-100 text-amber-900">
                    Live captions interrupted · review
                  </Badge>
                )}
              </div>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded bg-slate-200">
              <div
                className="h-full bg-teal-600 transition-[width]"
                style={{ width: `${Math.min(100, inputLevel * 220)}%` }}
              />
            </div>
          </div>

          {liveStatus !== "not_started" && (
            <div
              className="rounded-lg border border-sky-100 bg-sky-50/60 p-4"
              aria-live="polite"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="font-medium text-sky-950">
                  Temporary live transcript
                </p>
                <span className="text-xs text-sky-800">
                  Not the clinical record · finalization replaces this view
                </span>
              </div>
              {provisionalTranscript ? (
                <p className="mt-2 whitespace-pre-wrap text-sm text-slate-800">
                  {provisionalTranscript}
                </p>
              ) : (
                <p className="mt-2 text-sm text-slate-600">
                  {liveStatus === "connecting"
                    ? "Connecting to the clinic-scoped caption channel…"
                    : liveStatus === "available"
                      ? "Listening for provisional speech text…"
                      : "Recording continues securely without live text."}
                </p>
              )}
              {(liveProvider || liveReasonCode) && (
                <p className="mt-2 text-xs text-slate-500">
                  {liveProvider ?? "live transport"}
                  {liveReasonCode ? ` · ${liveReasonCode}` : ""}
                </p>
              )}
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            {state === "recording" ? (
              <Button className="min-h-11 min-w-32 bg-rose-700" onClick={stop}>
                <Square className="mr-2 size-4" /> Stop & finalize
              </Button>
            ) : (
              <Button
                className="min-h-11 min-w-32 bg-teal-700"
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
            <span className="text-sm text-slate-600">
              {uploadedChunks}/{capturedChunks} chunks acknowledged
            </span>
          </div>

          {message && (
            <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-950">
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
              <h2>Encrypted uploads to recover</h2>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {recoverable.map((capture) => (
              <div
                className="flex flex-wrap items-center justify-between gap-2 rounded border p-3"
                key={capture.id}
              >
                <span className="break-all text-sm">
                  {capture.id}
                  {capture.empty && (
                    <span className="ml-2 text-amber-800">no audio stored</span>
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
