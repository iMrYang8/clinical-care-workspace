import { AxiosError, type AxiosInstance } from "axios"

export const SESSION_INVALID_RESPONSE_HEADER = "X-Nightingale-Session-Invalid"

type AuthenticationRejectionHandler = () => void

let authenticationRejectionHandler: AuthenticationRejectionHandler | undefined

function axiosResponseHeader(headers: unknown, name: string): string | null {
  if (!headers || typeof headers !== "object") return null
  const getter = (headers as { get?: unknown }).get
  if (typeof getter === "function") {
    const value = getter.call(headers, name) as unknown
    return typeof value === "string" ? value : null
  }
  const values = headers as Record<string, unknown>
  const value = values[name.toLowerCase()] ?? values[name]
  return typeof value === "string" ? value : null
}

/**
 * Register the application-owned response to an invalid browser session.
 *
 * Keeping the transport independent from React avoids a hooks/fetch import
 * cycle. The application bootstrap installs the one handler that clears the
 * query cache and starts the same cross-tab termination flow as explicit
 * logout.
 */
export function setAuthenticationRejectionHandler(
  handler: AuthenticationRejectionHandler | undefined,
): void {
  authenticationRejectionHandler = handler
}

export function isAuthenticationRejection(
  response: Pick<Response, "headers" | "status">,
): boolean {
  return isAuthenticationRejectionStatus(
    response.status,
    response.headers.get(SESSION_INVALID_RESPONSE_HEADER),
  )
}

export function isAuthenticationRejectionStatus(
  status: number,
  sessionInvalidMarker: string | null | undefined,
): boolean {
  return status === 401 || (status === 403 && sessionInvalidMarker === "1")
}

/**
 * Browser-native fetch for authenticated Nightingale endpoints.
 *
 * A normal role/record-level 403 is deliberately returned to its caller. Only
 * 401 or the server's explicit session-invalid 403 marker starts termination.
 * The response body is never consumed, so callers retain ordinary Response
 * semantics.
 */
export async function authenticatedFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const response = await fetch(input, init)
  if (isAuthenticationRejection(response)) {
    authenticationRejectionHandler?.()
  }
  return response
}

/**
 * Apply the same session classification to every generated Axios SDK call.
 * Direct VoiceService calls do not necessarily pass through React Query, so
 * this interceptor belongs on the generated client's shared instance rather
 * than in a query or mutation callback.
 */
export function installAxiosAuthenticationRejectionInterceptor(
  instance: AxiosInstance,
): number {
  return instance.interceptors.response.use(undefined, (error: unknown) => {
    if (error instanceof AxiosError) {
      const status = error.response?.status
      const marker = axiosResponseHeader(
        error.response?.headers,
        SESSION_INVALID_RESPONSE_HEADER,
      )
      if (
        status !== undefined &&
        isAuthenticationRejectionStatus(status, marker)
      ) {
        authenticationRejectionHandler?.()
      }
    }
    return Promise.reject(error)
  })
}
