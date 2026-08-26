class NightingaleLivePcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super()
    // Coalesce render quanta into one 100 ms application frame. This keeps
    // authorization fresh for every frame without producing ~188 database
    // checks per second at 24 kHz.
    this.frameSamples = Math.max(128, Math.round(sampleRate * 0.1))
    this.pending = new Float32Array(this.frameSamples)
    this.pendingLength = 0
  }

  process(inputs) {
    const input = inputs[0]?.[0]
    if (!input?.length) return true

    let inputOffset = 0
    while (inputOffset < input.length) {
      const writable = Math.min(
        this.frameSamples - this.pendingLength,
        input.length - inputOffset,
      )
      this.pending.set(
        input.subarray(inputOffset, inputOffset + writable),
        this.pendingLength,
      )
      inputOffset += writable
      this.pendingLength += writable
      if (this.pendingLength === this.frameSamples) {
        const completed = this.pending
        this.port.postMessage(completed.buffer, [completed.buffer])
        this.pending = new Float32Array(this.frameSamples)
        this.pendingLength = 0
      }
    }
    return true
  }
}

registerProcessor("nightingale-live-pcm", NightingaleLivePcmProcessor)
