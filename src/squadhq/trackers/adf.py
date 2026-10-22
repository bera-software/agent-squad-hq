"""Basic Atlassian Document Format (ADF) <-> Markdown conversion.

Covers what tickets and agent comments actually use: paragraphs, headings,
bullet/ordered lists (nested), code blocks, inline code, bold, italic,
strike, links, hard breaks and horizontal rules. Unknown ADF nodes degrade
to their text content; nothing raises on odd input.
"""
from __future__ import annotations

import re
from typing import Any

Node = dict[str, Any]

# ---------------------------------------------------------------------------
# ADF -> Markdown
# ---------------------------------------------------------------------------


def _marks(text: str, marks: list[Node]) -> str:
    code = any(m.get("type") == "code" for m in marks)
    if code:
        text = f"`{text}`"
    for m in marks:
        t = m.get("type")
        if t == "strong":
            text = f"**{text}**"
        elif t == "em":
            text = f"*{text}*"
        elif t == "strike":
            text = f"~~{text}~~"
    for m in marks:
        if m.get("type") == "link":
            href = (m.get("attrs") or {}).get("href", "")
            text = f"[{text}]({href})"
    return text


def _inline(nodes: list[Node]) -> str:
    out: list[str] = []
    for n in nodes:
        t = n.get("type")
        if t == "text":
            out.append(_marks(str(n.get("text", "")), list(n.get("marks") or [])))
        elif t == "hardBreak":
            out.append("\n")
        elif t == "mention":
            out.append(str((n.get("attrs") or {}).get("text", "@user")))
        elif t == "emoji":
            out.append(str((n.get("attrs") or {}).get("text", "")))
        elif t == "inlineCard":
            out.append(str((n.get("attrs") or {}).get("url", "")))
        else:
            out.append(_inline(list(n.get("content") or [])))
    return "".join(out)


def _block(n: Node, indent: str = "") -> list[str]:
    t = n.get("type")
    content = list(n.get("content") or [])
    if t == "paragraph":
        return [indent + _inline(content)]
    if t == "heading":
        level = int((n.get("attrs") or {}).get("level", 1))
        return [indent + "#" * max(1, min(level, 6)) + " " + _inline(content)]
    if t == "codeBlock":
        lang = (n.get("attrs") or {}).get("language") or ""
        return [f"{indent}```{lang}", *(indent + line for line in _inline(content).split("\n")), f"{indent}```"]
    if t in ("bulletList", "orderedList"):
        lines: list[str] = []
        start = int((n.get("attrs") or {}).get("order", 1))
        for i, item in enumerate(content):
            marker = "- " if t == "bulletList" else f"{start + i}. "
            sub: list[str] = []
            for child in item.get("content") or []:
                sub.extend(_block(child, indent + " " * len(marker)))
            if sub:
                sub[0] = indent + marker + sub[0][len(indent) + len(marker):]
            else:
                sub = [indent + marker.rstrip()]
            lines.extend(sub)
        return lines
    if t == "rule":
        return [indent + "---"]
    if t == "blockquote":
        inner: list[str] = []
        for child in content:
            inner.extend(_block(child))
        return [indent + "> " + line for line in inner]
    if content:
        res: list[str] = []
        for child in content:
            res.extend(_block(child, indent) if child.get("type") not in ("text", "hardBreak") else [])
        if not res:
            return [indent + _inline(content)]
        return res
    return []


def adf_to_markdown(doc: Any) -> str:
    """Render an ADF document (``{"type": "doc", ...}``) as Markdown. Strings pass through."""
    if doc is None:
        return ""
    if isinstance(doc, str):
        return doc
    if not isinstance(doc, dict):
        return str(doc)
    blocks: list[str] = []
    for n in doc.get("content") or []:
        if isinstance(n, dict):
            rendered = _block(n)
            if rendered:
                blocks.append("\n".join(rendered))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Markdown -> ADF
# ---------------------------------------------------------------------------

_INLINE_RE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<link>\[(?P<ltext>[^\]]+)\]\((?P<href>[^)\s]+)\))"
    r"|(?P<bold>\*\*(?P<btext>.+?)\*\*)"
    r"|(?P<strike>~~(?P<stext>.+?)~~)"
    r"|(?P<em>(?<![\w*])[*_](?P<etext>[^*_\s][^*_]*?)[*_](?![\w*]))"
)


def _text(text: str, marks: list[Node] | None = None) -> Node:
    node: Node = {"type": "text", "text": text}
    if marks:
        node["marks"] = marks
    return node


def _parse_inline(text: str, marks: list[Node] | None = None) -> list[Node]:
    marks = marks or []
    out: list[Node] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            out.append(_text(text[pos:m.start()], marks))
        if m.group("code"):
            out.append(_text(m.group("code")[1:-1], [*marks, {"type": "code"}]))
        elif m.group("link"):
            out.extend(_parse_inline(m.group("ltext"), [*marks, {"type": "link", "attrs": {"href": m.group("href")}}]))
        elif m.group("bold"):
            out.extend(_parse_inline(m.group("btext"), [*marks, {"type": "strong"}]))
        elif m.group("strike"):
            out.extend(_parse_inline(m.group("stext"), [*marks, {"type": "strike"}]))
        else:
            out.extend(_parse_inline(m.group("etext"), [*marks, {"type": "em"}]))
        pos = m.end()
    if pos < len(text):
        out.append(_text(text[pos:], marks))
    return out


def _paragraph(lines: list[str]) -> Node:
    content: list[Node] = []
    for i, line in enumerate(lines):
        if i:
            content.append({"type": "hardBreak"})
        content.extend(_parse_inline(line))
    return {"type": "paragraph", "content": content}


_LIST_RE = re.compile(r"^(?P<indent>\s*)(?P<marker>[-*+]|\d+[.)])\s+(?P<text>.*)$")


def _parse_list(lines: list[str], i: int, base_indent: int) -> tuple[Node, int]:
    first = _LIST_RE.match(lines[i])
    assert first is not None
    ordered = first.group("marker")[0].isdigit()
    node: Node = {"type": "orderedList" if ordered else "bulletList", "content": []}
    if ordered:
        node["attrs"] = {"order": int(first.group("marker")[:-1])}
    while i < len(lines):
        m = _LIST_RE.match(lines[i])
        if not m:
            break
        indent = len(m.group("indent"))
        if indent < base_indent:
            break
        if indent > base_indent:
            sub, i = _parse_list(lines, i, indent)
            if node["content"]:
                node["content"][-1]["content"].append(sub)
            continue
        if m.group("marker")[0].isdigit() != ordered:
            break
        node["content"].append({"type": "listItem", "content": [_paragraph([m.group("text")])]})
        i += 1
    return node, i


def markdown_to_adf(md: str) -> Node:
    """Convert basic Markdown to an ADF ``doc`` node (Jira Cloud REST v3 bodies)."""
    lines = (md or "").replace("\r\n", "\n").split("\n")
    content: list[Node] = []
    para: list[str] = []
    i = 0

    def flush() -> None:
        if para:
            content.append(_paragraph(para))
            para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```"):
            flush()
            lang = stripped[3:].strip()
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            block: Node = {"type": "codeBlock", "content": [_text("\n".join(code))] if code else []}
            if lang:
                block["attrs"] = {"language": lang}
            content.append(block)
            continue
        h = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if h:
            flush()
            content.append({"type": "heading", "attrs": {"level": len(h.group(1))},
                            "content": _parse_inline(h.group(2))})
            i += 1
            continue
        if stripped in ("---", "***", "___"):
            flush()
            content.append({"type": "rule"})
            i += 1
            continue
        if _LIST_RE.match(line):
            flush()
            lst, i = _parse_list(lines, i, len(_LIST_RE.match(line).group("indent")))  # type: ignore[union-attr]
            content.append(lst)
            continue
        if not stripped:
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return {"type": "doc", "version": 1, "content": content}
