from __future__ import annotations

from squadhq.trackers.adf import adf_to_markdown, markdown_to_adf

MD = """# Title

Some **bold** and *em* and `code` and [link](https://x.test) and ~~gone~~.
second line

- one
- two
  - nested

1. first
2. second

```python
print("hi")
```

---"""


def test_round_trip_preserves_structure():
    doc = markdown_to_adf(MD)
    types = [n["type"] for n in doc["content"]]
    assert types == ["heading", "paragraph", "bulletList", "orderedList", "codeBlock", "rule"]
    para = doc["content"][1]["content"]
    marks = {n["text"]: [m["type"] for m in n.get("marks", [])] for n in para if n["type"] == "text"}
    assert marks["bold"] == ["strong"] and marks["em"] == ["em"] and marks["code"] == ["code"]
    assert marks["link"] == ["link"] and marks["gone"] == ["strike"]
    assert {"type": "hardBreak"} in para
    nested = doc["content"][2]["content"][1]["content"][1]
    assert nested["type"] == "bulletList"
    assert doc["content"][4]["attrs"] == {"language": "python"}
    back = adf_to_markdown(doc)
    assert back == MD


def test_adf_edge_cases():
    assert adf_to_markdown(None) == ""
    assert adf_to_markdown("plain") == "plain"
    assert adf_to_markdown(5) == "5"
    doc = {"type": "doc", "content": [
        {"type": "blockquote", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "q"}]}]},
        {"type": "panel", "content": [{"type": "paragraph", "content": [
            {"type": "mention", "attrs": {"text": "@bob"}}, {"type": "emoji", "attrs": {"text": ":)"}},
            {"type": "inlineCard", "attrs": {"url": "https://u"}}, {"type": "status", "content": [{"type": "text", "text": "S"}]}]}]},
        {"type": "mediaSingle"},
        {"type": "bulletList", "content": [{"type": "listItem", "content": []}]},
        {"type": "orderedList", "attrs": {"order": 3}, "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x"}]}]}]},
        {"type": "weird", "content": [{"type": "text", "text": "inline only"}]},
        "junk",
    ]}
    assert adf_to_markdown(doc) == "> q\n\n@bob:)https://uS\n\n-\n\n3. x\n\ninline only"


def test_markdown_edge_cases():
    assert markdown_to_adf("")["content"] == []
    doc = markdown_to_adf("```\n```\n* a\n1) b")
    assert doc["content"][0] == {"type": "codeBlock", "content": []}
    assert [n["type"] for n in doc["content"][1:]] == ["bulletList", "orderedList"]
    assert markdown_to_adf("  - indented")["content"][0]["type"] == "bulletList"
