import { useSyncExternalStore } from "react";

const QUERY = "(prefers-reduced-motion: reduce)";

function subscribe(onChange: () => void) {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const mql = window.matchMedia(QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

/** SVG SMIL animations ignore CSS, so animated SVG elements check this before rendering. */
export function usePrefersReducedMotion(): boolean {
  return useSyncExternalStore(subscribe, () => typeof window.matchMedia === "function" && window.matchMedia(QUERY).matches);
}
