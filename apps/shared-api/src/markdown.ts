/**
 * A deliberately small Markdown parser that produces a plain data tree.
 *
 * It never produces HTML: raw HTML in the source is kept as literal text, and
 * link targets are restricted to http(s) and mailto. Renderers (React Native
 * <Text>, React DOM elements) turn the tree into native elements, so there is
 * no injection surface. Supports headings, paragraphs, ordered/unordered lists,
 * fenced code, block quotes, horizontal rules, **strong**, *em*, `code` and
 * [links](https://...).
 */

export type Inline =
  | { type: "text"; text: string }
  | { type: "strong"; children: Inline[] }
  | { type: "em"; children: Inline[] }
  | { type: "code"; text: string }
  | { type: "link"; href: string | null; children: Inline[] };

export type Block =
  | { type: "heading"; level: 1 | 2 | 3 | 4 | 5 | 6; children: Inline[] }
  | { type: "paragraph"; children: Inline[] }
  | { type: "list"; ordered: boolean; items: Inline[][] }
  | { type: "code"; lang: string; text: string }
  | { type: "quote"; children: Inline[] }
  | { type: "hr" };

const SAFE_HREF = /^(https?:\/\/|mailto:)/i;

export function safeHref(href: string): string | null {
  const h = href.trim();
  return SAFE_HREF.test(h) ? h : null;
}

export function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let buf = "";
  const flush = () => {
    if (buf) out.push({ type: "text", text: buf });
    buf = "";
  };
  let i = 0;
  while (i < src.length) {
    const rest = src.slice(i);
    let m: RegExpExecArray | null;
    if ((m = /^`([^`]+)`/.exec(rest))) {
      flush();
      out.push({ type: "code", text: m[1] ?? "" });
      i += m[0].length;
    } else if ((m = /^(\*\*|__)(.+?)\1/.exec(rest))) {
      flush();
      out.push({ type: "strong", children: parseInline(m[2] ?? "") });
      i += m[0].length;
    } else if ((m = /^(\*|_)([^*_\s][^*_]*?)\1/.exec(rest))) {
      flush();
      out.push({ type: "em", children: parseInline(m[2] ?? "") });
      i += m[0].length;
    } else if ((m = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(rest))) {
      flush();
      out.push({ type: "link", href: safeHref(m[2] ?? ""), children: parseInline(m[1] ?? "") });
      i += m[0].length;
    } else {
      buf += src[i];
      i += 1;
    }
  }
  flush();
  return out;
}

export function parseMarkdown(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let para: string[] = [];
  const flushPara = () => {
    if (para.length) blocks.push({ type: "paragraph", children: parseInline(para.join(" ")) });
    para = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? "";
    const fence = /^```\s*([\w-]*)\s*$/.exec(line);
    if (fence) {
      flushPara();
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i] ?? "")) {
        body.push(lines[i] ?? "");
        i += 1;
      }
      blocks.push({ type: "code", lang: fence[1] ?? "", text: body.join("\n") });
      continue;
    }
    if (/^\s*$/.test(line)) {
      flushPara();
      continue;
    }
    const heading = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (heading) {
      flushPara();
      const level = (heading[1]?.length ?? 1) as 1 | 2 | 3 | 4 | 5 | 6;
      blocks.push({ type: "heading", level, children: parseInline(heading[2] ?? "") });
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushPara();
      blocks.push({ type: "hr" });
      continue;
    }
    if (/^>\s?/.test(line)) {
      flushPara();
      const q: string[] = [];
      while (i < lines.length && /^>\s?/.test(lines[i] ?? "")) {
        q.push((lines[i] ?? "").replace(/^>\s?/, ""));
        i += 1;
      }
      i -= 1;
      blocks.push({ type: "quote", children: parseInline(q.join(" ")) });
      continue;
    }
    const bullet = /^\s*([-*+]|\d+[.)])\s+/.exec(line);
    if (bullet) {
      flushPara();
      const ordered = /\d/.test(bullet[1] ?? "");
      const items: Inline[][] = [];
      while (i < lines.length) {
        const l = lines[i] ?? "";
        const b = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(l);
        if (!b || /\d/.test(b[1] ?? "") !== ordered) break;
        items.push(parseInline(b[2] ?? ""));
        i += 1;
      }
      i -= 1;
      blocks.push({ type: "list", ordered, items });
      continue;
    }
    para.push(line.trim());
  }
  flushPara();
  return blocks;
}

/** Plain-text rendering of inline nodes (for accessibility labels and previews). */
export function inlineText(nodes: readonly Inline[]): string {
  return nodes
    .map((n) => (n.type === "text" || n.type === "code" ? n.text : inlineText(n.children)))
    .join("");
}
