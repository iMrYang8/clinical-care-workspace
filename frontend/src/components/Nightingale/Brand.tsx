import { Link } from "@tanstack/react-router"
import { HeartPulse } from "lucide-react"

import { cn } from "@/lib/utils"

type BrandProps = {
  compact?: boolean
  asLink?: boolean
  className?: string
}

export function Brand({
  compact = false,
  asLink = true,
  className,
}: BrandProps) {
  const mark = (
    <div className={cn("flex min-w-0 items-center gap-3", className)}>
      <span className="grid size-10 shrink-0 place-items-center rounded-2xl bg-teal-700 text-white shadow-sm shadow-teal-900/20">
        <HeartPulse aria-hidden="true" className="size-5" />
      </span>
      {!compact && (
        <span className="min-w-0 leading-tight">
          <span className="block truncate font-serif text-lg font-semibold tracking-tight text-slate-950">
            Nightingale
          </span>
          <span className="block truncate text-[0.65rem] font-semibold uppercase tracking-[0.2em] text-teal-700">
            Care note
          </span>
        </span>
      )}
    </div>
  )
  return asLink ? (
    <Link aria-label="Nightingale home" to="/">
      {mark}
    </Link>
  ) : (
    mark
  )
}
