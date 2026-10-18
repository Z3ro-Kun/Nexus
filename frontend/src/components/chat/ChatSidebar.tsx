import type { ConversationSummary } from "../../types/conversations";

interface ChatSidebarProps {
  conversations: ConversationSummary[];
  activeId: string | null;
  onSelectChat: (id: string) => void;
  onNewChat: () => void;
  onDeleteChat: (id: string) => void;
  open?: boolean;
  onClose?: () => void;
}

export default function ChatSidebar({
  conversations,
  activeId,
  onSelectChat,
  onNewChat,
  onDeleteChat,
  open = true,
  onClose,
}: ChatSidebarProps) {
  return (
    <aside
      className={`fixed inset-y-0 left-0 z-30 flex w-72 flex-col border-r border-line bg-surface transition-transform duration-200 md:static md:translate-x-0 ${
        open ? "translate-x-0" : "-translate-x-full"
      }`}
    >
      {/* Sidebar Header */}
      <div className="flex h-14 items-center justify-between border-b border-line px-4">
        <div className="flex items-center gap-2">
          <svg viewBox="0 0 20 20" className="size-5 text-accent" aria-hidden="true">
            <path
              d="M3 4.5L10 10M3 15.5L10 10M3 10h7m0 0l7 0"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              fill="none"
            />
            <circle cx="3" cy="4.5" r="1.6" fill="currentColor" />
            <circle cx="3" cy="10" r="1.6" fill="currentColor" />
            <circle cx="3" cy="15.5" r="1.6" fill="currentColor" />
            <circle cx="16.5" cy="10" r="2.4" fill="currentColor" />
          </svg>
          <span className="text-sm font-semibold tracking-wider text-ink uppercase">NEXUS</span>
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="md:hidden p-1.5 text-dim hover:text-ink"
            aria-label="Close sidebar"
          >
            ✕
          </button>
        )}
      </div>

      {/* New Chat Button */}
      <div className="p-3">
        <button
          type="button"
          onClick={onNewChat}
          className="flex w-full items-center justify-center gap-2 rounded-xl border border-line bg-surface-2 px-3.5 py-2.5 text-sm font-medium text-ink hover:border-accent hover:text-accent transition-all shadow-2xs"
        >
          <svg className="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M12 4v16m8-8H4" />
          </svg>
          New Chat
        </button>
      </div>

      {/* Conversation List */}
      <div className="flex-1 overflow-y-auto px-2 py-1 space-y-1">
        {conversations.length === 0 ? (
          <p className="px-3 py-6 text-center text-xs text-dim">No chats yet.</p>
        ) : (
          conversations.map((c) => {
            const isActive = c.id === activeId;
            return (
              <div
                key={c.id}
                className={`group relative flex items-center justify-between rounded-lg px-3 py-2 text-sm transition-colors cursor-pointer ${
                  isActive
                    ? "bg-surface-2 text-ink font-medium border border-line/80 shadow-2xs"
                    : "text-dim hover:bg-surface-2/60 hover:text-ink"
                }`}
                onClick={() => onSelectChat(c.id)}
              >
                <div className="min-w-0 flex-1 pr-2">
                  <p className="truncate text-xs">{c.title || "Untitled Chat"}</p>
                  <p className="text-[10px] text-dim opacity-70">
                    {c.turn_count} {c.turn_count === 1 ? "turn" : "turns"}
                  </p>
                </div>

                {/* Delete Chat Button on Hover */}
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (window.confirm("Delete this chat and its data?")) {
                      onDeleteChat(c.id);
                    }
                  }}
                  title="Delete chat"
                  className="opacity-0 group-hover:opacity-100 p-1 text-dim hover:text-bad rounded transition-opacity"
                  aria-label={`Delete chat ${c.title}`}
                >
                  <svg className="size-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth="2"
                      d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
                    />
                  </svg>
                </button>
              </div>
            );
          })
        )}
      </div>

      {/* Sidebar Footer */}
      <div className="border-t border-line p-3 text-center text-[11px] text-dim">
        NEXUS Multi-Agent AI
      </div>
    </aside>
  );
}
