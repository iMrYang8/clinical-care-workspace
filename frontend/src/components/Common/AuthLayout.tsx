import { Activity, LockKeyhole, ShieldCheck } from "lucide-react"

import { Appearance } from "@/components/Common/Appearance"
import { Brand } from "@/components/Nightingale/Brand"

interface AuthLayoutProps {
  children: React.ReactNode
}

export function AuthLayout({ children }: AuthLayoutProps) {
  return (
    <div className="grid min-h-svh bg-[#f7faf9] lg:grid-cols-[minmax(0,1.1fr)_minmax(28rem,0.9fr)]">
      <div className="relative hidden overflow-hidden bg-teal-950 p-12 text-white lg:flex lg:flex-col lg:justify-between">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_20%,rgba(45,212,191,0.24),transparent_36%),radial-gradient(circle_at_80%_75%,rgba(56,189,248,0.14),transparent_38%)]" />
        <div className="relative">
          <Brand asLink={false} className="[&_span]:text-white" />
        </div>
        <div className="relative max-w-2xl space-y-7">
          <p className="text-sm font-semibold uppercase tracking-[0.22em] text-teal-200">
            Evidence before assumption
          </p>
          <h1 className="font-serif text-5xl font-semibold leading-[1.06] tracking-tight">
            One calm place for the care team to see what matters now.
          </h1>
          <p className="max-w-xl text-lg leading-8 text-teal-50/80">
            Synthetic demo records, immutable source anchors, and role-scoped
            collaboration—designed for a trustworthy clinical review flow.
          </p>
          <div className="grid gap-3 sm:grid-cols-3">
            {[
              [ShieldCheck, "Clinic scoped"],
              [LockKeyhole, "Encrypted fields"],
              [Activity, "Source linked"],
            ].map(([Icon, label]) => (
              <div
                className="flex items-center gap-2 rounded-2xl border border-white/10 bg-white/5 p-3 text-sm text-teal-50"
                key={String(label)}
              >
                <Icon aria-hidden="true" className="size-4 text-teal-300" />
                {label as string}
              </div>
            ))}
          </div>
        </div>
        <p className="relative text-xs text-teal-100/60">
          Nightingale local fixture · All people and records are synthetic
        </p>
      </div>
      <div className="flex flex-col gap-5 p-5 sm:p-8 md:p-12">
        <div className="flex items-center justify-between lg:justify-end">
          <Brand asLink={false} className="lg:hidden" />
          <Appearance />
        </div>
        <div className="flex flex-1 items-center justify-center">
          <div className="w-full max-w-lg">{children}</div>
        </div>
        <p className="text-center text-xs text-slate-500">
          Secure clinic access · Local demos use synthetic records only
        </p>
      </div>
    </div>
  )
}
