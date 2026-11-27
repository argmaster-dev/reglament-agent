"""Extract the expert's own words from a reply letter: no quoted original, no signature, no tags."""
from __future__ import annotations

import re
from html.parser import HTMLParser

_ORIGINAL = re.compile(r"^\s*[-_=]{2,}\s*(original message|исходное сообщение|пересылаемое сообщение|"
                       r"forwarded message|первоначальное сообщение)\s*[-_=]{2,}\s*$", re.I)
_WROTE = re.compile(r"(wrote|пишет|написал[аи]?|schrieb|a écrit)\s*:\s*$", re.I)
_WROTE_START = re.compile(r"^\s*(on|в|le|am)\b", re.I)
_HEADER = re.compile(r"^\s*(от|from|отправлено|sent|дата|date|кому|to|копия|cc|тема|subject)\s*:", re.I)
_SIGNATURE = re.compile(r"^--\s*$")
_DEVICE_SIG = re.compile(r"^\s*(отправлено с (моего )?(iphone|ipad|android|телефона|мобильного)|"
                         r"sent from my |получено из |get outlook for )", re.I)
_OUR_LETTER = re.compile(r"^\s*Обращение №\s*\d{4}-\d{4,}\s*$")


def _cut_index(lines: list[str]) -> int:
    """Index of the first line that starts quoted material or a signature (len(lines) if none)."""
    for i, line in enumerate(lines):
        s = line.rstrip()
        if _ORIGINAL.match(s) or _SIGNATURE.match(s) or _DEVICE_SIG.match(s):
            return i
        if _WROTE.search(s):
            # «В пт, 27 нояб. 2026 г. в 10:00, Имя <a@b> пишет:» is often wrapped onto two lines.
            if i > 0 and not _WROTE_START.match(s) and _WROTE_START.match(lines[i - 1]):
                return i - 1
            return i
        if _HEADER.match(s):
            following = [x for x in lines[i + 1:i + 5] if x.strip()][:3]
            if any(_HEADER.match(x) for x in following):
                return i
        if _OUR_LETTER.match(s):
            nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            if nxt == "Вопрос:":
                return i
    return len(lines)


def clean_plain(text: str) -> str:
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if not ln.lstrip().startswith(">")]
    lines = lines[:_cut_index(lines)]
    out = "\n".join(lines).strip()
    out = re.sub(r"(?:\n[ \t]*){3,}", "\n\n", out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    return re.sub(r"(?:^|\n)[ \t]*[_=-]{3,}\s*$", "", out).strip()


_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "pre"}
_SKIP_TAGS = {"script", "style", "head", "title"}


class _Html(HTMLParser):
    """Text of an HTML letter without quoted blocks (blockquote, gmail_quote, Outlook reply header)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_tag: str | None = None
        self.skip_depth = 0
        self.stopped = False

    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        a = dict(attrs)
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        marks = f"{a.get('class') or ''} {a.get('id') or ''}".lower()
        if tag in _SKIP_TAGS or tag == "blockquote" or any(
                m in marks for m in ("gmail_quote", "moz-cite-prefix", "yahoo_quoted")):
            self.skip_tag, self.skip_depth = tag, 1
        elif any(m in marks for m in ("divrplyfwdmsg", "appendonsend")):
            self.stopped = True
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip_tag and not self.stopped:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.stopped:
            return
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip_tag = None
            return
        if tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip_tag and not self.stopped:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _Html()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(lines)


def extract_answer(text: str | None, html: str | None = None, limit: int = 20000) -> str:
    """Plain text wins; HTML is converted only when there is no usable plain part."""
    for candidate in ((text or ""), html_to_text(html) if html else ""):
        cleaned = clean_plain(candidate) if candidate.strip() else ""
        if cleaned:
            return cleaned[:limit]
    return ""
