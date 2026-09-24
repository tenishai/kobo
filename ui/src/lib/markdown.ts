// A small markdown renderer for the room's own guide (GettingStarted.svelte).
//
// The guide is written by the room's authors and packaged with the API, so
// the dialect is deliberately narrow: `##`/`###` headings, paragraphs, `-`
// bullet lists, `1.` numbered lists, **bold**, `code`, and [text](url) links
// to http(s) or in-app `#/` hashes. Everything is HTML-escaped first, so a
// stray angle bracket in a guide file renders as text; nothing in the input
// can reach the page as markup except what this file emits. No dependency,
// no configuration, and a test that pins each construct (markdown.test.ts).

function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function safeHref(href: string): string | null {
  if (href.startsWith("#/") || href.startsWith("https://") || href.startsWith("http://")) {
    return href;
  }
  return null;
}

/** Inline constructs, applied to already-escaped text. */
export function renderInline(escaped: string): string {
  let out = escaped.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, text: string, href: string) => {
    const safe = safeHref(href);
    if (safe === null) {
      return text;
    }
    const external = safe.startsWith("http") ? ' rel="noopener" target="_blank"' : "";
    return `<a href="${safe}"${external}>${text}</a>`;
  });
  return out;
}

type Block =
  | { kind: "heading"; level: 2 | 3; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] };

function parseBlocks(markdown: string): Block[] {
  const blocks: Block[] = [];
  const lines = markdown.replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    const heading = /^(#{2,3})\s+(.+?)\s*$/.exec(line);
    if (heading) {
      blocks.push({
        kind: "heading",
        level: heading[1].length === 2 ? 2 : 3,
        text: heading[2],
      });
      i += 1;
      continue;
    }
    const bullet = /^-\s+(.*)$/.exec(line);
    const numbered = /^\d+\.\s+(.*)$/.exec(line);
    if (bullet || numbered) {
      const ordered = Boolean(numbered);
      const items: string[] = [];
      while (i < lines.length) {
        const match = ordered ? /^\d+\.\s+(.*)$/.exec(lines[i]) : /^-\s+(.*)$/.exec(lines[i]);
        if (!match) {
          break;
        }
        let item = match[1];
        // A wrapped list item continues on indented lines.
        while (i + 1 < lines.length && /^\s{2,}\S/.test(lines[i + 1])) {
          item += ` ${lines[i + 1].trim()}`;
          i += 1;
        }
        items.push(item);
        i += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    // Paragraph: consecutive non-blank lines that are not a heading or list.
    const parts: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^#{2,3}\s/.test(lines[i]) &&
      !/^-\s/.test(lines[i]) &&
      !/^\d+\.\s/.test(lines[i])
    ) {
      parts.push(lines[i].trim());
      i += 1;
    }
    blocks.push({ kind: "paragraph", text: parts.join(" ") });
  }
  return blocks;
}

/** Render a guide section's markdown body to HTML. Safe to place with `{@html}`. */
export function renderMarkdown(markdown: string): string {
  const html: string[] = [];
  for (const block of parseBlocks(markdown)) {
    if (block.kind === "heading") {
      const tag = `h${block.level}`;
      html.push(`<${tag}>${renderInline(escapeHtml(block.text))}</${tag}>`);
    } else if (block.kind === "paragraph") {
      html.push(`<p>${renderInline(escapeHtml(block.text))}</p>`);
    } else {
      const tag = block.ordered ? "ol" : "ul";
      const items = block.items
        .map((item) => `<li>${renderInline(escapeHtml(item))}</li>`)
        .join("");
      html.push(`<${tag}>${items}</${tag}>`);
    }
  }
  return html.join("\n");
}
