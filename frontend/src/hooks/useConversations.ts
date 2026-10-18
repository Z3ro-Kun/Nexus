import { useCallback, useEffect, useRef, useState } from "react";
import {
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  sendMessage,
} from "../api/conversations";
import { startExecution } from "../lib/executions";
import type {
  ConversationDetail,
  ConversationSummary,
} from "../types/conversations";

export function useConversations(initialId?: string | null) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(initialId ?? null);
  const [activeConversation, setActiveConversation] = useState<ConversationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  // Load conversation list
  const refreshList = useCallback(async () => {
    try {
      const list = await listConversations();
      setConversations(list);
    } catch (err: unknown) {
      console.error("Failed to load conversations", err);
    }
  }, []);

  useEffect(() => {
    refreshList();
  }, [refreshList]);

  // Load active conversation details
  const refreshActive = useCallback(async (id: string) => {
    try {
      setLoading(true);
      setError(null);
      const detail = await getConversation(id);
      setActiveConversation(detail);
    } catch (err: unknown) {
      setError("Failed to load chat history.");
      console.error(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeId) {
      refreshActive(activeId);
    } else {
      setActiveConversation(null);
    }
  }, [activeId, refreshActive]);

  // Periodic polling if the latest turn in active conversation is running
  const latestTurn = activeConversation?.turns[activeConversation.turns.length - 1];
  const isRunning =
    latestTurn &&
    (latestTurn.status === "created" ||
      latestTurn.status === "running" ||
      latestTurn.status === "executing" ||
      latestTurn.status === "verifying");

  useEffect(() => {
    if (!isRunning || !activeId) return;

    const interval = setInterval(async () => {
      try {
        const updated = await getConversation(activeId);
        setActiveConversation(updated);
        const last = updated.turns[updated.turns.length - 1];
        if (last && (last.status === "completed" || last.status === "failed")) {
          refreshList();
        }
      } catch {
        // ignore polling error
      }
    }, 2000);

    return () => clearInterval(interval);
  }, [isRunning, activeId, refreshList]);

  // Start a new conversation or send prompt
  const handleStartNewChat = useCallback(
    async (objective: string, documentIds: string[] = []) => {
      setSending(true);
      setError(null);
      try {
        const detail = await createConversation({
          objective,
          document_ids: documentIds,
        });
        setConversations((prev) => [
          {
            id: detail.id,
            title: detail.title,
            created_at: detail.created_at,
            updated_at: detail.updated_at,
            turn_count: detail.turns.length,
            last_status: detail.turns[0]?.status ?? "created",
          },
          ...prev,
        ]);
        setActiveId(detail.id);
        setActiveConversation(detail);

        // Start execution of the initial run
        const firstRunId = detail.turns[0]?.run_id;
        if (firstRunId) {
          startExecution(firstRunId);
        }
        return detail.id;
      } catch (err: unknown) {
        setError("Failed to start conversation.");
        throw err;
      } finally {
        setSending(false);
      }
    },
    []
  );

  // Send follow-up message into active conversation
  const handleSendMessage = useCallback(
    async (objective: string, documentIds: string[] = []) => {
      if (!activeId) return;
      setSending(true);
      setError(null);
      try {
        const run = await sendMessage(activeId, {
          objective,
          document_ids: documentIds,
        });

        // Trigger execution of this run
        startExecution(run.id);

        // Immediately update conversation turns locally
        const updated = await getConversation(activeId);
        setActiveConversation(updated);
        refreshList();
      } catch (err: unknown) {
        setError("Failed to send message.");
        throw err;
      } finally {
        setSending(false);
      }
    },
    [activeId, refreshList]
  );

  // Delete conversation
  const handleDeleteChat = useCallback(
    async (id: string) => {
      try {
        await deleteConversation(id);
        setConversations((prev) => prev.filter((c) => c.id !== id));
        if (activeId === id) {
          setActiveId(null);
          setActiveConversation(null);
        }
      } catch (err) {
        console.error("Failed to delete conversation", err);
      }
    },
    [activeId]
  );

  return {
    conversations,
    activeId,
    setActiveId,
    activeConversation,
    loading,
    error,
    sending,
    isRunning: Boolean(isRunning),
    startNewChat: handleStartNewChat,
    sendFollowUp: handleSendMessage,
    deleteChat: handleDeleteChat,
    refreshActive: () => (activeId ? refreshActive(activeId) : Promise.resolve()),
  };
}
