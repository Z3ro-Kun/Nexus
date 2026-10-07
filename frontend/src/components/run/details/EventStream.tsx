import { useEffect, useRef, useState } from "react";

import { describeEvent } from "../../../lib/events";
import { formatTime } from "../../../lib/format";
import type { NexusEvent } from "../../../types/api";
import { Empty, Mono, Panel, TEXT } from "./ui";

/** The run's real events, in sequence order, newest at the bottom. */
export default function EventStream({ events }: { events: NexusEvent[] }) {
  const [onlyImportant, setOnlyImportant] = useState(false);
  const list = useRef<HTMLOListElement>(null);
  const shown = onlyImportant ? events.filter((e) => describeEvent(e).important) : events;

  useEffect(() => {
    const el = list.current;
    if (el && typeof el.scrollTo === "function") el.scrollTo({ top: el.scrollHeight });
  }, [shown.length]);

  return (
    <Panel
      title="Event stream"
      aside={
        <label className="flex items-center gap-2 font-mono text-[11px] text-dim">
          <input type="checkbox" checked={onlyImportant} onChange={(e) => setOnlyImportant(e.target.checked)} className="accent-accent" />
          key events only
        </label>
      }
    >
      {shown.length === 0 ? (
        <Empty>No events yet.</Empty>
      ) : (
        <ol ref={list} aria-label="Events" className="max-h-[28rem] space-y-0.5 overflow-y-auto pr-1">
          {shown.map((event) => {
            const d = describeEvent(event);
            return (
              <li
                key={event.sequence}
                data-sequence={event.sequence}
                className={`grid grid-cols-[2.5rem_4.5rem_minmax(8rem,10rem)_1fr] items-baseline gap-2 rounded px-1.5 py-1 text-xs ${d.important ? "bg-raised" : ""}`}
              >
                <Mono className="text-right text-faint">{event.sequence}</Mono>
                <Mono className="text-faint">{formatTime(event.timestamp)}</Mono>
                <Mono className={`truncate ${d.important ? TEXT[d.tone] : "text-dim"}`}>{event.event_type}</Mono>
                <span className="min-w-0 break-words text-dim">
                  {(event.agent_id || event.task_id) && (
                    <Mono className="mr-2 text-faint">
                      [{[event.agent_id, event.task_id].filter(Boolean).join(" · ")}]
                    </Mono>
                  )}
                  {d.detail}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </Panel>
  );
}
