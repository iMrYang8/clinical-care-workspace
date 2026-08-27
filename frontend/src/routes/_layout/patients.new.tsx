import { useMutation } from "@tanstack/react-query"
import { createFileRoute, useNavigate } from "@tanstack/react-router"
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  LoaderCircle,
} from "lucide-react"
import { type FormEvent, useEffect, useState } from "react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  clinicalApi,
  type PatientDuplicateCheck,
  type PatientIdentityInput,
} from "@/features/api"
import { patientRouteReferenceFromId } from "@/features/routeReferences"
import useAuth, { roleHome } from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/patients/new")({
  component: AddPatientPage,
  head: () => ({ meta: [{ title: "Add patient · Nightingale" }] }),
})

const emptyForm: PatientIdentityInput = {
  display_name: "",
  date_of_birth: "",
  medical_record_number: "",
  identity_document_type: "nric_fin",
  identity_document_number: "",
}

function AddPatientPage() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState(emptyForm)
  const [review, setReview] = useState<PatientDuplicateCheck | null>(null)
  const [verified, setVerified] = useState(false)
  const allowed = user?.role === "staff" || user?.role === "clinician"

  useEffect(() => {
    if (user && !allowed)
      void navigate({ to: roleHome(user.role), replace: true })
  }, [allowed, navigate, user])

  const check = useMutation({
    mutationFn: () => clinicalApi.duplicateCheck(form),
    onSuccess: (result) => setReview(result),
  })
  const create = useMutation({
    mutationFn: () =>
      clinicalApi.createPatientRecord({
        ...form,
        duplicate_confirmation_token:
          review?.status === "possible_match" && verified
            ? (review.duplicate_confirmation_token ?? undefined)
            : undefined,
      }),
    onSuccess: (patient) =>
      navigate({
        to: "/patients/$patientId",
        params: { patientId: patientRouteReferenceFromId(patient.id) },
      }),
  })

  if (!user || !allowed) {
    return <LoaderCircle className="mx-auto mt-24 animate-spin text-primary" />
  }

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!review) check.mutate()
    else create.mutate()
  }

  const update = (key: keyof PatientIdentityInput, value: string) => {
    setForm((current) => ({ ...current, [key]: value }))
    setReview(null)
    setVerified(false)
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <Button variant="ghost" onClick={() => history.back()}>
        <ArrowLeft className="size-4" /> Back to patients
      </Button>
      <Card>
        <CardHeader>
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
            Patient registration
          </p>
          <CardTitle className="font-serif text-3xl">Add patient</CardTitle>
          <p className="text-sm leading-6 text-muted-foreground">
            Create one shared clinical record for your clinic. Identity details
            are encrypted and are used only to prevent duplicate records.
          </p>
        </CardHeader>
        <CardContent>
          <form className="space-y-6" onSubmit={submit}>
            <section className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2 sm:col-span-2">
                <Label htmlFor="patient-name">Full name</Label>
                <Input
                  id="patient-name"
                  required
                  maxLength={255}
                  value={form.display_name}
                  onChange={(e) => update("display_name", e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="patient-dob">Date of birth</Label>
                <Input
                  id="patient-dob"
                  required
                  type="date"
                  value={form.date_of_birth}
                  onChange={(e) => update("date_of_birth", e.target.value)}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="patient-mrn">Medical record number</Label>
                <Input
                  id="patient-mrn"
                  required
                  minLength={3}
                  maxLength={80}
                  value={form.medical_record_number}
                  onChange={(e) =>
                    update(
                      "medical_record_number",
                      e.target.value.toUpperCase(),
                    )
                  }
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="identity-type">Identity document</Label>
                <select
                  id="identity-type"
                  className="h-10 w-full rounded-md border border-input bg-background px-3 text-sm"
                  value={form.identity_document_type}
                  onChange={(e) =>
                    update("identity_document_type", e.target.value)
                  }
                >
                  <option value="nric_fin">NRIC / FIN</option>
                  <option value="passport">Passport</option>
                  <option value="other">Other</option>
                </select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="identity-number">Document number</Label>
                <Input
                  id="identity-number"
                  required
                  minLength={3}
                  maxLength={80}
                  value={form.identity_document_number}
                  onChange={(e) =>
                    update(
                      "identity_document_number",
                      e.target.value.toUpperCase(),
                    )
                  }
                />
              </div>
            </section>

            {review?.status === "clear" && (
              <Alert className="border-success/40 bg-success-muted text-success-muted-foreground">
                <CheckCircle2 className="size-4" />
                <AlertTitle>No matching patient found</AlertTitle>
                <AlertDescription>
                  Review the details, then create the clinical record.
                </AlertDescription>
              </Alert>
            )}
            {review?.status === "exact_match" && (
              <Alert className="border-critical/40 bg-critical-muted text-critical-muted-foreground">
                <AlertTriangle className="size-4" />
                <AlertTitle>Patient already exists</AlertTitle>
                <AlertDescription>
                  The medical record number or identity document matches an
                  existing patient. Open that record instead.
                </AlertDescription>
              </Alert>
            )}
            {review?.status === "possible_match" && (
              <Alert className="border-warning/40 bg-warning-muted text-warning-muted-foreground">
                <AlertTriangle className="size-4" />
                <AlertTitle>Possible duplicate patient</AlertTitle>
                <AlertDescription className="space-y-3">
                  {review.candidates.map((candidate) => (
                    <p key={candidate.patient_id}>
                      {candidate.display_name} · DOB{" "}
                      {candidate.date_of_birth ?? "not recorded"} · MRN{" "}
                      {candidate.medical_record_number ?? "not recorded"} · ID{" "}
                      {candidate.masked_identity_document ?? "not recorded"}
                    </p>
                  ))}
                  <label className="flex items-start gap-2 font-medium">
                    <input
                      type="checkbox"
                      checked={verified}
                      onChange={(event) => setVerified(event.target.checked)}
                    />
                    I checked the physical identity document and confirmed this
                    is a different person.
                  </label>
                </AlertDescription>
              </Alert>
            )}
            {(check.isError || create.isError) && (
              <Alert variant="destructive">
                <AlertDescription>
                  The patient record was not created. Review the details and try
                  again.
                </AlertDescription>
              </Alert>
            )}
            <Button
              className="min-h-11"
              disabled={
                check.isPending ||
                create.isPending ||
                review?.status === "exact_match" ||
                (review?.status === "possible_match" && !verified)
              }
              type="submit"
            >
              {(check.isPending || create.isPending) && (
                <LoaderCircle className="animate-spin" />
              )}
              {!review ? "Check for duplicate" : "Create patient record"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
