"""
Cleaning the HTML of updates (comments) before it is stored or shown.

Updates are rich text: bold, lists, links, @mentions, and the tables and quotes people pasted into
monday.com.  Only a short list of tags and attributes is kept; everything else is dropped (its text
stays), so nothing typed into or imported from an update can run script in someone else's browser.
Links keep only http(s) and mailto addresses and always open in a new tab.

A mention is <span class="mention" data-user="12">@Jo Citizen</span>; the user id says who is notified.
"""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED = {
    "p": (), "br": (), "b": (), "strong": (), "i": (), "em": (), "u": (), "s": (), "strike": (), "del": (),
    "ul": (), "ol": (), "li": (), "blockquote": (), "code": (), "pre": (), "h1": (), "h2": (), "h3": (), "h4": (),
    "table": (), "thead": (), "tbody": (), "tr": (), "td": ("colspan", "rowspan"), "th": ("colspan", "rowspan"),
    "a": ("href",), "span": ("class", "data-user"), "div": (), "hr": (), "sub": (), "sup": (),
}
VOID = {"br", "hr"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "template", "noscript", "svg", "math", "head", "title"}
MENTION_RE = re.compile(r'data-user="(\d+)"')


def _safe_href(href: str) -> str | None:
    h = (href or "").strip()
    if not h:
        return None
    try:
        scheme = urlsplit(h).scheme.lower()
    except ValueError:
        return None
    return h if scheme in ("http", "https", "mailto") else None


class _Cleaner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED:
            return
        kept = []
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a":
            href = _safe_href(a.get("href", ""))
            if href:
                kept.append(f'href="{html.escape(href, quote=True)}"')
                kept.append('target="_blank" rel="noopener noreferrer"')
        elif tag == "span":
            if a.get("class", "").split() == ["mention"] and a.get("data-user", "").isdigit():
                kept.append(f'class="mention" data-user="{a["data-user"]}"')
        else:
            for k in ALLOWED[tag]:
                if k in a and a[k].isdigit():
                    kept.append(f'{k}="{a[k]}"')
        self.out.append(f"<{tag}{' ' + ' '.join(kept) if kept else ''}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and self.stack and self.stack[-1] == tag.lower():
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def close(self):
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")


def clean_html(text: str, max_len: int = 200_000) -> str:
    c = _Cleaner()
    c.feed((text or "")[:max_len])
    c.close()
    return "".join(c.out).strip()


class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre", "hr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(text: str) -> str:
    p = _Text()
    p.feed(text or "")
    p.close()
    t = "".join(p.parts).replace(" ", " ").replace("﻿", "")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", t)).strip()


def text_to_html(text: str) -> str:
    """Plain text typed without the editor -> paragraphs."""
    paras = [p for p in re.split(r"\n{2,}", (text or "").strip()) if p.strip()]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras)


def mentioned_users(clean: str) -> list[int]:
    out = []
    for m in MENTION_RE.finditer(clean or ""):
        u = int(m.group(1))
        if u not in out:
            out.append(u)
    return out
