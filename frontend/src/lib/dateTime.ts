export const SINGAPORE_TIME_ZONE = "Asia/Singapore"

const dateTimeFormatter = new Intl.DateTimeFormat("en-SG", {
  timeZone: SINGAPORE_TIME_ZONE,
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
})

const dateFormatter = new Intl.DateTimeFormat("en-SG", {
  timeZone: SINGAPORE_TIME_ZONE,
  year: "numeric",
  month: "short",
  day: "2-digit",
})

export function formatSingaporeDateTime(value: string | Date): string {
  return `${dateTimeFormatter.format(new Date(value))} SGT`
}

export function formatSingaporeDate(value: string | Date): string {
  // Date-only values must not shift a day when interpreted in another locale.
  const normalized =
    typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
      ? `${value}T00:00:00+08:00`
      : value
  return dateFormatter.format(new Date(normalized))
}
