import "fake-indexeddb/auto"

import { deleteDB } from "idb"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  acknowledgeChunk,
  completeLocalCapture,
  createLocalCapture,
  decryptQueuedChunk,
  enqueueEncryptedChunk,
  nextPendingChunk,
  pendingChunkCount,
  recoverableCaptures,
  resetVoiceDatabaseForTests,
} from "./offlineQueue"
import { uploadPendingChunks } from "./voiceApi"

describe("encrypted voice offline queue", () => {
  beforeEach(async () => {
    await resetVoiceDatabaseForTests()
    await deleteDB("nightingale-voice-v1")
  })

  afterEach(async () => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    localStorage.clear()
    await resetVoiceDatabaseForTests()
    await deleteDB("nightingale-voice-v1")
  })

  it("stores ciphertext and restores ordered pending chunks after reload", async () => {
    const capture = await createLocalCapture({
      serverSessionId: "session-1",
      serverDeviceId: "device-1",
      patientId: "patient-1",
      mediaType: "audio/webm;codecs=opus",
    })
    expect(capture.key.extractable).toBe(false)
    expect((await recoverableCaptures()).map((item) => item.id)).toEqual([
      "session-1",
    ])
    const first = await enqueueEncryptedChunk(
      capture.id,
      new Blob(["first"], { type: "audio/webm" }),
      0,
      2_000,
    )
    const second = await enqueueEncryptedChunk(
      capture.id,
      new Blob(["second"], { type: "audio/webm" }),
      2_000,
      4_000,
    )
    expect(new TextDecoder().decode(first.ciphertext)).not.toContain("first")
    expect(new TextDecoder().decode(await decryptQueuedChunk(first))).toBe(
      "first",
    )

    await resetVoiceDatabaseForTests()
    const recovered = await recoverableCaptures()
    expect(recovered.map((item) => item.id)).toEqual(["session-1"])
    expect((await nextPendingChunk(capture.id))?.chunkIndex).toBe(0)
    expect(await pendingChunkCount(capture.id)).toBe(2)

    await acknowledgeChunk(first.id)
    expect((await nextPendingChunk(capture.id))?.id).toBe(second.id)
    await acknowledgeChunk(second.id)
    expect(await nextPendingChunk(capture.id)).toBeUndefined()
    expect(await pendingChunkCount(capture.id)).toBe(0)
    expect((await recoverableCaptures()).map((item) => item.id)).toEqual([
      "session-1",
    ])

    await completeLocalCapture(capture.id)
    expect(await recoverableCaptures()).toEqual([])
  })

  it("streams a large recovery queue without getAll materialization", async () => {
    const capture = await createLocalCapture({
      serverSessionId: "session-large",
      serverDeviceId: "device-large",
      patientId: "patient-1",
      mediaType: "audio/webm;codecs=opus",
    })
    const payload = new Uint8Array(1024 * 1024)
    for (let index = 0; index < 16; index += 1) {
      payload[0] = index
      await enqueueEncryptedChunk(
        capture.id,
        new Blob([payload], { type: "audio/webm" }),
        index * 2_000,
        (index + 1) * 2_000,
      )
    }

    const getAllSpy = vi.spyOn(IDBIndex.prototype, "getAll")
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200 }))
    vi.stubGlobal("fetch", fetchMock)

    await resetVoiceDatabaseForTests()
    const result = await uploadPendingChunks(capture.id)

    expect(result).toEqual({ uploaded: 16, remaining: 0 })
    expect(fetchMock).toHaveBeenCalledTimes(16)
    expect(getAllSpy).not.toHaveBeenCalled()
  })
})
