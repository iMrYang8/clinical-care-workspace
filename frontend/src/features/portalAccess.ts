import type { MePublic } from "@/client"

export type PortalKind = "clinical" | "patient"
export type PortalRoute = "/patient/my-care" | "/patients" | "/admin" | "/login"

export function roleHome(role: MePublic["role"]): PortalRoute {
  if (role === "patient") return "/patient/my-care"
  if (role === "staff" || role === "clinician") return "/patients"
  if (role === "admin") return "/admin"
  return "/login"
}

/**
 * Determines whether a trusted /auth/me role belongs in the requested portal.
 * Worker sessions are not interactive patient sessions; individual clinical
 * routes continue to enforce their narrower role permissions.
 */
export function portalRedirectForRole(
  role: MePublic["role"],
  portal: PortalKind,
): PortalRoute | null {
  if (portal === "patient") {
    return role === "patient" ? null : roleHome(role)
  }
  if (role === "patient" || role === "worker") return roleHome(role)
  return null
}
