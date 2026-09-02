import type {
  ClinicOnboardingCreate,
  ClinicPreflightPublic,
  PatientDetailPublic,
  PatientTimelineEntry,
  PlatformClinicPublic,
  PlatformMePublic,
} from "@/client"

export type PlatformMe = PlatformMePublic

export type PlatformClinic = PlatformClinicPublic

export type PlatformPatient = PatientDetailPublic

export type PlatformTimelineEntry = PatientTimelineEntry

/** UI form requires every server-defaulted choice to be made explicitly. */
export type ClinicOnboardingInput = Required<
  Omit<ClinicOnboardingCreate, "formulary_template">
>

export type ClinicPreflight = ClinicPreflightPublic

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1/platform${path}`, {
    credentials: "same-origin",
    ...init,
  })
  if (!response.ok)
    throw new Error(`Platform request failed (${response.status})`)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const platformApi = {
  login: (email: string, password: string) =>
    request<{ access_token: string }>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email.trim().toLowerCase(), password }),
    }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<PlatformMe>("/auth/me"),
  clinics: async () =>
    (await request<{ data: PlatformClinic[] }>("/clinics")).data,
  patients: (clinicCode: string) =>
    request<PlatformPatient[]>(
      `/clinics/${encodeURIComponent(clinicCode)}/patients`,
    ),
  timeline: async (clinicCode: string, patientId: string) =>
    (
      await request<{ data: PlatformTimelineEntry[] }>(
        `/clinics/${encodeURIComponent(clinicCode)}/patients/${encodeURIComponent(patientId)}/timeline`,
      )
    ).data,
  preflightClinic: (body: ClinicOnboardingInput) =>
    request<ClinicPreflight>("/clinics/preflight", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  onboardClinic: (body: ClinicOnboardingInput) =>
    request<PlatformClinic>("/clinics/onboard", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": crypto.randomUUID(),
      },
      body: JSON.stringify(body),
    }),
}
