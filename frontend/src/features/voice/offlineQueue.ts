import { type DBSchema, type IDBPDatabase, openDB } from "idb"

export type LocalCapture = {
  id: string
  serverSessionId: string
  serverDeviceId: string
  patientId: string
  mediaType: string
  key: CryptoKey
  nextChunkIndex: number
  createdAt: string
}

export type EncryptedVoiceChunk = {
  id: string
  captureId: string
  chunkIndex: number
  iv: Uint8Array<ArrayBuffer>
  ciphertext: ArrayBuffer
  sha256: string
  byteLength: number
  mediaType: string
  startMs: number
  endMs: number
  createdAt: string
}

interface VoiceCaptureDatabase extends DBSchema {
  captures: {
    key: string
    value: LocalCapture
  }
  chunks: {
    key: string
    value: EncryptedVoiceChunk
    indexes: { "by-capture": string }
  }
}

let databasePromise: Promise<IDBPDatabase<VoiceCaptureDatabase>> | undefined

function database(): Promise<IDBPDatabase<VoiceCaptureDatabase>> {
  databasePromise ??= openDB<VoiceCaptureDatabase>("nightingale-voice-v1", 1, {
    upgrade(db) {
      db.createObjectStore("captures", { keyPath: "id" })
      const chunks = db.createObjectStore("chunks", { keyPath: "id" })
      chunks.createIndex("by-capture", "captureId")
    },
  })
  return databasePromise
}

export function resetVoiceDatabaseForTests(): void {
  databasePromise = undefined
}

function hex(buffer: ArrayBuffer): string {
  return [...new Uint8Array(buffer)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("")
}

export async function createLocalCapture(input: {
  serverSessionId: string
  serverDeviceId: string
  patientId: string
  mediaType: string
}): Promise<LocalCapture> {
  const db = await database()
  const existing = await db.get("captures", input.serverSessionId)
  if (existing) return existing
  const key = await crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  )
  const capture: LocalCapture = {
    id: input.serverSessionId,
    ...input,
    key,
    nextChunkIndex: 0,
    createdAt: new Date().toISOString(),
  }
  await db.put("captures", capture)
  return capture
}

export async function enqueueEncryptedChunk(
  captureId: string,
  blob: Blob,
  startMs: number,
  endMs: number,
): Promise<EncryptedVoiceChunk> {
  const db = await database()
  const capture = await db.get("captures", captureId)
  if (!capture) throw new Error("Local voice capture is missing")
  const reservedIndex = capture.nextChunkIndex
  const plaintext = await blob.arrayBuffer()
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    capture.key,
    plaintext,
  )
  const chunk: EncryptedVoiceChunk = {
    id: `${captureId}:${reservedIndex}`,
    captureId,
    chunkIndex: reservedIndex,
    iv,
    ciphertext,
    sha256: hex(await crypto.subtle.digest("SHA-256", plaintext)),
    byteLength: plaintext.byteLength,
    mediaType: blob.type || capture.mediaType,
    startMs,
    endMs,
    createdAt: new Date().toISOString(),
  }
  const transaction = db.transaction(["captures", "chunks"], "readwrite")
  const current = await transaction.objectStore("captures").get(captureId)
  if (!current) {
    transaction.abort()
    throw new Error("Local voice capture is missing")
  }
  if (current.nextChunkIndex !== reservedIndex) {
    transaction.abort()
    try {
      await transaction.done
    } catch {
      // Expected: the transaction was aborted to release the stale index.
    }
    // MediaRecorder callbacks can overlap while encryption is in flight. Retry
    // against the next durable index instead of reusing an index.
    return enqueueEncryptedChunk(captureId, blob, startMs, endMs)
  }
  current.nextChunkIndex += 1
  await transaction.objectStore("chunks").add(chunk)
  await transaction.objectStore("captures").put(current)
  await transaction.done
  return chunk
}

export async function decryptQueuedChunk(
  chunk: EncryptedVoiceChunk,
): Promise<ArrayBuffer> {
  const db = await database()
  const capture = await db.get("captures", chunk.captureId)
  if (!capture) throw new Error("Local voice encryption key is missing")
  return crypto.subtle.decrypt(
    { name: "AES-GCM", iv: chunk.iv },
    capture.key,
    chunk.ciphertext,
  )
}

export async function pendingChunks(
  captureId: string,
): Promise<EncryptedVoiceChunk[]> {
  const db = await database()
  const rows = await db.getAllFromIndex("chunks", "by-capture", captureId)
  return rows.sort((left, right) => left.chunkIndex - right.chunkIndex)
}

export async function acknowledgeChunk(chunkId: string): Promise<void> {
  const db = await database()
  await db.delete("chunks", chunkId)
}

export async function localCapture(
  captureId: string,
): Promise<LocalCapture | undefined> {
  return (await database()).get("captures", captureId)
}

export async function recoverableCaptures(): Promise<LocalCapture[]> {
  const db = await database()
  const captures = await db.getAll("captures")
  // Keep a stopped/uploaded capture recoverable until finalization itself is
  // acknowledged. A network failure can happen after the final chunk ACK.
  return captures
    .filter((capture) => capture.nextChunkIndex > 0)
    .sort((left, right) => left.createdAt.localeCompare(right.createdAt))
}

export async function completeLocalCapture(captureId: string): Promise<void> {
  const db = await database()
  const transaction = db.transaction(["captures", "chunks"], "readwrite")
  const chunks = await transaction
    .objectStore("chunks")
    .index("by-capture")
    .getAll(captureId)
  for (const chunk of chunks) {
    await transaction.objectStore("chunks").delete(chunk.id)
  }
  await transaction.objectStore("captures").delete(captureId)
  await transaction.done
}
