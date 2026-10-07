/**
 * A user switch for live-execution motion (worker loops, data particles), which can run for
 * as long as a run does. Paused motion freezes CSS animations (the `motion-paused` class on
 * <html>) and stops rendering SVG particles. Remembered per browser when storage allows.
 */

import { useSyncExternalStore } from "react";

const KEY = "nexus.motion-paused";
const listeners = new Set<() => void>();

function read(): boolean {
  try {
    return window.localStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

let paused = typeof window !== "undefined" && read();
const apply = () => document.documentElement.classList.toggle("motion-paused", paused);
if (typeof document !== "undefined") apply();

export function setMotionPaused(value: boolean) {
  paused = value;
  try {
    window.localStorage.setItem(KEY, value ? "1" : "0");
  } catch {
    // Not remembered; still applies now.
  }
  apply();
  listeners.forEach((l) => l());
}

export function useMotionPaused(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    () => paused,
  );
}
