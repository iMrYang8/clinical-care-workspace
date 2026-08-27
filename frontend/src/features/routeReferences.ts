const CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
const UUID_HEX_PATTERN = /^[0-9a-f]{32}$/i
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export type ResolvedRouteReference = {
  id: string
  reference: string
}

function canonicalUuid(value: string): string | null {
  const trimmed = value.trim()
  if (!UUID_PATTERN.test(trimmed)) return null
  return trimmed.toLowerCase()
}

export function crockfordCodeFromUuid(uuid: string): string | null {
  const canonical = canonicalUuid(uuid)
  if (!canonical) return null

  const hex = canonical.replace(/-/g, "")
  if (!UUID_HEX_PATTERN.test(hex)) return null

  let value = BigInt(`0x${hex}`)
  let encoded = ""
  for (let index = 0; index < 26; index += 1) {
    encoded = CROCKFORD_ALPHABET[Number(value % 32n)] + encoded
    value /= 32n
  }
  return encoded.replace(/(.{5})(?=.)/g, "$1-")
}

export function uuidFromCrockfordCode(code: string): string | null {
  const normalized = code.replace(/[\s-]/g, "").toUpperCase()
  if (normalized.length !== 26) return null

  let value = 0n
  for (const character of normalized) {
    const digit = CROCKFORD_ALPHABET.indexOf(character)
    if (digit < 0) return null
    value = value * 32n + BigInt(digit)
  }
  if (value >= 1n << 128n) return null

  const hex = value.toString(16).padStart(32, "0")
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

export function recordingCodeFromSessionId(sessionId: string): string {
  return crockfordCodeFromUuid(sessionId) ?? ""
}

/**
 * Accepts both the public recording code and a raw UUID. Raw UUID support is
 * retained for joining recordings created by older clients; new URLs always
 * use the public code returned by recordingCodeFromSessionId.
 */
export function sessionIdFromRecordingCode(recordingCode: string): string {
  return (
    uuidFromCrockfordCode(recordingCode) ??
    canonicalUuid(recordingCode) ??
    recordingCode.trim()
  )
}

export function resolveRecordingRouteReference(
  value: string,
): ResolvedRouteReference | null {
  const id = uuidFromCrockfordCode(value) ?? canonicalUuid(value)
  if (!id) return null
  const reference = recordingCodeFromSessionId(id)
  return reference ? { id, reference } : null
}

export function patientRouteReferenceFromId(patientId: string): string {
  const code = crockfordCodeFromUuid(patientId)
  if (!code) throw new Error("Patient identifier must be a UUID")
  return `PAT-${code}`
}

export function resolvePatientRouteReference(
  value: string,
): ResolvedRouteReference | null {
  const trimmed = value.trim()
  const id = trimmed.toUpperCase().startsWith("PAT-")
    ? uuidFromCrockfordCode(trimmed.slice(4))
    : canonicalUuid(trimmed)
  if (!id) return null
  return { id, reference: patientRouteReferenceFromId(id) }
}
