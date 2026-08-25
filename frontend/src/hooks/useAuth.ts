import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import type { MePublic } from "@/client"
import {
  authApi,
  type DemoPersona,
  discardAccessToken,
  getAccessToken,
} from "@/features/api"
import useCustomToast from "./useCustomToast"

const isLoggedIn = () => {
  return getAccessToken() !== null
}

export function roleHome(
  role: MePublic["role"],
): "/my-care" | "/patients" | "/admin" {
  if (role === "patient") return "/my-care"
  if (role === "staff" || role === "clinician") return "/patients"
  return "/admin"
}

const useAuth = () => {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()

  const meQuery = useQuery<MePublic, Error>({
    queryKey: ["auth", "me"],
    queryFn: authApi.me,
    enabled: isLoggedIn(),
    retry: false,
    staleTime: 30_000,
  })

  const loginMutation = useMutation({
    mutationFn: (persona: DemoPersona) => authApi.demoLogin(persona),
    onSuccess: async () => {
      const me = await queryClient.fetchQuery({
        queryKey: ["auth", "me"],
        queryFn: authApi.me,
      })
      await navigate({ to: roleHome(me.role) })
    },
    onError: (error) => showErrorToast(error.message),
  })

  const logout = async () => {
    const token = getAccessToken()
    // Clear PHI-bearing client state synchronously. Server logout is best
    // effort because the current bearer JWT is stateless and the API may be
    // offline exactly when a user needs to leave the clinical screen.
    discardAccessToken()
    queryClient.clear()
    await navigate({ to: "/login", replace: true })
    if (token) void authApi.logout(token).catch(() => undefined)
  }

  return {
    loginMutation,
    logout,
    user: meQuery.data,
    meQuery,
  }
}

export { isLoggedIn }
export default useAuth
