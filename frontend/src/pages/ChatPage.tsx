import { useEffect, useRef, useState } from "react";
import ChatInput from "../components/chat/ChatInput";
import ChatMessage from "../components/chat/ChatMessage";
import ChatSidebar from "../components/chat/ChatSidebar";
import RecentRuns from "../components/RecentRuns";
import { useConversations } from "../hooks/useConversations";

export default function ChatPage({ initialConversationId }: { initialConversationId?: string | null }) {
  const {
    conversations,
    activeId,
    setActiveId,
    activeConversation,
    loading,
    error,
    sending,
    isRunning,
    startNewChat,
    sendFollowUp,
    deleteChat,
  } = useConversations(initialConversationId);

  const [sidebarOpen, setSidebarOpen] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    if (typeof messagesEndRef.current?.scrollIntoView === "function") {
      messagesEndRef.current.scrollIntoView({ behavior: "smooth" });
    }
  };

  useEffect(() => {
    scrollToBottom();
  }, [activeConversation?.turns, isRunning]);

  const handleSend = async (message: string, documentIds: string[]) => {
    if (!activeId) {
      await startNewChat(message, documentIds);
    } else {
      await sendFollowUp(message, documentIds);
    }
  };

  const handleSuggestion = (prompt: string) => {
    handleSend(prompt, []);
  };

  const turns = activeConversation?.turns || [];

  return (
    <div className="flex h-screen w-full overflow-hidden bg-bg text-ink">
      {/* Sidebar */}
      <ChatSidebar
        conversations={conversations}
        activeId={activeId}
        onSelectChat={(id) => {
          setActiveId(id);
          setSidebarOpen(false);
        }}
        onNewChat={() => {
          setActiveId(null);
          setSidebarOpen(false);
        }}
        onDeleteChat={deleteChat}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
      />

      {/* Main Chat Area */}
      <div className="flex flex-1 flex-col h-full min-w-0 bg-bg">
        {/* Chat Header */}
        <header className="flex h-14 items-center justify-between border-b border-line px-4 bg-surface/50 backdrop-blur-xs">
          <div className="flex items-center gap-3 min-w-0">
            <button
              type="button"
              onClick={() => setSidebarOpen(true)}
              className="md:hidden p-1.5 text-dim hover:text-ink rounded-lg"
              aria-label="Open sidebar"
            >
              <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
            <h1 className="text-sm font-semibold truncate text-ink">
              {activeConversation?.title || "New Chat"}
            </h1>
          </div>

          <div className="flex items-center gap-2">
            {isRunning && (
              <span className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium text-accent bg-accent/10 border border-accent/20 rounded-full">
                <span className="size-1.5 rounded-full bg-accent animate-pulse" />
                Agents Active
              </span>
            )}
            <button
              type="button"
              onClick={() => setActiveId(null)}
              className="px-3 py-1.5 text-xs font-medium text-dim hover:text-ink border border-line rounded-lg hover:bg-surface-2 transition-colors"
            >
              + New Chat
            </button>
          </div>
        </header>

        {/* Message Thread Container */}
        <div className="flex-1 overflow-y-auto px-4 py-6 md:px-8">
          <div className="max-w-4xl mx-auto space-y-4">
            {loading && (
              <p className="text-center text-sm text-dim py-8">Loading chat history…</p>
            )}

            {error && (
              <div className="p-3 bg-bad/10 border border-bad/30 rounded-xl text-bad text-xs text-center">
                {error}
              </div>
            )}

            {/* Empty Chat Welcome Hero */}
            {!loading && turns.length === 0 && (
              <div className="flex flex-col items-center justify-center min-h-[50vh] text-center px-4 space-y-6">
                <div className="size-12 rounded-2xl bg-accent/10 border border-accent/25 flex items-center justify-center text-accent">
                  <svg viewBox="0 0 20 20" className="size-7" aria-hidden="true">
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
                </div>
                <div className="space-y-1.5 max-w-md">
                  <h2 className="text-xl font-semibold text-ink tracking-tight">How can NEXUS help you today?</h2>
                  <p className="text-xs text-dim">
                    NEXUS deploys a specialized multi-agent team (researcher, analyst, specialist, verifier) to execute and rigorously verify your request.
                  </p>
                </div>

                {/* Prompt Suggestions */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5 w-full max-w-xl text-left">
                  <button
                    type="button"
                    onClick={() => handleSuggestion("Tell me 5-10 latest anime streaming this month with platforms and summaries")}
                    className="p-3 rounded-xl border border-line bg-surface hover:border-accent hover:bg-surface-2 transition-all text-xs space-y-1 group"
                  >
                    <p className="font-medium text-ink group-hover:text-accent">Anime Streaming Guide</p>
                    <p className="text-dim text-[11px]">Tell me 5-10 latest anime streaming this month with platforms</p>
                  </button>
                  <button
                    type="button"
                    onClick={() => handleSuggestion("Summarize the attached document, identify key skills, and evaluate experience")}
                    className="p-3 rounded-xl border border-line bg-surface hover:border-accent hover:bg-surface-2 transition-all text-xs space-y-1 group"
                  >
                    <p className="font-medium text-ink group-hover:text-accent">Document Summarization</p>
                    <p className="text-dim text-[11px]">Attach a resume or PDF and extract structured skills</p>
                  </button>
                </div>

                <div className="w-full max-w-xl text-left pt-2">
                  <RecentRuns />
                </div>
              </div>
            )}

            {/* Render Each Chat Turn */}
            {turns.map((turn, idx) => (
              <ChatMessage
                key={turn.run_id}
                turn={turn}
                isLatest={idx === turns.length - 1}
                onRefresh={() => {
                  if (activeId) refreshActive(activeId);
                  refreshList();
                }}
              />
            ))}

            <div ref={messagesEndRef} />
          </div>
        </div>

        {/* Sticky Bottom Input Bar */}
        <ChatInput
          onSend={handleSend}
          disabled={sending || isRunning}
          placeholder={
            turns.length > 0
              ? "Ask a follow-up about the results above..."
              : "What would you like NEXUS to research or build?"
          }
        />
      </div>
    </div>
  );
}
