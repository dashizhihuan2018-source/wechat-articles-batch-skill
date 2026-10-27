#!/usr/bin/env python3
"""Offline, structural WeChat body extraction from a Firecrawl response (stdlib)."""
import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from organize_exports import canonical_url, published_day, iso_time

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
DROP = {"script", "style", "noscript", "iframe", "form", "button", "input", "svg"}
BLOCK = {"p", "div", "section", "article", "header", "footer", "figure", "figcaption"}


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Element("root")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 100000 or len(self.stack) > 200:
            raise ValueError("HTML structural limit exceeded")
        node = Element(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def walk(node):
    if isinstance(node, Element):
        yield node
        for child in node.children:
            yield from walk(child)


def visible_walk(node):
    if isinstance(node, Element) and not hidden(node):
        yield node
        for child in node.children:
            yield from visible_walk(child)


def hidden(node):
    style = re.sub(r"\s+", "", node.attrs.get("style", "").lower())
    return (node.tag in DROP or "hidden" in node.attrs
            or node.attrs.get("aria-hidden") == "true"
            or "display:none" in style or "visibility:hidden" in style)


def plain(node):
    if isinstance(node, str):
        return node
    if hidden(node):
        return ""
    return "".join(plain(c) for c in node.children)


def compact(value):
    return re.sub(r"\s+", " ", value).strip()


def text_md(value):
    # Escape HTML and Markdown control characters; never preserve executable HTML.
    value = value.replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_\[\]#!|])", r"\\\1", value)


def safe_link(value, base):
    if not value or any(ord(c) < 32 for c in value):
        return ""
    target = urljoin(base, value.strip())
    try:
        u = urlsplit(target)
        if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
            return ""
        host = u.hostname.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return ""
        try:
            if not ipaddress.ip_address(host).is_global:
                return ""
        except ValueError:
            pass  # No DNS requests during offline conversion; enforce DNS controls at media fetch time.
    except ValueError:
        return ""
    # Encoding prevents Markdown destination/attribute breakout; this never fetches links.
    return target.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def render(node, base):
    if isinstance(node, str):
        return text_md(re.sub(r"\s+", " ", node))
    if hidden(node):
        return ""
    tag = node.tag
    if tag == "pre":
        code = plain(node).strip("\n")
        fence = "`" * max(3, max([len(x) + 1 for x in re.findall(r"`+", code)] or [3]))
        return f"\n\n{fence}\n{code}\n{fence}\n\n"
    if tag == "img":
        link = safe_link(node.attrs.get("data-src") or node.attrs.get("src"), base)
        return f"\n\n![{text_md(node.attrs.get('alt', '图片'))}]({link})\n\n" if link else ""
    if tag == "br":
        return "\n"
    if tag == "hr":
        return "\n\n---\n\n"
    if tag == "table":
        rows = []
        for tr in walk(node):
            if tr.tag == "tr":
                cells = [compact(render(c, base)) for c in tr.children
                         if isinstance(c, Element) and c.tag in {"td", "th"}]
                if cells:
                    rows.append(cells)
        if not rows:
            return ""
        width = max(map(len, rows))
        lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
        lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n\n" + "\n".join(lines) + "\n\n"
    inner = "".join(render(c, base) for c in node.children)
    if tag == "a":
        link = safe_link(node.attrs.get("href"), base)
        return f"[{inner.strip()}]({link})" if link and inner.strip() else inner
    if tag in {"strong", "b"} and inner.strip():
        return "**" + inner.strip() + "**"
    if tag in {"em", "i"} and inner.strip():
        return "*" + inner.strip() + "*"
    if re.fullmatch(r"h[1-6]", tag):
        return "\n\n" + "#" * int(tag[1]) + " " + inner.strip() + "\n\n"
    if tag == "blockquote":
        return "\n\n" + "\n".join("> " + x for x in inner.strip().splitlines()) + "\n\n"
    if tag in {"ul", "ol"}:
        items = [c for c in node.children if isinstance(c, Element) and c.tag == "li"]
        lines = []
        for i, item in enumerate(items, 1):
            content = render(item, base).strip().splitlines()
            marker = f"{i}. " if tag == "ol" else "- "
            lines.append(marker + (content[0] if content else ""))
            lines.extend("   " + x for x in content[1:])
        return "\n\n" + "\n".join(lines) + "\n\n"
    if tag in {"video", "audio", "mpvideo", "mp-common-videosnap", "mp-common-mpaudio", "mpvoice"}:
        return "\n\n[嵌入音视频未归档]\n\n"
    if tag in BLOCK:
        return "\n\n" + inner.strip() + "\n\n"
    return inner


def extract_response(payload, url, expected_account="", fetched_at=""):
    """Return organizer record plus QC. No Markdown fallback if body container is absent."""
    url, _ = canonical_url(url)
    iso_time(fetched_at)
    record = dict(url=url, title="", account="", author="", published_at="",
                  content_md="", status="error", tags=[],
                  fetched_at=fetched_at, collector="firecrawl-response/structural")
    qc = dict(reason="api_error", warnings=[], image_refs=0,
              latest_100_verified=False, offline_media_guaranteed=False)
    data = payload.get("data") or {}
    metadata = data.get("metadata") or {}
    status = metadata.get("statusCode")
    if status in {401, 403, 429}:
        record["status"], qc["reason"] = "blocked", "http_access_restriction"
        return record, qc
    code = str(payload.get("code", "")).upper()
    if any(s in code for s in ("RATE_LIMIT", "CAPTCHA", "BLOCKED", "UNAUTHORIZED", "FORBIDDEN")):
        record["status"], qc["reason"] = "blocked", "api_access_restriction"
        return record, qc
    if not payload.get("success") or (status is not None and status != 200):
        return record, qc
    html = data.get("html") or data.get("rawHtml") or ""
    if not isinstance(html, str) or len(html.encode()) > 8 * 1024 * 1024:
        qc["reason"] = "invalid_or_oversized_html"
        return record, qc
    tree = Tree()
    tree.feed(html)
    nodes = list(walk(tree.root))
    ids = {n.attrs.get("id"): n for n in nodes if n.attrs.get("id")}
    body = ids.get("js_content")
    if body is None:
        # Inspect page-level signals only when there is no actual article container.
        message = compact(plain(tree.root) + " " + str(data.get("markdown") or ""))[:3000]
        signals = ("环境异常", "访问过于频繁", "请完成验证", "安全验证", "验证码", "访问受限", "请在微信客户端打开")
        if any(s in message for s in signals):
            record["status"], qc["reason"] = "blocked", "verification_page"
        else:
            record["status"], qc["reason"] = "missing_body", "missing_js_content"
            # Firecrawl's cleaned HTML can remove the verification scripts and
            # all visible challenge text. Inspect retained raw HTML offline only;
            # a generic captcha library/word alone is not evidence of a challenge.
            raw = data.get("rawHtml") or ""
            if isinstance(raw, str) and len(raw.encode()) <= 8 * 1024 * 1024:
                template = "secitptpage/template/verify" in raw
                captcha = "TCaptcha.js" in raw or "cap_appid" in raw
                if template and captcha:
                    record["status"], qc["reason"] = "blocked", "verification_template"
        return record, qc
    def by_id(*names):
        return next((compact(plain(ids[n])) for n in names if n in ids and compact(plain(ids[n]))), "")
    record["title"] = by_id("activity-name", "js_title") or compact(str(metadata.get("title") or ""))
    record["account"] = by_id("js_name")
    record["author"] = by_id("js_author_name")
    # Never assume that a rendered clock has a known timezone; retain only its date.
    date_text = by_id("publish_time")
    m = re.search(r"(\d{4})[年/-](\d{1,2})[月/-](\d{1,2})", date_text)
    if m:
        candidate = f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
        try:
            published_day(candidate)
            record["published_at"] = candidate
        except ValueError:
            qc["warnings"].append("invalid_visible_date")
    if not record["published_at"]:
        qc["warnings"].append("published_at_missing")
    if not record["title"]:
        qc["warnings"].append("title_missing")
    if not record["account"]:
        qc["warnings"].append("account_missing")
    if expected_account and record["account"] != expected_account:
        qc["reason"] = "account_mismatch"
        return record, qc
    markdown = render(body, url).replace("\u200b", "").replace("\xa0", " ")
    markdown = re.sub(r"\n[ \t]+\n", "\n\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown).strip()
    text = compact(plain(body))
    images = [n for n in visible_walk(body) if n.tag == "img"]
    qc["image_refs"] = sum(bool(safe_link(n.attrs.get("data-src") or n.attrs.get("src"), url)) for n in images)
    if not text and not qc["image_refs"]:
        record["status"], qc["reason"] = "missing_body", "empty_body"
        return record, qc
    if not text:
        qc["warnings"].append("image_only_requires_visual_review")
    if any(n.attrs.get("rowspan") or n.attrs.get("colspan") for n in walk(body)):
        qc["warnings"].append("table_spans_require_review")
    if any(n.tag in {"video", "audio", "mpvideo", "mp-common-videosnap", "mp-common-mpaudio", "mpvoice"} for n in walk(body)):
        qc["warnings"].append("embedded_media_not_archived")
    record["content_md"], record["status"], qc["reason"] = markdown, "ok", "body_extracted"
    return record, qc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--response", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-account", default="")
    parser.add_argument("--fetched-at", default="", help="Known timezone-aware fetch time, not the cleaning time")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    record, qc = extract_response(json.loads(args.response.read_text(encoding="utf-8")), args.url, args.expected_account, args.fetched_at)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, value in (("normalized.jsonl", json.dumps(record, ensure_ascii=False) + "\n"),
                        ("quality.json", json.dumps(qc, ensure_ascii=False, indent=2) + "\n"),
                        ("body.md", record["content_md"] + "\n")):
        with (args.output / name).open("x", encoding="utf-8") as target:
            target.write(value)
        (args.output / name).chmod(0o600)
    print(json.dumps(dict(status=record["status"], **qc), ensure_ascii=False))
    return 0 if record["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
