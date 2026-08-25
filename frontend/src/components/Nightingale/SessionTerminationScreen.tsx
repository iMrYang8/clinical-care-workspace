import { LoaderCircle, ShieldAlert } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import type { SessionTerminationState } from "@/hooks/useAuth"

type SessionTerminationScreenProps = {
  onRetry: () => Promise<boolean>
  state: Exclude<SessionTerminationState, { phase: "idle" }>
}

export function SessionTerminationScreen({
  onRetry,
  state,
}: SessionTerminationScreenProps) {
  const pending = state.phase === "terminating" || state.phase === "confirmed"
  const serverEnded = state.phase === "failed" && state.serverEnded
  return (
    <main
      className="grid min-h-svh place-items-center bg-slate-950 p-6"
      data-testid="session-termination-boundary"
    >
      <Alert
        className="max-w-xl border-amber-300/40 bg-white text-slate-950 shadow-2xl"
        role={pending ? "status" : "alert"}
      >
        {pending ? (
          <LoaderCircle className="animate-spin text-teal-700" />
        ) : (
          <ShieldAlert className="text-amber-700" />
        )}
        <h1 className="col-start-2 min-h-4 text-lg font-medium tracking-tight">
          {pending
            ? "Securing this device"
            : serverEnded
              ? "Local cleanup incomplete"
              : "Session termination incomplete"}
        </h1>
        <AlertDescription className="space-y-4 text-slate-700">
          {pending ? (
            <p>
              {state.phase === "confirmed"
                ? "The server logout is confirmed. Nightingale is closing this tab's encrypted offline store before opening the login screen."
                : "Care information is hidden while Nightingale waits for the server to delete the secure session cookie."}
            </p>
          ) : (
            <>
              <p>{state.error}</p>
              {serverEnded ? (
                <p className="font-medium text-slate-950">
                  The server session is logged out, but local encrypted data may
                  remain. Care information stays hidden until cleanup succeeds.
                </p>
              ) : (
                <p className="font-medium text-slate-950">
                  You are not logged out yet. Care information remains hidden;
                  retry before leaving this shared device.
                </p>
              )}
              <Button onClick={() => void onRetry()}>
                {serverEnded ? "Retry local cleanup" : "Retry secure logout"}
              </Button>
            </>
          )}
        </AlertDescription>
      </Alert>
    </main>
  )
}
