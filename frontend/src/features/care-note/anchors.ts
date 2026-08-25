export type CanonicalAnchor = {
  start_offset: number
  end_offset: number
  exact_quote: string
  prefix: string
  suffix: string
}

const DEFAULT_CONTEXT_CODE_POINTS = 32

export function canonicalizeText(value: string): string {
  return value.replace(/\r\n?/g, "\n").normalize("NFC")
}

export function codePointLength(value: string): number {
  return Array.from(value).length
}

export function createCanonicalAnchor(
  rawContent: string,
  rawStartUtf16: number,
  rawEndUtf16: number,
  contextCodePoints = DEFAULT_CONTEXT_CODE_POINTS,
): CanonicalAnchor {
  if (
    !Number.isInteger(rawStartUtf16) ||
    !Number.isInteger(rawEndUtf16) ||
    rawStartUtf16 < 0 ||
    rawEndUtf16 < rawStartUtf16 ||
    rawEndUtf16 > rawContent.length
  ) {
    throw new RangeError("Selection is outside the canonical care-note text")
  }

  const before = canonicalizeText(rawContent.slice(0, rawStartUtf16))
  const quote = canonicalizeText(rawContent.slice(rawStartUtf16, rawEndUtf16))
  const after = canonicalizeText(rawContent.slice(rawEndUtf16))
  const beforePoints = Array.from(before)
  const quotePoints = Array.from(quote)

  return {
    start_offset: beforePoints.length,
    end_offset: beforePoints.length + quotePoints.length,
    exact_quote: quote,
    prefix: beforePoints.slice(-contextCodePoints).join(""),
    suffix: Array.from(after).slice(0, contextCodePoints).join(""),
  }
}

export function locateExactQuote(
  rawContent: string,
  rawQuote: string,
  occurrence = 0,
): CanonicalAnchor {
  const content = canonicalizeText(rawContent)
  const quote = canonicalizeText(rawQuote)
  if (!quote || occurrence < 0 || !Number.isInteger(occurrence)) {
    throw new RangeError("A non-empty quote and valid occurrence are required")
  }

  let cursor = 0
  let index = -1
  for (let current = 0; current <= occurrence; current += 1) {
    index = content.indexOf(quote, cursor)
    if (index < 0) {
      throw new RangeError("Quote occurrence was not found")
    }
    cursor = index + quote.length
  }
  return createCanonicalAnchor(content, index, index + quote.length)
}
