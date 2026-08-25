import type {
  TranscriptRevisionPublic,
  VoiceFinalizePublic,
  VoiceSessionPublic,
} from "@/client"
import { VoiceService } from "@/client"
import { ACCESS_TOKEN_KEY } from "@/features/api"
import {
  acknowledgeChunk,
  completeLocalCapture,
  decryptQueuedChunk,
  localCapture,
  nextPendingChunk,
  pendingChunkCount,
} from "./offlineQueue"

function token(): string {
  const value = localStorage.getItem(ACCESS_TOKEN_KEY)
  if (!value) throw new Error("A signed-in membership is required")
  return value
}

function apiUrl(path: string): string {
  return `${import.meta.env.VITE_API_URL ?? ""}${path}`
}

export async function uploadPendingChunks(captureId: string): Promise<{
  uploaded: number
  remaining: number
}> {
  const capture = await localCapture(captureId)
  if (!capture) throw new Error("Local voice capture is missing")
  let uploaded = 0
  // Snapshot the current tail so a slower network does not chase chunks from
  // an active MediaRecorder forever. Each loop reads/decrypts exactly one
  // IndexedDB row, keeping reload recovery memory O(one chunk).
  const maxChunkIndex = capture.nextChunkIndex - 1
  while (true) {
    const chunk = await nextPendingChunk(captureId, maxChunkIndex)
    if (!chunk) break
    const plaintext = await decryptQueuedChunk(chunk)
    const response = await fetch(
      apiUrl(
        `/api/v1/voice/sessions/${capture.serverSessionId}/devices/${capture.serverDeviceId}/chunks/${chunk.chunkIndex}`,
      ),
      {
        method: "PUT",
        headers: {
          Authorization: `Bearer ${token()}`,
          "Content-Type": chunk.mediaType,
          "X-Chunk-SHA256": chunk.sha256,
          "X-Chunk-Start-Ms": String(chunk.startMs),
          "X-Chunk-End-Ms": String(chunk.endMs),
        },
        body: plaintext,
      },
    )
    if (!response.ok) {
      if (response.status === 409) {
        const payload = (await response.json()) as {
          detail?: { code?: string }
        }
        if (payload.detail?.code === "AUDIO_CHUNK_HASH_CONFLICT") {
          throw new Error(
            "The server rejected a changed chunk at the same index",
          )
        }
      }
      throw new Error(`Chunk upload paused (${response.status})`)
    }
    await acknowledgeChunk(chunk.id)
    uploaded += 1
  }
  return { uploaded, remaining: await pendingChunkCount(captureId) }
}

export async function abandonEmptyCapture(captureId: string): Promise<void> {
  const capture = await localCapture(captureId)
  if (!capture) throw new Error("Local voice capture is missing")
  if (
    capture.nextChunkIndex !== 0 ||
    (await pendingChunkCount(captureId)) !== 0
  ) {
    throw new Error("A device with captured audio cannot be abandoned")
  }
  await VoiceService.abandonDevice({
    path: {
      session_id: capture.serverSessionId,
      device_id: capture.serverDeviceId,
    },
  })
  await completeLocalCapture(captureId)
}

export async function finalizeCapture(
  captureId: string,
): Promise<VoiceFinalizePublic> {
  const capture = await localCapture(captureId)
  if (!capture || capture.nextChunkIndex < 1) {
    throw new Error("No captured audio is available to finalize")
  }
  await VoiceService.sealDevice({
    path: {
      session_id: capture.serverSessionId,
      device_id: capture.serverDeviceId,
    },
    body: { last_chunk_index: capture.nextChunkIndex - 1 },
  })
  const status = (
    await VoiceService.getChunkStatus({
      path: { session_id: capture.serverSessionId },
    })
  ).data
  const unsealed = status.devices.filter(
    (device) => device.last_declared_chunk_index === null,
  )
  if (unsealed.length > 0) {
    throw new Error(
      "Other device tracks are still recording. Stop them before finalizing.",
    )
  }
  const devices = status.devices.map((device) => ({
    device_id: device.device_id,
    last_chunk_index: device.last_declared_chunk_index as number,
  }))
  if (devices.length < 1)
    throw new Error("No uploaded device tracks are available")
  const response = await VoiceService.finalize({
    path: { session_id: capture.serverSessionId },
    headers: { "Idempotency-Key": `voice-finalize-${capture.serverSessionId}` },
    body: { devices },
  })
  await completeLocalCapture(captureId)
  return response.data
}

export async function voiceSession(
  sessionId: string,
): Promise<VoiceSessionPublic> {
  return (await VoiceService.sessionStatus({ path: { session_id: sessionId } }))
    .data
}

export async function voiceTranscript(
  sessionId: string,
): Promise<TranscriptRevisionPublic> {
  return (await VoiceService.transcript({ path: { session_id: sessionId } }))
    .data
}

export function voiceAudioUrl(sessionId: string): string {
  return apiUrl(`/api/v1/voice/sessions/${sessionId}/audio`)
}

export async function loadAuthorizedAudio(sessionId: string): Promise<string> {
  const response = await fetch(voiceAudioUrl(sessionId), {
    headers: { Authorization: `Bearer ${token()}` },
  })
  if (!response.ok) throw new Error(`Audio unavailable (${response.status})`)
  return URL.createObjectURL(await response.blob())
}
