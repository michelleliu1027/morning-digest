"""Convert GitHub/CommonMark markdown into Slack mrkdwn.

Claude tends to emit GitHub-flavored markdown (**bold**, [text](url), # headings).
Slack uses a different dialect (*bold*, <url|text>, no headings). This module
does the translation deterministically so the digest renders cleanly regardless
of how the model formats its output.
"""

import re

# [text](url) -> <url|text>   (do this before bold/italic so we don't mangle it)
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
# **bold** or __bold__ -> *bold*
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
# leading markdown heading (#, ##, ...) -> bold line
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)


def to_slack_mrkdwn(text: str) -> str:
    # 1. links: [t](u) -> <u|t>
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)
    # 2. headings: "## Foo" -> "*Foo*"
    text = _HEADING_RE.sub(lambda m: f"*{m.group(1).strip()}*", text)
    # 3. bold: **x**/__x__ -> *x*
    text = _BOLD_RE.sub(lambda m: f"*{m.group(2)}*", text)
    return text


# A whole-line bold like "*:sunrise: Morning digest*" is a section header,
# not inline bold inside a bullet (those have other text around the *...*).
_HEADER_LINE_RE = re.compile(r"^\*[^*\n]+\*$")
# A markdown bullet: optional indent, then "- " or "* ".
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")

_SECTION_CHAR_LIMIT = 2900  # Slack section text hard limit is 3000


def _bulletize(line: str) -> str:
    """Turn a markdown '- '/'* ' bullet into a real Slack bullet glyph.

    Slack mrkdwn does NOT auto-format '-' as a bullet, so the digest looked like
    a wall of dashes. Top level -> '•', nested (indented) -> '◦' with padding.
    """
    m = _BULLET_RE.match(line)
    if not m:
        return line
    indent, rest = m.groups()
    depth = len(indent) // 2
    if depth >= 1:
        return f"{'    ' * depth}◦ {rest}"
    return f"• {rest}"


def _chunk(lines: list[str]) -> list[str]:
    """Join body lines into <=_SECTION_CHAR_LIMIT chunks (Slack per-block cap)."""
    chunks, buf, size = [], [], 0
    for ln in lines:
        if size + len(ln) + 1 > _SECTION_CHAR_LIMIT and buf:
            chunks.append("\n".join(buf))
            buf, size = [], 0
        buf.append(ln)
        size += len(ln) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def to_slack_blocks(text: str) -> list[dict]:
    """Render the digest as Block Kit: a header, then one section per group.

    Splits the text on whole-line bold headers (the '*:emoji: Title*' lines the
    prompt emits), drops a divider between groups, and converts '-' bullets to
    real glyphs so the message is scannable instead of a flat block of text.
    """
    text = to_slack_mrkdwn(text)
    title: str | None = None
    sections: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None

    for raw in text.split("\n"):
        s = raw.strip()
        if not s:
            continue
        if _HEADER_LINE_RE.match(s):
            head = s.strip("*").strip()
            if title is None:
                title = head
            else:
                cur = []
                sections.append((head, cur))
            continue
        line = _bulletize(raw)
        if cur is None:  # body before any header -> implicit lead section
            cur = []
            sections.append(("", cur))
        cur.append(line)

    blocks: list[dict] = []
    if title:
        blocks.append({"type": "header",
                       "text": {"type": "plain_text", "text": title[:150], "emoji": True}})
    for head, body in sections:
        blocks.append({"type": "divider"})
        prefix = f"*{head}*\n" if head else ""
        chunks = _chunk(body) or [""]
        for i, chunk in enumerate(chunks):
            section_text = (prefix if i == 0 else "") + chunk
            if not section_text.strip():
                continue
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": section_text}})
    return blocks
