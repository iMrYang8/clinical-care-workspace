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
  createLocalCapture,
  enqueueEncryptedChunk,
  recoverableCaptures,
} from "@/features/voice/offlineQueue"
import {
  analyzeInputLevel,
  preferredRecorderMimeType,
  VOICE_CHUNK_INTERVAL_MS,
} from "@/features/voice/recorder"
import { finalizeCapture, uploadPendingChunks } from "@/features/voice/voiceApi"

type CaptureKind = "patient" | "clinical"

type VoiceCaptureProps = {
  patientId: string
  captureKind: CaptureKind
  role: MePublic["role"]
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

export function VoiceCapture({
  patientId,
  captureKind,
  role,
  onFinalized,
}: VoiceCaptureProps) {
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
  const [recoverable, setRecoverable] = useState<string[]>([])
  const [message, setMessage] = useState<string>()
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const analyzerFrameRef = useRef<number | null>(null)
  const chunkStartedAtRef = useRef(0)
  const writeChainRef = useRef(Promise.resolve())
  const uploadChainRef = useRef<Promise<void>>(Promise.resolve())

  const refreshRecovery = useCallback(async () => {
    const captures = await recoverableCaptures()
    setRecoverable(
      captures
        .filter((capture) => capture.patientId === patientId)
        .map((capture) => capture.id),
    )
  }, [patientId])

  const flush = useCallback(
    async (localId: string) => {
      const activelyRecording = recorderRef.current?.state === "recording"
      if (!navigator.onLine) {
        if (!activelyRecording) setState("queued")
        setMessage("Offline: encrypted chunks remain on this device.")
        return false
      }
      if (!activelyRecording) setState("uploading")
      try {
        const result = await uploadPendingChunks(localId)
        setUploadedChunks((count) => count + result.uploaded)
        if (result.remaining === 0)
          setMessage("All encrypted queue items acknowledged.")
        await refreshRecovery()
        return result.remaining === 0
      } catch (error) {
        if (!activelyRecording) setState("queued")
        setMessage(error instanceof Error ? error.message : "Upload paused")
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

  useEffect(
    () => () => {
      if (analyzerFrameRef.current)
        cancelAnimationFrame(analyzerFrameRef.current)
      streamRef.current?.getTracks().forEach((track) => {
        track.stop()
      })
      void audioContextRef.current?.close()
    },
    [],
  )

  const startLevelMeter = (stream: MediaStream) => {
    const context = new AudioContext()
    const analyser = context.createAnalyser()
    analyser.fftSize = 512
    context.createMediaStreamSource(stream).connect(analyser)
    audioContextRef.current = context
    const samples = new Uint8Array(analyser.fftSize)
    const sample = () => {
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
    setState("requesting")
    setMessage(undefined)
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      streamRef.current = stream
      setPermission("granted")
      const track = stream.getAudioTracks()[0]
      setInputLabel(track?.label || "Microphone permission granted")
      const serverSessionId = existingSessionId.trim()
        ? existingSessionId.trim()
        : (
            await VoiceService.createSession({
              body: {
                patient_id: patientId,
                capture_kind: captureKind,
                synthetic_fixture: syntheticFixture,
                fixture_id: syntheticFixture ? "code-switch-overlap-v1" : null,
              },
            })
          ).data.id
      const joined = (
        await VoiceService.joinDevice({
          path: { session_id: serverSessionId },
          body: {
            client_device_id: browserDeviceId(),
            capture_role:
              role === "patient" || role === "staff" || role === "clinician"
                ? role
                : "staff",
          },
        })
      ).data
      const mediaType = preferredRecorderMimeType()
      const recorder = mediaType
        ? new MediaRecorder(stream, { mimeType: mediaType })
        : new MediaRecorder(stream)
      recorderRef.current = recorder
      const local = await createLocalCapture({
        serverSessionId,
        serverDeviceId: joined.id,
        patientId,
        mediaType: recorder.mimeType || mediaType || "audio/webm",
      })
      setActiveSessionId(serverSessionId)
      setCaptureId(local.id)
      chunkStartedAtRef.current = 0
      writeChainRef.current = Promise.resolve()
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size === 0) return
        const startMs = chunkStartedAtRef.current
        const endMs = startMs + VOICE_CHUNK_INTERVAL_MS
        chunkStartedAtRef.current = endMs
        writeChainRef.current = writeChainRef.current.then(async () => {
          await enqueueEncryptedChunk(local.id, event.data, startMs, endMs)
          setCapturedChunks((count) => count + 1)
          if (navigator.onLine) void scheduleFlush(local.id)
        })
      })
      recorder.start(VOICE_CHUNK_INTERVAL_MS)
      startLevelMeter(stream)
      setState("recording")
    } catch (error) {
      streamRef.current?.getTracks().forEach((track) => {
        track.stop()
      })
      streamRef.current = null
      if (error instanceof DOMException && error.name === "NotAllowedError") {
        setPermission("denied")
      }
      setState("idle")
      setMessage(
        error instanceof Error ? error.message : "Recording could not start",
      )
    }
  }

  const stop = async () => {
    const recorder = recorderRef.current
    if (!recorder || !captureId) return
    await new Promise<void>((resolve) => {
      recorder.addEventListener("stop", () => resolve(), { once: true })
      recorder.stop()
    })
    streamRef.current?.getTracks().forEach((track) => {
      track.stop()
    })
    if (analyzerFrameRef.current) cancelAnimationFrame(analyzerFrameRef.current)
    await audioContextRef.current?.close()
    await writeChainRef.current
    const uploaded = await scheduleFlush(captureId)
    if (!uploaded) return
    setState("finalizing")
    try {
      const result = await finalizeCapture(captureId)
      setState("idle")
      setMessage(
        `Recording accepted. Processing is ${result.state}; live captions are unavailable in this build.`,
      )
      onFinalized?.(result.session_id)
    } catch (error) {
      setState("queued")
      setMessage(error instanceof Error ? error.message : "Finalization paused")
    }
  }

  const resume = async (localId: string) => {
    try {
      setCaptureId(localId)
      const complete = await scheduleFlush(localId)
      if (complete) {
        const result = await finalizeCapture(localId)
        onFinalized?.(result.session_id)
        setMessage("Recovered queue uploaded and finalized.")
        setState("idle")
        await refreshRecovery()
      }
    } catch (error) {
      setState("queued")
      setMessage(error instanceof Error ? error.message : "Recovery paused")
    }
  }

  const busy = ["requesting", "uploading", "finalizing"].includes(state)

  return (
    <div className="space-y-4" data-testid="voice-capture">
      <Card className="border-teal-100 shadow-sm">
        <CardHeader>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <CardTitle className="flex items-center gap-2 text-xl">
              <Mic className="text-teal-700" /> Secure voice capture
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
                unavailable.
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
              </div>
            </div>
            <div className="mt-3 h-2 overflow-hidden rounded bg-slate-200">
              <div
                className="h-full bg-teal-600 transition-[width]"
                style={{ width: `${Math.min(100, inputLevel * 220)}%` }}
              />
            </div>
          </div>

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

      {recoverable.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Encrypted uploads to recover
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {recoverable.map((localId) => (
              <div
                className="flex flex-wrap items-center justify-between gap-2 rounded border p-3"
                key={localId}
              >
                <span className="break-all text-sm">{localId}</span>
                <Button
                  variant="outline"
                  className="min-h-11"
                  onClick={() => void resume(localId)}
                >
                  Resume upload
                </Button>
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  )
}
