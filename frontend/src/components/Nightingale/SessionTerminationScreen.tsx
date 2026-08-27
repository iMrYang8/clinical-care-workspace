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
      className="grid min-h-svh place-items-center bg-background p-6"
      data-testid="session-termination-boundary"
    >
      <Alert
        className="max-w-xl border-warning/40 bg-card text-card-foreground shadow-2xl"
        role={pending ? "status" : "alert"}
      >
        {pending ? (
          <LoaderCircle className="animate-spin text-primary" />
        ) : (
          <ShieldAlert className="text-warning" />
        )}
        <h1 className="col-start-2 min-h-4 text-lg font-medium tracking-tight">
          {pending
            ? "Securing this device"
            : serverEnded
              ? "Local cleanup incomplete"
              : "Session termination incomplete"}
        </h1>
        <AlertDescription className="space-y-4 text-muted-foreground">
          {pending ? (
            <p>
              {state.phase === "confirmed"
                ? "Sign out is confirmed. Nightingale is finishing local cleanup before opening the login screen."
                : "Care information is hidden while Nightingale securely signs you out."}
            </p>
          ) : (
            <>
              <p>{state.error}</p>
              {serverEnded ? (
                <p className="font-medium text-foreground">
                  Your account is signed out, but local cleanup still needs to
                  finish. Care information stays hidden until it succeeds.
                </p>
              ) : (
                <p className="font-medium text-foreground">
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
