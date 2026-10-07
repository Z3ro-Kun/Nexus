import type { ReactNode } from "react";

import { linkTo } from "../lib/router";
import HealthIndicator from "./HealthIndicator";

export default function AppShell({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <div className="flex min-h-screen flex-col">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-10 focus:rounded-md focus:bg-surface focus:px-3 focus:py-2 focus:text-sm">
        Skip to content
      </a>
      <header className="border-b border-line">
        <div className={`mx-auto flex h-14 w-full items-center justify-between gap-4 px-4 sm:px-6 ${wide ? "max-w-4xl" : "max-w-3xl"}`}>
          <a {...linkTo("/")} className="flex items-center gap-2.5 text-ink" aria-label="NEXUS home" translate="no">
            <Wordmark />
          </a>
          <HealthIndicator />
        </div>
      </header>
      <main id="main" className={`mx-auto w-full flex-1 px-4 pt-10 pb-24 sm:px-6 sm:pt-14 ${wide ? "max-w-4xl" : "max-w-3xl"}`}>
        {children}
      </main>
    </div>
  );
}

/** Three nodes converging on one: many agents, one objective. */
function Wordmark() {
  return (
    <>
      <svg viewBox="0 0 20 20" className="size-5 text-accent" aria-hidden="true">
        <path d="M3 4.5L10 10M3 15.5L10 10M3 10h7m0 0l7 0" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none" />
        <circle cx="3" cy="4.5" r="1.6" fill="currentColor" />
        <circle cx="3" cy="10" r="1.6" fill="currentColor" />
        <circle cx="3" cy="15.5" r="1.6" fill="currentColor" />
        <circle cx="16.5" cy="10" r="2.4" fill="currentColor" />
      </svg>
      <span className="text-[15px] font-semibold tracking-[0.14em]">NEXUS</span>
    </>
  );
}
