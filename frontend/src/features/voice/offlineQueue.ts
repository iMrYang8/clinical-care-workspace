import { type DBSchema, deleteDB, type IDBPDatabase, openDB } from "idb"

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
    indexes: {
      "by-capture": string
      "by-capture-index": [string, number]
    }
  }
}

let databasePromise: Promise<IDBPDatabase<VoiceCaptureDatabase>> | undefined

function database(): Promise<IDBPDatabase<VoiceCaptureDatabase>> {
  databasePromise ??= openDB<VoiceCaptureDatabase>("nightingale-voice-v1", 2, {
    upgrade(db, oldVersion, _newVersion, transaction) {
      if (oldVersion < 1) {
        db.createObjectStore("captures", { keyPath: "id" })
        const chunks = db.createObjectStore("chunks", { keyPath: "id" })
        chunks.createIndex("by-capture", "captureId")
        chunks.createIndex("by-capture-index", ["captureId", "chunkIndex"])
        return
      }
      if (oldVersion < 2) {
        // Numeric compound ordering avoids lexicographic 10-before-2 uploads
        // while allowing one encrypted record to be read at a time.
        transaction
          .objectStore("chunks")
          .createIndex("by-capture-index", ["captureId", "chunkIndex"])
      }
    },
  })
  return databasePromise
}

export async function resetVoiceDatabaseForTests(): Promise<void> {
  const existing = databasePromise
  databasePromise = undefined
  if (existing) (await existing).close()
}

export async function purgeVoiceDatabase(): Promise<void> {
  await resetVoiceDatabaseForTests()
  await deleteDB("nightingale-voice-v1")
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

export async function nextPendingChunk(
  captureId: string,
  maxChunkIndex = Number.MAX_SAFE_INTEGER,
): Promise<EncryptedVoiceChunk | undefined> {
  if (maxChunkIndex < 0) return undefined
  const db = await database()
  return db.getFromIndex(
    "chunks",
    "by-capture-index",
    IDBKeyRange.bound(
      [captureId, 0],
      [captureId, Math.min(maxChunkIndex, Number.MAX_SAFE_INTEGER)],
    ),
  )
}

export async function pendingChunkCount(captureId: string): Promise<number> {
  const db = await database()
  return db.countFromIndex("chunks", "by-capture", captureId)
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
  // Zero-chunk captures remain visible so a crash between server join and the
  // first MediaRecorder event can explicitly abandon that empty server device.
  // Non-empty captures remain until finalization itself is acknowledged.
  return captures.sort((left, right) =>
    left.createdAt.localeCompare(right.createdAt),
  )
}

export async function completeLocalCapture(captureId: string): Promise<void> {
  const db = await database()
  const transaction = db.transaction(["captures", "chunks"], "readwrite")
  let cursor = await transaction
    .objectStore("chunks")
    .index("by-capture")
    .openCursor(captureId)
  while (cursor) {
    await cursor.delete()
    cursor = await cursor.continue()
  }
  await transaction.objectStore("captures").delete(captureId)
  await transaction.done
}
