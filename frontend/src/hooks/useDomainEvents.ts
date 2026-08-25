import { useQueryClient } from "@tanstack/react-query"
import { useEffect } from "react"

import { streamDomainEvents } from "@/features/api"

function waitForReconnect(signal: AbortSignal, milliseconds: number) {
  return new Promise<void>((resolve) => {
    const finish = () => {
      window.clearTimeout(timer)
      signal.removeEventListener("abort", finish)
      resolve()
    }
    const timer = window.setTimeout(finish, milliseconds)
    signal.addEventListener("abort", finish, { once: true })
  })
}

export function useDomainEvents(enabled: boolean): void {
  const queryClient = useQueryClient()

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    let lastEventId: number | undefined

    const connect = async () => {
      let retryDelay = 1_000
      while (!controller.signal.aborted) {
        try {
          await streamDomainEvents(
            (event) => {
              if (event.id !== null && Number.isFinite(event.id)) {
                lastEventId = event.id
              }
              retryDelay = 1_000
              void queryClient.invalidateQueries({
                predicate: (query) =>
                  query.queryKey[0] === "patients" ||
                  query.queryKey[0] === "entries",
              })
            },
            { signal: controller.signal, lastEventId },
          )
        } catch {
          if (controller.signal.aborted) return
        }
        if (controller.signal.aborted) return
        await waitForReconnect(controller.signal, retryDelay)
        retryDelay = Math.min(retryDelay * 2, 15_000)
      }
    }

    void connect()
    return () => controller.abort()
  }, [enabled, queryClient])
}
