import { describe, expect, it } from "vitest"

import {
  crockfordCodeFromUuid,
  patientRouteReferenceFromId,
  recordingCodeFromSessionId,
  resolvePatientRouteReference,
  resolveRecordingRouteReference,
  sessionIdFromRecordingCode,
  uuidFromCrockfordCode,
} from "./routeReferences"

const UUID = "123e4567-e89b-12d3-a456-426614174000"

describe("public route references", () => {
  it("round-trips UUIDs through the shared Crockford codec", () => {
    const code = crockfordCodeFromUuid(UUID)

    expect(code).toMatch(/^[0-9A-HJKMNP-TV-Z-]{31}$/)
    expect(code).not.toContain(UUID)
    expect(uuidFromCrockfordCode(code!)).toBe(UUID)
  })

  it("creates a prefixed patient reference and resolves it case-insensitively", () => {
    const reference = patientRouteReferenceFromId(UUID)

    expect(reference).toMatch(/^PAT-[0-9A-HJKMNP-TV-Z-]{31}$/)
    expect(resolvePatientRouteReference(reference.toLowerCase())).toEqual({
      id: UUID,
      reference,
    })
  })

  it("accepts legacy raw patient UUIDs but returns their canonical route", () => {
    const resolved = resolvePatientRouteReference(UUID.toUpperCase())

    expect(resolved).toEqual({
      id: UUID,
      reference: patientRouteReferenceFromId(UUID),
    })
  })

  it("uses recording codes in voice routes while retaining legacy UUID input", () => {
    const reference = recordingCodeFromSessionId(UUID)

    expect(sessionIdFromRecordingCode(reference)).toBe(UUID)
    expect(resolveRecordingRouteReference(UUID)).toEqual({
      id: UUID,
      reference,
    })
  })

  it("rejects malformed and overflowing public references", () => {
    expect(resolvePatientRouteReference("PAT-not-a-code")).toBeNull()
    expect(resolvePatientRouteReference("not-a-uuid")).toBeNull()
    expect(resolveRecordingRouteReference("Z".repeat(26))).toBeNull()
    expect(uuidFromCrockfordCode("O".repeat(26))).toBeNull()
  })
})
