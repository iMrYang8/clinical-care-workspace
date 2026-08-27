import { Appearance } from "@/components/Common/Appearance"
import { Brand } from "@/components/Nightingale/Brand"

interface AuthLayoutProps {
  children: React.ReactNode
}

export function AuthLayout({ children }: AuthLayoutProps) {
  return (
    <div className="grid min-h-svh bg-background lg:grid-cols-[minmax(0,1.1fr)_minmax(28rem,0.9fr)]">
      <div className="relative hidden overflow-hidden bg-foreground p-12 text-background lg:flex lg:flex-col lg:justify-between">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_20%,rgba(45,212,191,0.24),transparent_36%),radial-gradient(circle_at_80%_75%,rgba(56,189,248,0.14),transparent_38%)]" />
        <div className="relative">
          <Brand asLink={false} className="[&_span]:text-background" />
        </div>
        <div className="relative max-w-xl space-y-5">
          <h1 className="font-serif text-4xl font-semibold leading-tight tracking-tight">
            Welcome to Nightingale
          </h1>
          <p className="text-lg leading-8 text-background/80">
            Sign in to access patient records, care notes, and team
            collaboration.
          </p>
        </div>
        <p className="relative text-xs text-background/60">
          Nightingale Clinical Workspace
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
        <p className="text-center text-xs text-muted-foreground">
          Secure access for patients and care teams
        </p>
      </div>
    </div>
  )
}
