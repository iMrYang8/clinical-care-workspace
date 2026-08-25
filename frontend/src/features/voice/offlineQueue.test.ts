import "fake-indexeddb/auto"

import { deleteDB } from "idb"
import { beforeEach, describe, expect, it } from "vitest"

import {
  acknowledgeChunk,
  completeLocalCapture,
  createLocalCapture,
  decryptQueuedChunk,
  enqueueEncryptedChunk,
  pendingChunks,
  recoverableCaptures,
  resetVoiceDatabaseForTests,
} from "./offlineQueue"

describe("encrypted voice offline queue", () => {
  beforeEach(async () => {
    await deleteDB("nightingale-voice-v1")
    resetVoiceDatabaseForTests()
  })

  it("stores ciphertext and restores ordered pending chunks after reload", async () => {
    const capture = await createLocalCapture({
      serverSessionId: "session-1",
      serverDeviceId: "device-1",
      patientId: "patient-1",
      mediaType: "audio/webm;codecs=opus",
    })
    expect(capture.key.extractable).toBe(false)
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

    resetVoiceDatabaseForTests()
    const recovered = await recoverableCaptures()
    expect(recovered.map((item) => item.id)).toEqual(["session-1"])
    expect(
      (await pendingChunks(capture.id)).map((item) => item.chunkIndex),
    ).toEqual([0, 1])

    await acknowledgeChunk(first.id)
    expect((await pendingChunks(capture.id)).map((item) => item.id)).toEqual([
      second.id,
    ])
    await acknowledgeChunk(second.id)
    expect((await pendingChunks(capture.id)).length).toBe(0)
    expect((await recoverableCaptures()).map((item) => item.id)).toEqual([
      "session-1",
    ])

    await completeLocalCapture(capture.id)
    expect(await recoverableCaptures()).toEqual([])
  })
})
