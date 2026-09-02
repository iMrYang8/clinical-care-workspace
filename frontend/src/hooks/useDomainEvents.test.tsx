import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, render, waitFor } from "@testing-library/react"
import type { ReactNode } from "react"
import { describe, expect, it, vi } from "vitest"

import type { DomainEvent } from "@/features/api"
import { useDomainEvents } from "./useDomainEvents"

const mocks = vi.hoisted(() => ({
  streamDomainEvents: vi.fn(),
}))

vi.mock("@/features/api", async (loadOriginal) => {
  const original = await loadOriginal<typeof import("@/features/api")>()
  return { ...original, streamDomainEvents: mocks.streamDomainEvents }
})

function Harness({ onEvent }: { onEvent: (event: DomainEvent) => void }) {
  useDomainEvents(true, "clinic-1", onEvent)
  return null
}

describe("useDomainEvents editor presence", () => {
  it("delivers editor_presence without invalidating patient timeline queries", async () => {
    let deliver: ((event: DomainEvent) => void) | undefined
    mocks.streamDomainEvents.mockImplementation(
      (
        onEvent: (event: DomainEvent) => void,
        options: { signal: AbortSignal },
      ) => {
        deliver = onEvent
        return new Promise<void>((resolve) => {
          options.signal.addEventListener("abort", () => resolve(), {
            once: true,
          })
        })
      },
    )
    const onEvent = vi.fn()
    const queryClient = new QueryClient()
    const invalidate = vi.spyOn(queryClient, "invalidateQueries")
    const view = render(<Harness onEvent={onEvent} />, {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={queryClient}>
          {children}
        </QueryClientProvider>
      ),
    })
    await waitFor(() => expect(deliver).toBeTypeOf("function"))
    const event: DomainEvent = {
      id: 31,
      event: "editor_presence",
      data: {
        aggregate_type: "entry",
        aggregate_id: "entry-1",
        payload: {
          clinic_id: "clinic-1",
          patient_id: "patient-1",
          entry_id: "entry-1",
          entry_version_id: "version-1",
          actor_id: "actor-1",
          actor_role: "clinician",
          actor_display_name: "Dr Lee",
          expires_at: "2099-01-01T00:00:00Z",
        },
      },
    }
    act(() => deliver?.(event))

    expect(onEvent).toHaveBeenCalledWith(event)
    expect(invalidate).not.toHaveBeenCalled()
    view.unmount()
  })
})
