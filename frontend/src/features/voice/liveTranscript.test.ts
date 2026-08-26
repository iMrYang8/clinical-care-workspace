import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  connectLiveTranscript,
  Pcm16StreamResampler,
  pcm16At24k,
} from "./liveTranscript"

const clientMocks = vi.hoisted(() => ({ liveStatus: vi.fn() }))

vi.mock("@/client", () => ({
  VoiceService: clientMocks,
}))

class FakeWebSocket extends EventTarget {
  binaryType: BinaryType = "blob"
  bufferedAmount = 0
  readyState = 0
  sent: Array<string | ArrayBufferLike | Blob | ArrayBufferView> = []
  closeCode?: number

  send(value: string | ArrayBufferLike | Blob | ArrayBufferView) {
    this.sent.push(value)
  }

  open() {
    this.readyState = 1
    this.dispatchEvent(new Event("open"))
  }

  message(payload: Record<string, unknown>) {
    this.dispatchEvent(
      new MessageEvent("message", { data: JSON.stringify(payload) }),
    )
  }

  close(code?: number) {
    this.closeCode = code
    this.readyState = 3
    this.dispatchEvent(new Event("close"))
  }
}

describe("live transcript transport", () => {
  beforeEach(() => {
    clientMocks.liveStatus.mockReset()
  })

  it("does not open a socket when the capability gate is unavailable", async () => {
    clientMocks.liveStatus.mockResolvedValue({
      data: {
        available: false,
        status: "unavailable",
        reason_code: "LIVE_TRANSCRIPT_NOT_CONFIGURED",
        provisional: true,
      },
    })
    const websocketFactory = vi.fn()
    const statuses: string[] = []

    const control = await connectLiveTranscript({
      sessionId: "session-1",
      stream: {} as MediaStream,
      onStatus: (event) => statuses.push(event.status),
      onDelta: vi.fn(),
      onCompleted: vi.fn(),
      websocketFactory,
    })

    expect(control).toBeUndefined()
    expect(websocketFactory).not.toHaveBeenCalled()
    expect(statuses).toEqual(["connecting", "unavailable"])
  })

  it("streams bounded PCM and keeps captions explicitly provisional", async () => {
    clientMocks.liveStatus.mockResolvedValue({
      data: {
        available: true,
        status: "available",
        reason_code: null,
        provisional: true,
      },
    })
    const socket = new FakeWebSocket()
    let emitFrame: ((frame: ArrayBuffer) => void) | undefined
    const stopPcm = vi.fn(async () => undefined)
    const statuses: string[] = []
    const deltas: string[] = []
    const completed: string[] = []
    const control = await connectLiveTranscript({
      sessionId: "session-1",
      stream: {} as MediaStream,
      onStatus: (event) => statuses.push(event.status),
      onDelta: (text) => deltas.push(text),
      onCompleted: (text) => completed.push(text),
      websocketFactory: () => socket as unknown as WebSocket,
      pcmCaptureFactory: async (_stream, onFrame) => {
        emitFrame = onFrame
        return { stop: stopPcm }
      },
    })
    expect(control).toBeDefined()

    socket.open()
    socket.message({
      type: "status",
      status: "available",
      provider: "deterministic-synthetic-fixture",
      model: "code-switch-overlap-v1",
      provisional: true,
      needs_review: false,
    })
    await Promise.resolve()
    const frame = new ArrayBuffer(8)
    emitFrame?.(frame)
    expect(socket.sent).toEqual([frame])

    socket.message({
      type: "transcript.delta",
      text: "penicillin ",
      provisional: true,
    })
    expect(deltas).toEqual(["penicillin "])

    const committing = control?.commit()
    await vi.waitFor(() => {
      expect(socket.sent[1]).toBe('{"type":"commit"}')
    })
    socket.message({
      type: "transcript.completed",
      text: "penicillin allergy",
      provisional: true,
    })
    await committing

    expect(completed).toEqual(["penicillin allergy"])
    expect(statuses).toEqual(["connecting", "available"])
    expect(stopPcm).toHaveBeenCalledOnce()
    expect(socket.closeCode).toBe(1000)
  })

  it("surfaces a review state when commit never receives provider completion", async () => {
    vi.useFakeTimers()
    try {
      clientMocks.liveStatus.mockResolvedValue({
        data: {
          available: true,
          status: "available",
          reason_code: null,
          provisional: true,
        },
      })
      const socket = new FakeWebSocket()
      const statuses: Array<{ status: string; reasonCode?: string }> = []
      const control = await connectLiveTranscript({
        sessionId: "session-1",
        stream: {} as MediaStream,
        onStatus: (event) => statuses.push(event),
        onDelta: vi.fn(),
        onCompleted: vi.fn(),
        websocketFactory: () => socket as unknown as WebSocket,
        pcmCaptureFactory: async () => ({ stop: async () => undefined }),
      })

      socket.open()
      socket.message({
        type: "status",
        status: "available",
        provisional: true,
      })
      const committing = control?.commit()
      await vi.runAllTimersAsync()
      await committing

      expect(statuses[statuses.length - 1]).toEqual({
        status: "needs_review",
        reasonCode: "LIVE_TRANSCRIPT_COMPLETION_TIMEOUT",
      })
      expect(socket.closeCode).toBe(1000)
    } finally {
      vi.useRealTimers()
    }
  })

  it("deduplicates PCM startup and closes a capture that resolves after teardown", async () => {
    clientMocks.liveStatus.mockResolvedValue({
      data: {
        available: true,
        status: "available",
        reason_code: null,
        provisional: true,
      },
    })
    const socket = new FakeWebSocket()
    const stopPcm = vi.fn(async () => undefined)
    let resolveCapture:
      | ((capture: { stop: () => Promise<void> }) => void)
      | undefined
    const pcmCaptureFactory = vi.fn(
      () =>
        new Promise<{ stop: () => Promise<void> }>((resolve) => {
          resolveCapture = resolve
        }),
    )
    const control = await connectLiveTranscript({
      sessionId: "session-1",
      stream: {} as MediaStream,
      onStatus: vi.fn(),
      onDelta: vi.fn(),
      onCompleted: vi.fn(),
      websocketFactory: () => socket as unknown as WebSocket,
      pcmCaptureFactory,
    })

    socket.open()
    const available = {
      type: "status",
      status: "available",
      provisional: true,
    }
    socket.message(available)
    socket.message(available)
    expect(pcmCaptureFactory).toHaveBeenCalledOnce()

    await control?.close()
    resolveCapture?.({ stop: stopPcm })
    await vi.waitFor(() => expect(stopPcm).toHaveBeenCalledOnce())
  })

  it("preserves a server replaced terminal state across the socket close", async () => {
    clientMocks.liveStatus.mockResolvedValue({
      data: {
        available: true,
        status: "available",
        reason_code: null,
        provisional: true,
      },
    })
    const socket = new FakeWebSocket()
    const statuses: Array<{ status: string; reasonCode?: string }> = []
    await connectLiveTranscript({
      sessionId: "session-1",
      stream: {} as MediaStream,
      onStatus: (event) => statuses.push(event),
      onDelta: vi.fn(),
      onCompleted: vi.fn(),
      websocketFactory: () => socket as unknown as WebSocket,
      pcmCaptureFactory: async () => ({ stop: async () => undefined }),
    })

    socket.open()
    socket.message({
      type: "status",
      status: "replaced",
      reason_code: "FINAL_TRANSCRIPT_AVAILABLE",
      provisional: true,
    })
    socket.close(1000)

    expect(statuses[statuses.length - 1]).toEqual({
      status: "replaced",
      reasonCode: "FINAL_TRANSCRIPT_AVAILABLE",
      provider: undefined,
      model: undefined,
    })
  })

  it("preserves the server review reason when commit closes without completion", async () => {
    clientMocks.liveStatus.mockResolvedValue({
      data: {
        available: true,
        status: "available",
        reason_code: null,
        provisional: true,
      },
    })
    const socket = new FakeWebSocket()
    const statuses: Array<{ status: string; reasonCode?: string }> = []
    const control = await connectLiveTranscript({
      sessionId: "session-1",
      stream: {} as MediaStream,
      onStatus: (event) => statuses.push(event),
      onDelta: vi.fn(),
      onCompleted: vi.fn(),
      websocketFactory: () => socket as unknown as WebSocket,
      pcmCaptureFactory: async () => ({ stop: async () => undefined }),
    })

    socket.open()
    socket.message({
      type: "status",
      status: "available",
      provisional: true,
    })
    const committing = control?.commit()
    await vi.waitFor(() => {
      expect(socket.sent).toContain('{"type":"commit"}')
    })
    socket.message({
      type: "status",
      status: "needs_review",
      reason_code: "LIVE_TRANSCRIPT_PROVIDER_ERROR",
      provisional: true,
    })
    socket.close(1011)
    await committing

    expect(statuses[statuses.length - 1]).toMatchObject({
      status: "needs_review",
      reasonCode: "LIVE_TRANSCRIPT_PROVIDER_ERROR",
    })
  })

  it("resamples and clamps browser audio to little-endian PCM16 at 24 kHz", () => {
    const encoded = pcm16At24k(
      new Float32Array([-2, -0.5, 0, 0.5, 2, 0, 0, 0]),
      48_000,
    )
    expect(Array.from(new Int16Array(encoded))).toEqual([-32768, 0, 32767, 0])
  })

  it("preserves phase across 100 ms 44.1 kHz application frames", () => {
    const samples = Float32Array.from({ length: 8_820 }, (_, index) =>
      Math.sin(index / 31),
    )
    const streaming = new Pcm16StreamResampler(44_100)
    const first = new Int16Array(streaming.encode(samples.slice(0, 4_410)))
    const second = new Int16Array(streaming.encode(samples.slice(4_410)))
    const joined = [...first, ...second]
    const whole = Array.from(new Int16Array(pcm16At24k(samples, 44_100)))

    expect(first.byteLength).toBe(4_800)
    expect(second.byteLength).toBe(4_800)
    expect(joined).toEqual(whole)
  })
})
