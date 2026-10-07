/**
 * Minimal client-side routing on the History API: "/" (objective) and "/runs/:runId"
 * (execution view). The Vite dev and preview servers fall back to index.html for any path,
 * so a refreshed /runs/:runId loads the app, which reconstructs the run from the backend.
 */

import { useSyncExternalStore } from "react";

export type Route = { page: "objective" } | { page: "run"; runId: string } | { page: "not_found" };

const RUN_PATH = /^\/runs\/([^/]+)\/?$/;

export function matchRoute(pathname: string): Route {
  if (pathname === "/" || pathname === "") return { page: "objective" };
  const match = RUN_PATH.exec(pathname);
  if (match) return { page: "run", runId: decodeURIComponent(match[1]) };
  return { page: "not_found" };
}

const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  window.addEventListener("popstate", listener);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("popstate", listener);
  };
}

export function navigate(path: string) {
  if (window.location.pathname === path) return;
  window.history.pushState(null, "", path);
  listeners.forEach((listener) => listener());
}

/**
 * Props for an in-app <a>: a plain left click navigates without a reload; Ctrl/Cmd/Shift/
 * middle clicks keep the browser's behaviour (new tab, new window).
 */
export function linkTo(path: string) {
  return {
    href: path,
    onClick: (event: { button: number; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean; altKey: boolean; preventDefault: () => void }) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      navigate(path);
    },
  };
}

export function runPath(runId: string) {
  return `/runs/${encodeURIComponent(runId)}`;
}

export function useRoute(): Route {
  const pathname = useSyncExternalStore(subscribe, () => window.location.pathname);
  return matchRoute(pathname);
}
