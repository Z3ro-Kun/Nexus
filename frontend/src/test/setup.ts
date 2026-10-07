import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

import { resetExecutions } from "../lib/executions";
import { setMotionPaused } from "../lib/motion";

afterEach(() => {
  cleanup();
  resetExecutions();
  window.history.replaceState(null, "", "/");
  setMotionPaused(false);
  window.localStorage.clear();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
