export type PlatformMe = {
  user_id: string
  platform_admin_id: string
  email: string
  full_name: string | null
  role: "platform_admin"
}

export type PlatformClinic = {
  id: string
  code: string
  name: string
  member_count: number
  patient_count: number
}

export type PlatformPatient = {
  id: string
  display_name: string
  date_of_birth: string | null
  medical_record_number: string | null
  masked_identity_document: string | null
  portal_access_state: string
  status: string
}

export type PlatformTimelineEntry = {
  id: string
  title: string
  content: string
  section: string
  entry_type: string
  occurred_at: string
  version_no: number
}

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
}
