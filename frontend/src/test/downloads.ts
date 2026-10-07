import { onTestFinished, vi } from "vitest";

/**
 * Capture browser downloads in a test: object URLs are faked (jsdom has none) and link
 * clicks are recorded instead of navigating. Everything is restored when the test ends.
 */
export function captureDownloads(): { saved: string[]; created: ReturnType<typeof vi.fn> } {
  const original = { create: URL.createObjectURL, revoke: URL.revokeObjectURL };
  const created = vi.fn(() => "blob:nexus/1");
  URL.createObjectURL = created as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn();
  const saved: string[] = [];
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
    saved.push(`${this.download} ${this.href}`.trim());
  });
  onTestFinished(() => {
    URL.createObjectURL = original.create;
    URL.revokeObjectURL = original.revoke;
    click.mockRestore();
  });
  return { saved, created };
}
