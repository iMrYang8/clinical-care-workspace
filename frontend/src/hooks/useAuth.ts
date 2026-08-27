import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"
import { useEffect, useSyncExternalStore } from "react"

import type { MePublic } from "@/client"
import {
  apiErrorMessage,
  authApi,
  httpStatus,
  type PasswordLoginInput,
} from "@/features/api"
import { roleHome } from "@/features/portalAccess"
import {
  closeVoiceDatabaseForLogout,
  purgeVoiceDatabase,
} from "@/features/voice/offlineQueue"
import useCustomToast from "./useCustomToast"

export type SessionTerminationState =
  | { phase: "idle"; error: null }
  | { phase: "terminating"; error: null; epoch: string }
  | { phase: "confirmed"; error: null; epoch: string }
  | { phase: "failed"; error: string; serverEnded: boolean; epoch: string }

const TERMINATION_PENDING_KEY = "nightingale_session_termination_pending"
const LOGOUT_EPOCH_KEY = "nightingale_logout_epoch"
const LOGOUT_CONTROL_KEY = "nightingale_session_control_v1"
const LOGOUT_CHANNEL_NAME = "nightingale-session-control-v1"
const terminationListeners = new Set<() => void>()
const CROSS_TAB_TERMINATION_TAKEOVER_MS = 6_000
let terminationTakeoverTimer: number | undefined

function clearTerminationTakeover() {
  if (terminationTakeoverTimer !== undefined) {
    window.clearTimeout(terminationTakeoverTimer)
    terminationTakeoverTimer = undefined
  }
}

function scheduleTerminationTakeover(epoch: string) {
  clearTerminationTakeover()
  terminationTakeoverTimer = window.setTimeout(() => {
    terminationTakeoverTimer = undefined
    if (
      terminationState.phase === "terminating" &&
      terminationState.epoch === epoch &&
      activeServerTermination === undefined
    ) {
      // The tab that originated the logout may have closed mid-request. Any
      // surviving tab takes over the same idempotent cookie deletion instead
      // of leaving the shared browser permanently masked as "terminating".
      void requestServerTermination()
    }
  }, CROSS_TAB_TERMINATION_TAKEOVER_MS)
}

function persistedTermination(): "unconfirmed" | "server-ended" | null {
  try {
    const value = window.localStorage.getItem(TERMINATION_PENDING_KEY)
    return value === "unconfirmed" || value === "server-ended" ? value : null
  } catch {
    return null
  }
}

const persisted = persistedTermination()
const restartEpoch = `restart-${Date.now()}`
let terminationState: SessionTerminationState = persisted
  ? persisted === "server-ended"
    ? {
        phase: "failed",
        error:
          "Your previous sign-out did not finish clearing protected offline recordings on this device.",
        serverEnded: true,
        epoch: restartEpoch,
      }
    : {
        phase: "failed",
        error:
          "Your previous sign-out was interrupted. Try again to finish securely.",
        serverEnded: false,
        epoch: restartEpoch,
      }
  : { phase: "idle", error: null }

function setTerminationState(next: SessionTerminationState) {
  terminationState = next
  try {
    if (next.phase === "idle") {
      window.localStorage.removeItem(TERMINATION_PENDING_KEY)
    } else {
      window.localStorage.setItem(
        TERMINATION_PENDING_KEY,
        next.phase === "confirmed" ||
          (next.phase === "failed" && next.serverEnded)
          ? "server-ended"
          : "unconfirmed",
      )
    }
  } catch {
    // The in-memory full-screen boundary remains active when storage is denied.
  }
  for (const listener of terminationListeners) listener()
}

function subscribeToTermination(listener: () => void) {
  terminationListeners.add(listener)
  return () => terminationListeners.delete(listener)
}

function getTerminationSnapshot() {
  return terminationState
}

function acceptConfirmedLogout(epoch: string) {
  if (
    terminationState.phase === "confirmed" &&
    terminationState.epoch === epoch
  )
    return
  setTerminationState({ phase: "confirmed", error: null, epoch })
}

type LogoutControlMessage =
  | { type: "logout-started"; epoch: string }
  | { type: "logout-failed"; epoch: string; error: string }
  | { type: "logout-confirmed"; epoch: string }

function isControlMessage(value: unknown): value is LogoutControlMessage {
  if (!value || typeof value !== "object") return false
  const candidate = value as Record<string, unknown>
  if (typeof candidate.epoch !== "string") return false
  if (
    candidate.type === "logout-started" ||
    candidate.type === "logout-confirmed"
  )
    return true
  return (
    candidate.type === "logout-failed" && typeof candidate.error === "string"
  )
}

function acceptControlMessage(message: LogoutControlMessage) {
  if (message.type === "logout-started") {
    // A new intent supersedes an earlier failed attempt. A confirmed server
    // logout is never downgraded by an out-of-order started message.
    if (terminationState.phase !== "confirmed") {
      setTerminationState({
        phase: "terminating",
        error: null,
        epoch: message.epoch,
      })
      closeVoiceDatabaseForLogout()
      scheduleTerminationTakeover(message.epoch)
    }
    return
  }
  if (message.type === "logout-failed") {
    if (
      terminationState.phase === "terminating" &&
      terminationState.epoch === message.epoch
    ) {
      setTerminationState({
        phase: "failed",
        error: message.error,
        serverEnded: false,
        epoch: message.epoch,
      })
      clearTerminationTakeover()
    }
    return
  }
  clearTerminationTakeover()
  acceptConfirmedLogout(message.epoch)
}

function persistControlMessage(message: LogoutControlMessage) {
  try {
    window.localStorage.setItem(LOGOUT_CONTROL_KEY, JSON.stringify(message))
    if (message.type === "logout-confirmed") {
      window.localStorage.setItem(LOGOUT_EPOCH_KEY, message.epoch)
    }
  } catch {
    // BroadcastChannel remains the primary live cross-tab signal.
  }
}

function publishControlMessage(message: LogoutControlMessage) {
  acceptControlMessage(message)
  persistControlMessage(message)
  logoutChannel?.postMessage(message)
}

let logoutChannel: BroadcastChannel | undefined
if (typeof window !== "undefined") {
  if (typeof BroadcastChannel !== "undefined") {
    logoutChannel = new BroadcastChannel(LOGOUT_CHANNEL_NAME)
    logoutChannel.addEventListener(
      "message",
      (event: MessageEvent<unknown>) => {
        if (isControlMessage(event.data)) acceptControlMessage(event.data)
      },
    )
  }
  window.addEventListener("storage", (event) => {
    if (event.key === LOGOUT_CONTROL_KEY && event.newValue) {
      try {
        const message: unknown = JSON.parse(event.newValue)
        if (isControlMessage(message)) acceptControlMessage(message)
      } catch {
        // Malformed local state is ignored; it never restores a session.
      }
      return
    }
    if (event.key === LOGOUT_EPOCH_KEY && event.newValue) {
      acceptConfirmedLogout(event.newValue)
    }
  })
}

function newLogoutEpoch() {
  return `${Date.now()}-${crypto.randomUUID()}`
}

let activeServerTermination: Promise<boolean> | undefined
// Once this document has completed a confirmed logout, a subsequent 401 on
// the public sign-in route is expected and must not start a second termination
// cycle. A fresh document still starts with `false`, so an expired cookie on a
// cold load continues to trigger the secure offline-recording purge.
let locallyConfirmedSignedOut = false

async function requestServerTermination(): Promise<boolean> {
  if (terminationState.phase === "confirmed") return true
  let epoch: string
  if (terminationState.phase === "terminating") {
    if (activeServerTermination) return activeServerTermination
    epoch = terminationState.epoch
  } else {
    if (terminationState.phase === "failed" && terminationState.serverEnded) {
      acceptConfirmedLogout(terminationState.epoch)
      return true
    }

    epoch = newLogoutEpoch()
    publishControlMessage({ type: "logout-started", epoch })
  }
  closeVoiceDatabaseForLogout()

  const pending = (async () => {
    try {
      await authApi.logout()
    } catch (error) {
      publishControlMessage({
        type: "logout-failed",
        epoch,
        error: `Sign-out could not be confirmed. ${apiErrorMessage(error)}`,
      })
      return false
    }
    publishControlMessage({ type: "logout-confirmed", epoch })
    return true
  })()
  activeServerTermination = pending
  try {
    return await pending
  } finally {
    if (activeServerTermination === pending) activeServerTermination = undefined
  }
}

/**
 * Route every authenticated 401 through the same cross-tab termination path
 * as an explicit logout. This clears the non-extractable voice key and its
 * ciphertext before another browser persona can enter the workspace.
 */
export function terminateUnauthorizedSession(): Promise<boolean> {
  return requestServerTermination()
}

export const trustedSessionUser = async (): Promise<MePublic | null> => {
  try {
    const user = await authApi.me()
    locallyConfirmedSignedOut = false
    return user
  } catch (error) {
    if (httpStatus(error) === 401 || httpStatus(error) === 403) {
      // The login route is not exempt: an expired HttpOnly cookie and an old
      // encrypted voice store must be terminated before another persona can
      // see the sign-in controls. Start the bounded server request but let the
      // route resolve immediately: the synchronously published termination
      // state mounts the full-screen boundary while that request is pending.
      // Awaiting here would leave a newly loaded document blank until logout
      // finished and could cancel the fetch when router navigation supersedes
      // the unresolved guard.
      if (!locallyConfirmedSignedOut) void terminateUnauthorizedSession()
    }
    return null
  }
}

const isLoggedIn = async () => (await trustedSessionUser()) !== null

export function useSecureLogout() {
  const queryClient = useQueryClient()
  const sessionTermination = useSyncExternalStore(
    subscribeToTermination,
    getTerminationSnapshot,
    getTerminationSnapshot,
  )

  const logout = async (): Promise<boolean> => {
    // requestServerTermination broadcasts before any network wait. Every tab
    // masks PHI and closes the app-managed IndexedDB handle immediately.
    queryClient.clear()
    return requestServerTermination()
  }

  return { logout, sessionTermination }
}

export function useSessionTerminationBoundary() {
  const secureLogout = useSecureLogout()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { sessionTermination } = secureLogout

  useEffect(() => {
    if (sessionTermination.phase === "idle") return
    queryClient.clear()
    closeVoiceDatabaseForLogout()
    if (sessionTermination.phase !== "confirmed") return
    let active = true
    const loginDestination = window.location.pathname.startsWith("/patient")
      ? "/patient/login"
      : "/login"

    const finish = async () => {
      try {
        await purgeVoiceDatabase()
      } catch (error) {
        if (active) {
          setTerminationState({
            phase: "failed",
            error: `Your account was signed out, but protected offline recordings are still being cleared. ${apiErrorMessage(error)}`,
            serverEnded: true,
            epoch: sessionTermination.epoch,
          })
        }
        return
      }

      // The destination's public-session guard runs during navigation. Mark
      // this document as deliberately signed out before that guard performs
      // its expected unauthenticated `/me` check, otherwise it starts another
      // logout cycle and can cancel the navigation.
      locallyConfirmedSignedOut = true
      try {
        await navigate({ to: loginDestination, replace: true })
      } catch (error) {
        if (active) {
          setTerminationState({
            phase: "failed",
            error: `Your account was signed out, but the sign-in screen did not open. ${apiErrorMessage(error)}`,
            serverEnded: true,
            epoch: sessionTermination.epoch,
          })
        }
        return
      }
      if (active) {
        setTerminationState({ phase: "idle", error: null })
        try {
          window.localStorage.removeItem(LOGOUT_CONTROL_KEY)
          window.localStorage.removeItem(LOGOUT_EPOCH_KEY)
        } catch {
          // The visible login screen is authoritative after server logout.
        }
      }
    }

    void finish()
    return () => {
      active = false
    }
  }, [navigate, queryClient, sessionTermination])

  return secureLogout
}

const useAuth = (options: { loadSession?: boolean } = {}) => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const { logout } = useSecureLogout()

  const meQuery = useQuery<MePublic, Error>({
    queryKey: ["auth", "me"],
    queryFn: authApi.me,
    retry: false,
    staleTime: 30_000,
    enabled: options.loadSession !== false,
  })

  const passwordLoginMutation = useMutation({
    mutationFn: (input: PasswordLoginInput) => authApi.passwordLogin(input),
    onSuccess: async () => {
      const me = await queryClient.fetchQuery({
        queryKey: ["auth", "me"],
        queryFn: authApi.me,
      })
      locallyConfirmedSignedOut = false
      await navigate({ to: roleHome(me.role) })
    },
    onError: (error) => showErrorToast(apiErrorMessage(error)),
  })

  return {
    passwordLoginMutation,
    logout,
    user: meQuery.data,
    meQuery,
  }
}

export { isLoggedIn, roleHome }
export default useAuth
