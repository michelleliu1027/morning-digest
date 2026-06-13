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
