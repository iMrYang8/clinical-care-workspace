import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "@tanstack/react-router"

import type { MePublic } from "@/client"
import { authApi, type DemoPersona } from "@/features/api"
import { purgeVoiceDatabase } from "@/features/voice/offlineQueue"
import useCustomToast from "./useCustomToast"

const isLoggedIn = async () => {
  try {
    await authApi.me()
    return true
  } catch {
    return false
  }
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
    // Clear PHI-bearing query and IndexedDB state before changing screens.
    queryClient.clear()
    await purgeVoiceDatabase()
    await authApi.logout().catch(() => undefined)
    await navigate({ to: "/login", replace: true })
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
