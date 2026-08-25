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
    try {
      if (isLoggedIn()) await authApi.logout()
    } catch {
      // The local token and cached clinical data must still be discarded.
    } finally {
      discardAccessToken()
      queryClient.clear()
      await navigate({ to: "/login", replace: true })
    }
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
