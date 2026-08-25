import { afterEach, describe, expect, it, vi } from "vitest"

import { streamDomainEvents } from "./api"

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("domain event transport", () => {
  it("uses the same-origin cookie and Last-Event-ID without browser tokens", async () => {
    const bytes = new TextEncoder().encode(
      'id: 42\nevent: entry.updated\ndata: {"aggregate_type":"entry","aggregate_id":"entry-1","payload":{}}\n\n',
    )
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes)
        controller.close()
      },
    })
    const fetchMock = vi.fn().mockResolvedValue(new Response(body))
    vi.stubGlobal("fetch", fetchMock)
    const received: Array<{ id: number | null; event: string }> = []

    await streamDomainEvents(
      (event) => received.push({ id: event.id, event: event.event }),
      { signal: new AbortController().signal, lastEventId: 41 },
    )

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toMatch(/\/api\/v1\/events\/stream$/)
    expect(url).not.toContain("token")
    expect(init.credentials).toBe("same-origin")
    expect(init.headers).toMatchObject({
      "Last-Event-ID": "41",
    })
    expect(init.headers).not.toHaveProperty("Authorization")
    expect(received).toEqual([{ id: 42, event: "entry.updated" }])
  })
})
