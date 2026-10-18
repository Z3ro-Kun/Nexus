import { useRef, useState, type KeyboardEvent } from "react";
import { uploadDocument } from "../../api/documents";
import type { DocumentRead } from "../../types/api";

interface ChatInputProps {
  onSend: (message: string, documentIds: string[]) => void;
  disabled?: boolean;
  placeholder?: string;
}

export default function ChatInput({
  onSend,
  disabled = false,
  placeholder = "Ask a follow-up or enter a new request...",
}: ChatInputProps) {
  const [text, setText] = useState("");
  const [attachedDocs, setAttachedDocs] = useState<DocumentRead[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleSubmit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled || uploading) return;
    onSend(
      trimmed,
      attachedDocs.map((d) => d.id)
    );
    setText("");
    setAttachedDocs([]);
    setUploadError(null);
  };

  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files || files.length === 0) return;

    setUploading(true);
    setUploadError(null);
    try {
      for (const file of Array.from(files)) {
        const doc = await uploadDocument(file);
        setAttachedDocs((prev) => [...prev, doc]);
      }
    } catch (err: unknown) {
      setUploadError(err instanceof Error ? err.message : "Failed to upload file");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const removeDoc = (id: string) => {
    setAttachedDocs((prev) => prev.filter((d) => d.id !== id));
  };

  return (
    <div className="w-full max-w-4xl mx-auto px-4 pb-4">
      {/* Attached Documents Pills */}
      {attachedDocs.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-2 p-2 bg-surface/60 border border-line rounded-lg">
          {attachedDocs.map((doc) => (
            <div
              key={doc.id}
              className="flex items-center gap-1.5 px-2.5 py-1 text-xs bg-surface-2 border border-line rounded-md text-ink"
            >
              <svg className="size-3.5 text-accent" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth="2"
                  d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
                />
              </svg>
              <span className="font-medium max-w-[200px] truncate">{doc.filename}</span>
              <span className="text-dim text-[11px]">
                ({Math.round(doc.size_bytes / 1024) || 1} KB)
              </span>
              <button
                type="button"
                onClick={() => removeDoc(doc.id)}
                className="ml-1 text-dim hover:text-bad"
                aria-label={`Remove ${doc.filename}`}
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      )}

      {uploadError && (
        <p className="text-xs text-bad mb-2 px-1">{uploadError}</p>
      )}

      {/* Input Form Box */}
      <div className="relative flex items-end gap-2 bg-surface border border-line rounded-2xl shadow-sm focus-within:border-accent focus-within:ring-1 focus-within:ring-accent transition-all p-2">
        {/* Hidden File Input */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFileChange}
          accept=".pdf,.txt,.md,.json,.csv"
        />

        {/* Attach Button */}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || uploading}
          title="Attach document (PDF, TXT, MD, JSON, CSV)"
          className="p-2 text-dim hover:text-ink disabled:opacity-40 rounded-xl transition-colors shrink-0"
        >
          {uploading ? (
            <svg className="size-5 animate-spin" viewBox="0 0 24 24" fill="none">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
              />
            </svg>
          ) : (
            <svg className="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="2"
                d="M15.172 7l-6.586 6.586a2 2 0 102.828 2.828l6.414-6.586a4 4 0 00-5.656-5.656l-6.415 6.585a6 6 0 108.486 8.486L20.5 13"
              />
            </svg>
          )}
        </button>

        {/* Text Area */}
        <textarea
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          placeholder={disabled ? "Agents are working on your request..." : placeholder}
          className="flex-1 max-h-36 resize-none bg-transparent py-2 px-1 text-sm text-ink placeholder:text-dim focus:outline-none"
        />

        {/* Send Button */}
        <button
          type="button"
          onClick={handleSubmit}
          disabled={!text.trim() || disabled || uploading}
          className="p-2 rounded-xl bg-accent text-accent-ink hover:opacity-90 disabled:opacity-30 disabled:hover:opacity-30 transition-all shrink-0"
          aria-label="Send message"
        >
          <svg className="size-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.5" d="M5 12h14M12 5l7 7-7 7" />
          </svg>
        </button>
      </div>
      <p className="text-[11px] text-dim text-center mt-1.5">
        Press <kbd className="px-1 py-0.5 bg-surface-2 border border-line rounded">Enter</kbd> to send, <kbd className="px-1 py-0.5 bg-surface-2 border border-line rounded">Shift + Enter</kbd> for new line.
      </p>
    </div>
  );
}
