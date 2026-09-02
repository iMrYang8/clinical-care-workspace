import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"

import { type DomainEvent, streamDomainEvents } from "@/features/api"

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

export function useDomainEvents(
  enabled: boolean,
  clinicId: string,
  onEvent?: (event: DomainEvent) => void,
): void {
  const queryClient = useQueryClient()
  const onEventRef = useRef(onEvent)

  useEffect(() => {
    onEventRef.current = onEvent
  }, [onEvent])

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    const cursorKey = `nightingale_event_cursor:${clinicId}`
    const storedCursor = Number(window.sessionStorage.getItem(cursorKey))
    let lastEventId =
      Number.isFinite(storedCursor) && storedCursor > 0
        ? storedCursor
        : undefined
    let invalidationTimer: number | undefined

    const scheduleInvalidation = () => {
      if (invalidationTimer !== undefined) return
      invalidationTimer = window.setTimeout(() => {
        invalidationTimer = undefined
        void queryClient.invalidateQueries({
          predicate: (query) =>
            query.queryKey[0] === "patients" || query.queryKey[0] === "entries",
        })
      }, 100)
    }

    const connect = async () => {
      let retryDelay = 1_000
      while (!controller.signal.aborted) {
        try {
          await streamDomainEvents(
            (event) => {
              if (typeof event.id === "number" && Number.isFinite(event.id)) {
                lastEventId = event.id
                window.sessionStorage.setItem(cursorKey, String(event.id))
              }
              retryDelay = 1_000
              onEventRef.current?.(event)
              if (event.event !== "editor_presence") scheduleInvalidation()
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
    return () => {
      controller.abort()
      if (invalidationTimer !== undefined) {
        window.clearTimeout(invalidationTimer)
      }
    }
  }, [clinicId, enabled, queryClient])
}
