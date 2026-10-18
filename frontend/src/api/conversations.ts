import { API_BASE_URL, ApiError, request } from "./client";
import type {
  ConversationDetail,
  ConversationSummary,
} from "../types/conversations";
import type { RunRead } from "../types/api";

function isConversationSummaryList(data: unknown): data is ConversationSummary[] {
  return Array.isArray(data);
}

function isConversationDetail(data: unknown): data is ConversationDetail {
  return typeof data === "object" && data !== null && "id" in data && "turns" in data;
}

function isRunRead(data: unknown): data is RunRead {
  return typeof data === "object" && data !== null && "id" in data && "status" in data;
}

export async function listConversations(): Promise<ConversationSummary[]> {
  return request("/conversations", isConversationSummaryList);
}

export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  return request(`/conversations/${conversationId}`, isConversationDetail);
}

export async function createConversation(payload: {
  title?: string;
  objective?: string;
  document_ids?: string[];
}): Promise<ConversationDetail> {
  return request("/conversations", isConversationDetail, {
    method: "POST",
    body: payload,
  });
}

export async function sendMessage(
  conversationId: string,
  payload: {
    objective: string;
    document_ids?: string[];
    constraints?: string[];
  }
): Promise<RunRead> {
  return request(`/conversations/${conversationId}/messages`, isRunRead, {
    method: "POST",
    body: payload,
  });
}

export async function deleteConversation(conversationId: string): Promise<void> {
  try {
    const res = await fetch(`${API_BASE_URL}/conversations/${conversationId}`, {
      method: "DELETE",
    });
    if (!res.ok && res.status !== 404) {
      throw new ApiError("http", "Failed to delete conversation", { status: res.status });
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("network", "Failed to reach server to delete conversation");
  }
}
