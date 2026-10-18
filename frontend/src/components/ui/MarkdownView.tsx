import { useMemo, useState } from "react";

interface MarkdownViewProps {
  content: string;
  className?: string;
  allowToggleRaw?: boolean;
}

const PLATFORM_COLORS: Record<string, { bg: string; text: string; border: string }> = {
  crunchyroll: { bg: "bg-orange-500/10 dark:bg-orange-500/20", text: "text-orange-700 dark:text-orange-400", border: "border-orange-500/30" },
  netflix: { bg: "bg-red-500/10 dark:bg-red-500/20", text: "text-red-700 dark:text-red-400", border: "border-red-500/30" },
  hulu: { bg: "bg-emerald-500/10 dark:bg-emerald-500/20", text: "text-emerald-700 dark:text-emerald-400", border: "border-emerald-500/30" },
  disney: { bg: "bg-blue-500/10 dark:bg-blue-500/20", text: "text-blue-700 dark:text-blue-400", border: "border-blue-500/30" },
  "disney+": { bg: "bg-blue-500/10 dark:bg-blue-500/20", text: "text-blue-700 dark:text-blue-400", border: "border-blue-500/30" },
  hidive: { bg: "bg-cyan-500/10 dark:bg-cyan-500/20", text: "text-cyan-700 dark:text-cyan-400", border: "border-cyan-500/30" },
  prime: { bg: "bg-sky-500/10 dark:bg-sky-500/20", text: "text-sky-700 dark:text-sky-400", border: "border-sky-500/30" },
  "prime video": { bg: "bg-sky-500/10 dark:bg-sky-500/20", text: "text-sky-700 dark:text-sky-400", border: "border-sky-500/30" },
};

function renderPlatformBadge(name: string) {
  const clean = name.trim();
  const lower = clean.toLowerCase();
  const matched = Object.entries(PLATFORM_COLORS).find(([k]) => lower.includes(k));
  if (!matched) {
    return (
      <span key={clean} className="inline-flex items-center rounded-md border border-line bg-raised px-2 py-0.5 font-sans text-xs font-medium text-dim">
        {clean}
      </span>
    );
  }
  const [, style] = matched;
  return (
    <span key={clean} className={`inline-flex items-center rounded-md border px-2 py-0.5 font-sans text-xs font-medium ${style.bg} ${style.text} ${style.border}`}>
      {clean}
    </span>
  );
}

function renderCellContent(text: string, isPlatformCol: boolean) {
  if (isPlatformCol) {
    const parts = text.split(/[,/]+/).map((p) => p.trim()).filter(Boolean);
    if (parts.length > 0 && parts.every((p) => p.length < 30)) {
      return <div className="flex flex-wrap gap-1.5">{parts.map(renderPlatformBadge)}</div>;
    }
  }
  return renderInline(text);
}

function renderInline(text: string): (string | JSX.Element)[] {
  // Parses **bold**, *italic*, `code`, and [link](url)
  const elements: (string | JSX.Element)[] = [];
  const regex = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|\[[^\]]+\]\([^)]+\))/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) {
      elements.push(text.slice(lastIndex, match.index));
    }
    const token = match[0];
    if (token.startsWith("**") && token.endsWith("**")) {
      elements.push(
        <strong key={match.index} className="font-semibold text-ink">
          {token.slice(2, -2)}
        </strong>
      );
    } else if (token.startsWith("*") && token.endsWith("*")) {
      elements.push(
        <em key={match.index} className="italic text-dim">
          {token.slice(1, -1)}
        </em>
      );
    } else if (token.startsWith("`") && token.endsWith("`")) {
      elements.push(
        <code key={match.index} className="rounded bg-raised px-1 py-0.5 font-mono text-xs font-medium text-accent">
          {token.slice(1, -1)}
        </code>
      );
    } else if (token.startsWith("[") && token.includes("](") && token.endsWith(")")) {
      const parts = token.slice(1, -1).split("](");
      elements.push(
        <a key={match.index} href={parts[1]} target="_blank" rel="noopener noreferrer" className="text-accent underline hover:opacity-80">
          {parts[0]}
        </a>
      );
    }
    lastIndex = regex.lastIndex;
  }

  if (lastIndex < text.length) {
    elements.push(text.slice(lastIndex));
  }

  return elements.length > 0 ? elements : [text];
}

interface TableBlock {
  type: "table";
  headers: string[];
  rows: string[][];
}

interface CodeBlock {
  type: "code";
  lang: string;
  code: string;
}

interface HeadingBlock {
  type: "heading";
  level: number;
  text: string;
}

interface ListBlock {
  type: "list";
  ordered: boolean;
  items: string[];
}

interface ParagraphBlock {
  type: "paragraph";
  text: string;
}

type Block = TableBlock | CodeBlock | HeadingBlock | ListBlock | ParagraphBlock;

function parseMarkdownBlocks(text: string): Block[] {
  const lines = text.split("\n");
  const blocks: Block[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i++;
      continue;
    }

    // Code block
    if (trimmed.startsWith("```")) {
      const lang = trimmed.slice(3).trim();
      const codeLines: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i++;
      }
      i++; // Skip closing ```
      blocks.push({ type: "code", lang, code: codeLines.join("\n") });
      continue;
    }

    // Table block (starts with |)
    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith("|") && lines[i].trim().endsWith("|")) {
        tableLines.push(lines[i].trim());
        i++;
      }
      if (tableLines.length >= 2) {
        const parseRow = (r: string) =>
          r
            .slice(1, -1)
            .split("|")
            .map((c) => c.trim());
        const headers = parseRow(tableLines[0]);
        // Row 1 is divider (|:---|:---|)
        const rows = tableLines.slice(2).map(parseRow);
        blocks.push({ type: "table", headers, rows });
        continue;
      }
    }

    // Heading block (#, ##, ###)
    const headingMatch = trimmed.match(/^(#{1,6})\s+(.*)$/);
    if (headingMatch) {
      blocks.push({
        type: "heading",
        level: headingMatch[1].length,
        text: headingMatch[2],
      });
      i++;
      continue;
    }

    // List block
    const isBullet = trimmed.startsWith("- ") || trimmed.startsWith("* ");
    const isOrdered = /^\d+\.\s+/.test(trimmed);
    if (isBullet || isOrdered) {
      const items: string[] = [];
      const ordered = isOrdered;
      while (i < lines.length) {
        const cur = lines[i].trim();
        if (ordered ? /^\d+\.\s+/.test(cur) : cur.startsWith("- ") || cur.startsWith("* ")) {
          const itemText = ordered ? cur.replace(/^\d+\.\s+/, "") : cur.slice(2);
          items.push(itemText.trim());
          i++;
        } else if (cur === "" || cur.startsWith("  ")) {
          // Empty or continuation line
          if (cur === "") break;
          i++;
        } else {
          break;
        }
      }
      blocks.push({ type: "list", ordered, items });
      continue;
    }

    // Normal paragraph
    const pLines: string[] = [];
    while (i < lines.length && lines[i].trim() !== "" && !lines[i].trim().startsWith("|") && !lines[i].trim().startsWith("```") && !lines[i].trim().startsWith("#") && !lines[i].trim().startsWith("- ") && !/^\d+\.\s+/.test(lines[i].trim())) {
      pLines.push(lines[i]);
      i++;
    }
    blocks.push({ type: "paragraph", text: pLines.join(" ") });
  }

  return blocks;
}

export default function MarkdownView({ content, className = "", allowToggleRaw = false }: MarkdownViewProps) {
  const [showRaw, setShowRaw] = useState(false);
  const blocks = useMemo(() => parseMarkdownBlocks(content), [content]);

  if (!content) return null;

  return (
    <div className={`space-y-4 ${className}`}>
      {allowToggleRaw && (
        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => setShowRaw(!showRaw)}
            className="font-mono text-[11px] text-faint hover:text-ink transition-colors"
          >
            {showRaw ? "Formatted view" : "Raw view"}
          </button>
        </div>
      )}

      {showRaw ? (
        <pre className="max-h-96 overflow-auto rounded-lg border border-line bg-raised p-3 font-mono text-xs leading-relaxed text-dim break-words whitespace-pre-wrap">
          {content}
        </pre>
      ) : (
        <div className="space-y-3">
          {blocks.map((block, idx) => {
            switch (block.type) {
              case "table": {
                const isPlatformCol = (header: string) => /platform|stream|service/i.test(header);
                return (
                  <div key={idx} className="my-3 overflow-hidden rounded-xl border border-line bg-surface shadow-xs">
                    <div className="overflow-x-auto">
                      <table className="w-full text-left text-sm border-collapse">
                        <thead>
                          <tr className="border-b border-line bg-raised/80">
                            {block.headers.map((h, hIdx) => (
                              <th
                                key={hIdx}
                                className="px-4 py-3 font-mono text-[11px] font-semibold uppercase tracking-wider text-dim"
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-line/60">
                          {block.rows.map((row, rIdx) => (
                            <tr key={rIdx} className="hover:bg-raised/40 transition-colors">
                              {row.map((cell, cIdx) => (
                                <td key={cIdx} className="px-4 py-3 align-top text-ink leading-relaxed">
                                  {renderCellContent(cell, isPlatformCol(block.headers[cIdx] || ""))}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                );
              }
              case "heading": {
                if (block.level === 1) {
                  return (
                    <h2 key={idx} className="mt-4 mb-2 text-xl font-bold tracking-tight text-ink">
                      {renderInline(block.text)}
                    </h2>
                  );
                }
                if (block.level === 2) {
                  return (
                    <h3 key={idx} className="mt-3.5 mb-1.5 text-lg font-semibold tracking-tight text-ink">
                      {renderInline(block.text)}
                    </h3>
                  );
                }
                return (
                  <h4 key={idx} className="mt-3 mb-1 text-base font-semibold text-ink">
                    {renderInline(block.text)}
                  </h4>
                );
              }
              case "list": {
                if (block.ordered) {
                  return (
                    <ol key={idx} className="my-2.5 list-decimal space-y-1.5 pl-5 text-ink leading-relaxed marker:text-faint">
                      {block.items.map((it, iIdx) => (
                        <li key={iIdx}>{renderInline(it)}</li>
                      ))}
                    </ol>
                  );
                }
                return (
                  <ul key={idx} className="my-2.5 list-disc space-y-1.5 pl-5 text-ink leading-relaxed marker:text-accent">
                    {block.items.map((it, iIdx) => (
                      <li key={iIdx}>{renderInline(it)}</li>
                    ))}
                  </ul>
                );
              }
              case "code": {
                return (
                  <div key={idx} className="my-3 overflow-hidden rounded-xl border border-line bg-raised">
                    {block.lang && (
                      <div className="border-b border-line px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-faint">
                        {block.lang}
                      </div>
                    )}
                    <pre className="overflow-x-auto p-3 font-mono text-xs leading-relaxed text-dim">
                      {block.code}
                    </pre>
                  </div>
                );
              }
              case "paragraph": {
                return (
                  <p key={idx} className="leading-relaxed text-ink/90 text-pretty">
                    {renderInline(block.text)}
                  </p>
                );
              }
              default:
                return null;
            }
          })}
        </div>
      )}
    </div>
  );
}
