import type { ProvenanceResolved } from "@/client"

export type SupportState = NonNullable<ProvenanceResolved["support_state"]>

/**
 * Resolve the badge from immutable source identity before presentation hints.
 * A pointer to a non-current version always fails closed as historical even if
 * a stale projection still says `current`; `superseded` remains the strongest
 * state and is never downgraded by a client-side fallback.
 */
export function authoritativeSupportState(
  provenanceState: SupportState | null | undefined,
  declaredState: SupportState | null | undefined,
  sourceVersionIsCurrent: boolean,
): SupportState {
  if (provenanceState === "superseded" || declaredState === "superseded") {
    return "superseded"
  }
  if (!sourceVersionIsCurrent) return "historical"
  return provenanceState ?? declaredState ?? "current"
}
