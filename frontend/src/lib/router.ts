/**
 * Client-side routing on the History API: "/" (chat), "/c/:conversationId", "/runs/:runId", and "/objective".
 */

import { useSyncExternalStore } from "react";

export type Route =
  | { page: "chat"; conversationId?: string }
  | { page: "objective" }
  | { page: "run"; runId: string }
  | { page: "not_found" };

const RUN_PATH = /^\/runs\/([^/]+)\/?$/;
const CHAT_PATH = /^\/c\/([^/]+)\/?$/;

export function matchRoute(pathname: string): Route {
  if (pathname === "/objective") return { page: "objective" };
  const runMatch = RUN_PATH.exec(pathname);
  if (runMatch) return { page: "run", runId: decodeURIComponent(runMatch[1]) };
  const chatMatch = CHAT_PATH.exec(pathname);
  if (chatMatch) return { page: "chat", conversationId: decodeURIComponent(chatMatch[1]) };
  if (pathname === "/" || pathname === "") return { page: "chat" };
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

export function chatPath(conversationId: string) {
  return `/c/${encodeURIComponent(conversationId)}`;
}

export function useRoute(): Route {
  const pathname = useSyncExternalStore(subscribe, () => window.location.pathname);
  return matchRoute(pathname);
}
