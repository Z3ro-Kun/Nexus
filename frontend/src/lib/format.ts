export function shortId(id: string, length = 8): string {
  return id.length > length ? `${id.slice(0, length)}…` : id;
}

export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const seconds = Math.floor(ms / 1000);
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m > 0 ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour12: false });
}

/** "just now", "5 min ago", "yesterday", or a date for anything older than a week. */
export function formatRelative(iso: string, now = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const seconds = Math.round((now - t) / 1000);
  if (seconds < 45) return "just now";
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" });
  if (seconds < 3600) return rtf.format(-Math.round(seconds / 60), "minute");
  if (seconds < 86_400) return rtf.format(-Math.round(seconds / 3600), "hour");
  if (seconds < 7 * 86_400) return rtf.format(-Math.round(seconds / 86_400), "day");
  return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function truncate(text: string, max = 160): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** A short, safe text rendering of an unknown JSON value. */
export function preview(value: unknown, max = 120): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return truncate(value, max);
  try {
    return truncate(JSON.stringify(value), max);
  } catch {
    return "";
  }
}
