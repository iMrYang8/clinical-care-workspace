import { AxiosError } from "axios"
import { afterEach, describe, expect, it, vi } from "vitest"

import { AuthService } from "@/client"
import { apiErrorMessage, authApi, streamDomainEvents } from "./api"

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("clinic authentication transport", () => {
  it("uppercases the clinic code without hiding invalid whitespace", async () => {
    const login = vi
      .spyOn(AuthService, "passwordLogin")
      .mockResolvedValue({} as never)

    await authApi.passwordLogin({
      clinicCode: " nightingale ",
      email: " Care.Team@Example.COM ",
      password: "  password whitespace remains  ",
    })

    expect(login).toHaveBeenCalledWith({
      headers: { "X-Clinic-Code": " NIGHTINGALE " },
      body: {
        username: "care.team@example.com",
        password: "  password whitespace remains  ",
      },
    })
  })
})

describe("product-safe request errors", () => {
  function responseError(status: number, detail: string) {
    return new AxiosError(
      "Request failed with a technical status",
      "ERR_BAD_RESPONSE",
      undefined,
      undefined,
      {
        config: {} as never,
        data: { detail },
        headers: {},
        status,
        statusText: "Error",
      },
    )
  }

  it("maps backend details to clinical product language", () => {
    expect(apiErrorMessage(responseError(400, "If-Match is required"))).toBe(
      "Review the information you entered and try again.",
    )
    expect(
      apiErrorMessage(responseError(403, "RLS tenant policy rejected UUID")),
    ).toBe("Your account does not have access to this action.")
    expect(
      apiErrorMessage(responseError(500, "provider model error_code")),
    ).toBe("Nightingale could not complete this request. Try again.")
  })

  it("does not expose transport or local exception messages", () => {
    expect(apiErrorMessage(new AxiosError("Network Error"))).toBe(
      "Nightingale could not reach the clinic service. Check your connection and try again.",
    )
    expect(
      apiErrorMessage(new Error("raw IndexedDB implementation detail")),
    ).toBe("Nightingale could not complete this request. Try again.")
  })
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
