import { createFileRoute } from "@tanstack/react-router"

import { PatientSafeCareNote } from "@/components/Patient/PatientSafeCareNote"

export const Route = createFileRoute("/patient/my-care/")({
  component: PatientMyCare,
  head: () => ({ meta: [{ title: "My Care · Nightingale" }] }),
})

function PatientMyCare() {
  return <PatientSafeCareNote />
}
