import { afterEach, describe, expect, it, vi } from "vitest"

import {
  authenticatedFetch,
  SESSION_INVALID_RESPONSE_HEADER,
  setAuthenticationRejectionHandler,
} from "./authenticatedFetch"

afterEach(() => {
  setAuthenticationRejectionHandler(undefined)
  vi.unstubAllGlobals()
})

describe("authenticated browser fetch", () => {
  it("starts session termination for every 401 without consuming the body", async () => {
    const handler = vi.fn()
    const response = new Response(JSON.stringify({ detail: "expired" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    })
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response))
    setAuthenticationRejectionHandler(handler)

    const returned = await authenticatedFetch("/api/v1/voice/chunk", {
      credentials: "same-origin",
      method: "PUT",
    })

    expect(returned).toBe(response)
    expect(handler).toHaveBeenCalledOnce()
    await expect(returned.json()).resolves.toEqual({ detail: "expired" })
  })

  it("starts termination for an explicitly marked authentication 403", async () => {
    const handler = vi.fn()
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Inactive membership" }), {
          status: 403,
          headers: { [SESSION_INVALID_RESPONSE_HEADER]: "1" },
        }),
      ),
    )
    setAuthenticationRejectionHandler(handler)

    await authenticatedFetch("/api/v1/events/stream")

    expect(handler).toHaveBeenCalledOnce()
  })

  it("preserves an ordinary RBAC 403 without logging out the user", async () => {
    const handler = vi.fn()
    const response = new Response(
      JSON.stringify({ detail: "Audio access is not permitted" }),
      { status: 403 },
    )
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response))
    setAuthenticationRejectionHandler(handler)

    const returned = await authenticatedFetch(
      "/api/v1/voice/sessions/session-1/audio",
    )

    expect(returned).toBe(response)
    expect(handler).not.toHaveBeenCalled()
  })
})
