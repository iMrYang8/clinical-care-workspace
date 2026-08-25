import { ReactQueryDevtools } from "@tanstack/react-query-devtools"
import { createRootRoute, HeadContent, Outlet } from "@tanstack/react-router"
import { TanStackRouterDevtools } from "@tanstack/react-router-devtools"
import ErrorComponent from "@/components/Common/ErrorComponent"
import NotFound from "@/components/Common/NotFound"
import { SessionTerminationScreen } from "@/components/Nightingale/SessionTerminationScreen"
import { useSessionTerminationBoundary } from "@/hooks/useAuth"

export const Route = createRootRoute({
  component: RootComponent,
  notFoundComponent: () => <NotFound />,
  errorComponent: () => <ErrorComponent />,
})

function RootComponent() {
  const { logout, sessionTermination } = useSessionTerminationBoundary()

  if (sessionTermination.phase !== "idle") {
    return (
      <SessionTerminationScreen onRetry={logout} state={sessionTermination} />
    )
  }

  return (
    <>
      <HeadContent />
      <a
        className="sr-only z-[100] rounded-md bg-white px-4 py-3 text-teal-900 shadow focus:not-sr-only focus:fixed focus:left-4 focus:top-4"
        href="#main-content"
      >
        Skip to care note
      </a>
      <Outlet />
      {import.meta.env.DEV && (
        <>
          <TanStackRouterDevtools position="bottom-right" />
          <ReactQueryDevtools initialIsOpen={false} />
        </>
      )}
    </>
  )
}
