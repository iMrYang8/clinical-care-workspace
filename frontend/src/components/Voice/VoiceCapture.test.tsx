import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  recordingCodeFromSessionId,
  sessionIdFromRecordingCode,
  VoiceCapture,
} from "./VoiceCapture"

const clientMocks = vi.hoisted(() => ({
  sessionStatus: vi.fn(),
  createSession: vi.fn(),
  joinDevice: vi.fn(),
  abandonDevice: vi.fn(),
}))

const queueMocks = vi.hoisted(() => ({
  recoverableCaptures: vi.fn(),
  createLocalCapture: vi.fn(),
  enqueueEncryptedChunk: vi.fn(),
}))

vi.mock("@/client", () => ({
  VoiceService: clientMocks,
}))

vi.mock("@/features/voice/offlineQueue", () => queueMocks)

vi.mock("@/features/voice/voiceApi", () => ({
  abandonEmptyCapture: vi.fn(),
  finalizeCapture: vi.fn(),
  uploadPendingChunks: vi.fn(),
}))

type Deferred<T> = {
  promise: Promise<T>
  resolve: (value: T) => void
}

function deferred<T>(): Deferred<T> {
  let resolve: ((value: T) => void) | undefined
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve
  })
  return {
    promise,
    resolve: (value) => resolve?.(value),
  }
}

function fakeStream() {
  const stop = vi.fn()
  const track = { label: "Test microphone", stop }
  return {
    stop,
    stream: {
      getTracks: () => [track],
      getAudioTracks: () => [track],
    } as unknown as MediaStream,
  }
}

describe("recording share codes", () => {
  it("round-trips a session identifier without displaying UUID syntax", () => {
    const sessionId = "550e8400-e29b-41d4-a716-446655440000"
    const recordingCode = recordingCodeFromSessionId(sessionId)

    expect(recordingCode).toMatch(/^[0-9A-HJKMNP-TV-Z-]{31}$/)
    expect(recordingCode).not.toContain(sessionId)
    expect(sessionIdFromRecordingCode(recordingCode)).toBe(sessionId)
  })
})

describe("VoiceCapture lifecycle", () => {
  const recorderConstructed = vi.fn()

  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem("nightingale_voice_browser_device_id", "browser-1")
    queueMocks.recoverableCaptures.mockResolvedValue([])
    clientMocks.createSession.mockResolvedValue({ data: { id: "session-1" } })
    clientMocks.abandonDevice.mockResolvedValue({ data: {} })

    class RecorderStub {
      static isTypeSupported() {
        return true
      }

      state = "inactive"
      mimeType = "audio/webm"

      constructor() {
        recorderConstructed()
      }
    }
    vi.stubGlobal("MediaRecorder", RecorderStub)
  })

  it("stops a permission stream that resolves after navigation", async () => {
    const permission = deferred<MediaStream>()
    const getUserMedia = vi.fn(() => permission.promise)
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    })
    const { stop, stream } = fakeStream()
    const captureProps = {
      patientId: "patient-1",
      captureKind: "clinical" as const,
      role: "clinician" as const,
      owner: {
        userId: "user-1",
        membershipId: "membership-1",
        clinicId: "clinic-1",
      },
    }
    const view = render(<VoiceCapture {...captureProps} />)

    fireEvent.click(screen.getByRole("button", { name: /Start recording/i }))
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledOnce())
    view.unmount()
    await act(async () => permission.resolve(stream))

    await waitFor(() => expect(stop).toHaveBeenCalledOnce())
    expect(clientMocks.createSession).not.toHaveBeenCalled()
    expect(clientMocks.joinDevice).not.toHaveBeenCalled()
    expect(recorderConstructed).not.toHaveBeenCalled()
  })

  it("abandons a joined empty track when navigation wins the join race", async () => {
    const joined = deferred<{ data: { id: string } }>()
    const { stop, stream } = fakeStream()
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn(async () => stream) },
    })
    clientMocks.joinDevice.mockReturnValue(joined.promise)
    const captureProps = {
      patientId: "patient-1",
      captureKind: "clinical" as const,
      role: "clinician" as const,
      owner: {
        userId: "user-1",
        membershipId: "membership-1",
        clinicId: "clinic-1",
      },
    }
    const view = render(<VoiceCapture {...captureProps} />)

    fireEvent.click(screen.getByRole("button", { name: /Start recording/i }))
    await waitFor(() => expect(clientMocks.joinDevice).toHaveBeenCalledOnce())
    view.unmount()
    await act(async () => joined.resolve({ data: { id: "device-1" } }))

    await waitFor(() =>
      expect(clientMocks.abandonDevice).toHaveBeenCalledWith({
        path: { session_id: "session-1", device_id: "device-1" },
      }),
    )
    expect(stop).toHaveBeenCalled()
    expect(queueMocks.createLocalCapture).not.toHaveBeenCalled()
    expect(recorderConstructed).not.toHaveBeenCalled()
  })
})
