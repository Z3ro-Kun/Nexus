import { API_BASE_URL, ApiError } from "./client";
import type { DocumentRead } from "../types/api";

export async function uploadDocument(file: File, signal?: AbortSignal): Promise<DocumentRead> {
  const formData = new FormData();
  formData.append("file", file);

  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}/documents/upload`, {
      method: "POST",
      body: formData,
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError("network", "Failed to reach the backend to upload document.");
  }

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}));
    const message = errorData.detail || `Upload failed with status ${response.status}`;
    throw new ApiError("http", message, { status: response.status });
  }

  return (await response.json()) as DocumentRead;
}
