import { VoiceService } from "@/client"

export type LiveCaptionStatus =
  | "not_started"
  | "connecting"
  | "available"
  | "unavailable"
  | "needs_review"
  | "replaced"

export type LiveCaptionStatusEvent = {
  status: LiveCaptionStatus
  reasonCode?: string
  provider?: string
  model?: string
}

export type LiveTranscriptControl = {
  commit: () => Promise<void>
  close: () => Promise<void>
}

type PcmCapture = { stop: () => Promise<void> }
type PcmCaptureFactory = (
  stream: MediaStream,
  onFrame: (frame: ArrayBuffer) => void,
) => Promise<PcmCapture>

type LiveTranscriptOptions = {
  sessionId: string
  stream: MediaStream
  onStatus: (event: LiveCaptionStatusEvent) => void
  onDelta: (text: string) => void
  onCompleted: (text: string) => void
  websocketFactory?: (url: string) => WebSocket
  pcmCaptureFactory?: PcmCaptureFactory
}

const LIVE_MAX_BUFFERED_BYTES = 256 * 1024
const LIVE_COMMIT_WAIT_MS = 1_500

function pcm16Sample(sample: number): number {
  const clamped = Math.max(-1, Math.min(1, sample))
  return clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff
}

export class Pcm16StreamResampler {
  private readonly sourceStep: number
  private carry = new Float32Array(0)
  private position = 0

  constructor(inputSampleRate: number) {
    if (!Number.isFinite(inputSampleRate) || inputSampleRate <= 0) {
      throw new Error("Invalid microphone sample rate")
    }
    this.sourceStep = inputSampleRate / 24_000
  }

  encode(samples: Float32Array): ArrayBuffer {
    if (samples.length === 0) return new ArrayBuffer(0)
    const combined = new Float32Array(this.carry.length + samples.length)
    combined.set(this.carry)
    combined.set(samples, this.carry.length)
    const encoded: number[] = []

    // Keep the fractional source position across application frames and use
    // linear interpolation across their boundary. This avoids 44.1 kHz drift
    // and discontinuities from independently rounding every frame.
    while (this.position + 1 < combined.length) {
      const leftIndex = Math.floor(this.position)
      const fraction = this.position - leftIndex
      const left = combined[leftIndex] ?? 0
      const right = combined[leftIndex + 1] ?? left
      encoded.push(pcm16Sample(left + (right - left) * fraction))
      this.position += this.sourceStep
    }

    const discard = Math.min(
      Math.floor(this.position),
      Math.max(0, combined.length - 1),
    )
    this.carry = combined.slice(discard)
    this.position -= discard
    return Int16Array.from(encoded).buffer
  }
}

export function pcm16At24k(
  samples: Float32Array,
  inputSampleRate: number,
): ArrayBuffer {
  return new Pcm16StreamResampler(inputSampleRate).encode(samples)
}

async function defaultPcmCaptureFactory(
  stream: MediaStream,
  onFrame: (frame: ArrayBuffer) => void,
): Promise<PcmCapture> {
  const context = new AudioContext({ sampleRate: 24_000 })
  try {
    if (context.state === "suspended") await context.resume()
    await context.audioWorklet.addModule("/live-pcm-worklet.js")
    const source = context.createMediaStreamSource(stream)
    const worklet = new AudioWorkletNode(context, "nightingale-live-pcm")
    const resampler = new Pcm16StreamResampler(context.sampleRate)
    const muted = context.createGain()
    muted.gain.value = 0
    worklet.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (!(event.data instanceof ArrayBuffer)) return
      const samples = new Float32Array(event.data)
      const frame = resampler.encode(samples)
      if (frame.byteLength > 0) onFrame(frame)
    }
    source.connect(worklet)
    worklet.connect(muted)
    muted.connect(context.destination)
    return {
      stop: async () => {
        worklet.port.onmessage = null
        source.disconnect()
        worklet.disconnect()
        muted.disconnect()
        await context.close().catch(() => undefined)
      },
    }
  } catch (error) {
    await context.close().catch(() => undefined)
    throw error
  }
}

function websocketUrl(sessionId: string): string {
  const apiBase = import.meta.env.VITE_API_URL || window.location.origin
  const url = new URL(
    `/api/v1/voice/sessions/${encodeURIComponent(sessionId)}/live/ws`,
    apiBase,
  )
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:"
  return url.toString()
}

function safeEvent(value: unknown): Record<string, unknown> | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return
  return value as Record<string, unknown>
}

export async function connectLiveTranscript({
  sessionId,
  stream,
  onStatus,
  onDelta,
  onCompleted,
  websocketFactory = (url) => new WebSocket(url),
  pcmCaptureFactory = defaultPcmCaptureFactory,
}: LiveTranscriptOptions): Promise<LiveTranscriptControl | undefined> {
  onStatus({ status: "connecting" })
  let availability: Awaited<ReturnType<typeof VoiceService.liveStatus>>["data"]
  try {
    availability = (
      await VoiceService.liveStatus({ path: { session_id: sessionId } })
    ).data
  } catch {
    onStatus({
      status: "unavailable",
      reasonCode: "LIVE_TRANSCRIPT_CAPABILITY_UNAVAILABLE",
    })
    return
  }
  if (!availability.available) {
    onStatus({
      status: availability.status === "replaced" ? "replaced" : "unavailable",
      reasonCode: availability.reason_code ?? undefined,
    })
    return
  }

  const socket = websocketFactory(websocketUrl(sessionId))
  socket.binaryType = "arraybuffer"
  let pcmCapture: PcmCapture | undefined
  let pcmStarting = false
  let pcmEpoch = 0
  let committed = false
  let deliberatelyClosed = false
  let completed = false
  let finalCompleted = false
  let serverTerminalStatus = false
  let finalCompletedResolve: (() => void) | undefined
  const finalCompletedPromise = new Promise<void>((resolve) => {
    finalCompletedResolve = resolve
  })
  const completedTurns: string[] = []

  const stopPcm = async () => {
    pcmEpoch += 1
    const active = pcmCapture
    pcmCapture = undefined
    await active?.stop()
  }

  const markNeedsReview = (reasonCode: string) => {
    onStatus({ status: "needs_review", reasonCode })
  }

  const beginPcm = async () => {
    if (
      pcmCapture ||
      pcmStarting ||
      deliberatelyClosed ||
      committed ||
      socket.readyState !== 1
    )
      return
    pcmStarting = true
    const startEpoch = pcmEpoch
    try {
      const capture = await pcmCaptureFactory(stream, (frame) => {
        if (
          socket.readyState !== 1 ||
          committed ||
          socket.bufferedAmount > LIVE_MAX_BUFFERED_BYTES
        ) {
          if (socket.bufferedAmount > LIVE_MAX_BUFFERED_BYTES) {
            markNeedsReview("LIVE_TRANSCRIPT_CLIENT_BACKPRESSURE")
            void stopPcm()
            socket.close(1011, "live transcript backpressure")
          }
          return
        }
        socket.send(frame)
      })
      if (
        startEpoch !== pcmEpoch ||
        deliberatelyClosed ||
        committed ||
        socket.readyState !== 1
      ) {
        await capture.stop()
      } else {
        pcmCapture = capture
      }
    } catch {
      if (!deliberatelyClosed && !committed) {
        markNeedsReview("LIVE_TRANSCRIPT_PCM_CAPTURE_UNAVAILABLE")
        socket.close(1011, "live PCM capture unavailable")
      }
    } finally {
      pcmStarting = false
    }
  }

  socket.addEventListener("message", (message) => {
    if (typeof message.data !== "string") return
    let parsed: unknown
    try {
      parsed = JSON.parse(message.data)
    } catch {
      markNeedsReview("LIVE_TRANSCRIPT_PROTOCOL_ERROR")
      socket.close(1002, "live transcript protocol error")
      return
    }
    const event = safeEvent(parsed)
    if (event?.provisional !== true) return
    if (event.type === "status") {
      const status = event.status
      if (
        status === "available" ||
        status === "unavailable" ||
        status === "needs_review" ||
        status === "replaced"
      ) {
        onStatus({
          status,
          reasonCode:
            typeof event.reason_code === "string"
              ? event.reason_code
              : undefined,
          provider:
            typeof event.provider === "string" ? event.provider : undefined,
          model: typeof event.model === "string" ? event.model : undefined,
        })
        if (status === "available") {
          void beginPcm()
        } else {
          // The server sends its specific terminal/review reason before
          // closing. Preserve that reason instead of replacing it with a
          // generic disconnect badge in the subsequent close event.
          serverTerminalStatus = true
          void stopPcm()
        }
      }
      return
    }
    if (event.type === "transcript.delta" && typeof event.text === "string") {
      onDelta(event.text)
      return
    }
    if (
      event.type === "transcript.completed" &&
      typeof event.text === "string"
    ) {
      completed = true
      const existing = completedTurns.join(" ")
      if (existing && event.text.startsWith(existing)) {
        completedTurns.splice(0, completedTurns.length, event.text)
      } else if (!completedTurns.includes(event.text)) {
        completedTurns.push(event.text)
      }
      onCompleted(completedTurns.join(" "))
      if (committed) {
        finalCompleted = true
        finalCompletedResolve?.()
      }
    }
  })
  socket.addEventListener("error", () => {
    markNeedsReview("LIVE_TRANSCRIPT_CONNECTION_ERROR")
  })
  socket.addEventListener("close", () => {
    void stopPcm()
    if (!deliberatelyClosed && !completed && !serverTerminalStatus) {
      serverTerminalStatus = true
      markNeedsReview("LIVE_TRANSCRIPT_DISCONNECTED")
    }
    if (committed) finalCompletedResolve?.()
  })

  return {
    commit: async () => {
      if (committed || deliberatelyClosed) return
      committed = true
      await stopPcm()
      if (socket.readyState === 0) {
        await new Promise<void>((resolve) => {
          const timer = window.setTimeout(resolve, 500)
          socket.addEventListener(
            "open",
            () => {
              window.clearTimeout(timer)
              resolve()
            },
            { once: true },
          )
        })
      }
      if (socket.readyState === 1) {
        socket.send(JSON.stringify({ type: "commit" }))
        await Promise.race([
          finalCompletedPromise,
          new Promise<void>((resolve) =>
            window.setTimeout(resolve, LIVE_COMMIT_WAIT_MS),
          ),
        ])
        if (!finalCompleted && !serverTerminalStatus) {
          markNeedsReview("LIVE_TRANSCRIPT_COMPLETION_TIMEOUT")
        }
      } else {
        markNeedsReview("LIVE_TRANSCRIPT_COMMIT_UNAVAILABLE")
      }
      deliberatelyClosed = true
      if (socket.readyState === 0 || socket.readyState === 1) {
        socket.close(1000, "capture finalized")
      }
    },
    close: async () => {
      if (deliberatelyClosed) return
      deliberatelyClosed = true
      await stopPcm()
      if (socket.readyState === 0 || socket.readyState === 1) {
        socket.close(1000, "capture closed")
      }
    },
  }
}
