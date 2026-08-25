export const VOICE_CHUNK_INTERVAL_MS = 2_000

export function preferredRecorderMimeType(
  supports: (type: string) => boolean = MediaRecorder.isTypeSupported,
): string {
  const candidates = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm"]
  return candidates.find((candidate) => supports(candidate)) ?? ""
}

export function analyzeInputLevel(samples: Uint8Array): {
  level: number
  clipping: boolean
  noise: boolean
} {
  if (samples.length === 0) return { level: 0, clipping: false, noise: true }
  let peak = 0
  let energy = 0
  for (const sample of samples) {
    const centered = Math.abs(sample - 128) / 128
    peak = Math.max(peak, centered)
    energy += centered * centered
  }
  const level = Math.sqrt(energy / samples.length)
  return {
    level,
    clipping: peak > 0.98,
    noise: level > 0 && level < 0.015,
  }
}
