import type { ConflictPublic } from "@/client"

type AllergyCategory = NonNullable<ConflictPublic["left_allergy_category"]>

export function allergyCategoryLabel(
  category: AllergyCategory | null | undefined,
): AllergyCategory | "unavailable" {
  return category ?? "unavailable"
}
