#!/usr/bin/env python3
"""Deterministic, local-only WeChat export organizer; no network or login code."""
import argparse
from collections import Counter
from datetime import date, datetime
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

FIELDS = {"url", "title", "account", "author", "published_at", "content_md",
          "status", "tags", "fetched_at", "collector"}
STATUSES = {"ok", "missing_body", "blocked", "deleted", "error"}
SHANGHAI = ZoneInfo("Asia/Shanghai")


def canonical_url(value):
    """Return a public URL and identity; never persist unknown query parameters."""
    if not isinstance(value, str):
        raise ValueError("url must be a string")
    value = html.unescape(value.strip())
    if any(ord(c) < 32 for c in value):
        raise ValueError("invalid URL characters")
    u = urlsplit(value)
    if (u.scheme not in {"http", "https"} or u.hostname != "mp.weixin.qq.com"
            or u.username or u.password or u.port):
        raise ValueError("only public mp.weixin.qq.com article URLs are accepted")
    if re.fullmatch(r"/s/[A-Za-z0-9_-]+", u.path):
        return "https://mp.weixin.qq.com" + u.path, "short:" + u.path[3:]
    if u.path != "/s":
        raise ValueError("unsupported article URL path")
    q = parse_qs(u.query, keep_blank_values=True)

    def one(key, alias=None, default=None):
        values = q.get(key, q.get(alias, [default]) if alias else [default])
        if len(values) != 1:
            raise ValueError("ambiguous article URL identity")
        if alias in q and key in q and q[alias] != q[key]:
            raise ValueError("conflicting article URL identity aliases")
        return values[0]

    biz, mid, idx = one("__biz"), one("mid", "appmsgid"), one("idx", "itemidx", "1")
    if (not biz or not re.fullmatch(r"[A-Za-z0-9_+/=-]{1,200}", biz)
            or not mid or not re.fullmatch(r"[0-9]{1,30}", mid)
            or not idx or not re.fullmatch(r"[0-9]{1,6}", idx) or int(idx) < 1):
        raise ValueError("missing or invalid article identity")
    mid, idx = str(int(mid)), str(int(idx))
    params = {"__biz": biz, "mid": mid, "idx": idx}
    sn = one("sn")
    if sn:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sn):
            raise ValueError("invalid public article signature")
        params["sn"] = sn
    return "https://mp.weixin.qq.com/s?" + urlencode(params), f"long:{biz}:{mid}:{idx}"


def iso_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("invalid ISO timestamp") from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return result


def published_day(value):
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    return iso_time(value).astimezone(SHANGHAI).date()


def normalize(raw):
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError("expected an object containing only documented fields")
    r = {key: raw.get(key, "") for key in FIELDS - {"tags", "status"}}
    if any(not isinstance(v, str) for v in r.values()):
        raise ValueError("text fields must be strings (use empty string for missing values)")
    r["url"], r["id"] = canonical_url(r["url"])
    r["date"] = str(published_day(r["published_at"]) or "")
    iso_time(r["fetched_at"])
    tags = raw.get("tags", [])
    if not isinstance(tags, list) or any(not isinstance(t, str) for t in tags):
        raise ValueError("tags must be an array of strings")
    r["tags"] = sorted(set(tags))
    status = raw.get("status", "ok")
    if not isinstance(status, str) or status not in STATUSES:
        raise ValueError("unsupported article status")
    r["status"] = "missing_body" if status == "ok" and not r["content_md"].strip() else status
    if r["status"] != "ok":
        r["content_md"] = ""
    return r


def rank(record):
    fetched = iso_time(record["fetched_at"])
    return record["status"] == "ok", fetched.timestamp() if fetched else float("-inf")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def md_label(value):
    value = html.escape(value.replace("\n", " ").replace("\r", " "))
    return re.sub(r"([\\`*_{}\[\]()!|])", r"\\\1", value)


def csv_safe(value):
    value = str(value)
    if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def build_files(input_path, since=None, until=None, keyword=None):
    counts = Counter()
    input_statuses = Counter()
    unique = {}
    with Path(input_path).open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            counts["input_rows"] += 1
            try:
                record = normalize(json.loads(line))
            except (ValueError, TypeError, OverflowError):
                # Never echo raw records/URLs: they may contain credentials.
                raise ValueError(f"invalid input at line {line_number}; check the data contract") from None
            input_statuses[record["status"]] += 1
            day = date.fromisoformat(record["date"]) if record["date"] else None
            if (since or until) and not day:
                counts["filtered_unknown_date"] += 1
                continue
            if ((since and day < since) or (until and day > until)
                    or (keyword and keyword.casefold() not in
                        (record["title"] + "\n" + record["content_md"]).casefold())):
                counts["filtered_other"] += 1
                continue
            counts["selected_rows"] += 1
            previous = unique.get(record["id"])
            if previous:
                counts["duplicate_identity_rows"] += 1
            if previous is None or rank(record) >= rank(previous):
                unique[record["id"]] = record
    files, catalog, body_ids = {}, [], {}
    index = ["# 公众号文章批次索引", "", "仅覆盖本批输入；不代表公众号全量历史。", ""]
    records = sorted(unique.values(), key=lambda r: (r["date"], r["id"]))
    statuses = Counter()
    for record in records:
        r = dict(record)
        body = r.pop("content_md")
        r["content_sha256"] = digest(body) if body else None
        r["same_content_as"] = None
        r["path"] = None
        statuses[r["status"]] += 1
        if body:
            key = r["content_sha256"]
            r["same_content_as"] = body_ids.get(key)
            body_ids.setdefault(key, r["id"])
            path = f"articles/{r['date'] or 'undated'}-{digest(r['id'])[:24]}.md"
            r["path"] = path
            frontmatter = "\n".join(f"{k}: {json_text(v)}" for k, v in sorted(r.items()))
            if path in files:
                raise ValueError("output identity collision")
            files[path] = ("---\n" + frontmatter + "\n---\n\n" + body.rstrip() + "\n").encode()
        catalog.append(r)
        label = md_label(r["title"] or "未命名文章")
        target = r["path"] or r["url"]
        index.append(f"- [{label}]({target}) · {md_label(r['account'])} · {r['date'] or '日期缺失'} · {r['status']}")
    for key in ("input_rows", "selected_rows", "duplicate_identity_rows", "filtered_unknown_date", "filtered_other"):
        counts.setdefault(key, 0)
    report = dict(counts, unique_articles=len(catalog), statuses=dict(sorted(statuses.items())),
                  input_statuses=dict(sorted(input_statuses.items())),
                  coverage="input-set-only; platform total and history completeness unknown",
                  online_collection_tested=False, offline_media_guaranteed=False)
    files["catalog.jsonl"] = ("\n".join(map(json_text, catalog)) + ("\n" if catalog else "")).encode()
    files["report.json"] = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    files["INDEX.md"] = ("\n".join(index) + "\n").encode()
    table = io.StringIO(newline="")
    headers = ["title", "account", "author", "published_at", "url", "status", "path", "tags"]
    writer = csv.DictWriter(table, fieldnames=headers)
    writer.writeheader()
    for row in catalog:
        writer.writerow({k: csv_safe(";".join(row[k]) if k == "tags" else row[k] or "") for k in headers})
    files["catalog.csv"] = table.getvalue().encode("utf-8-sig")
    return files, report


def write_files(output, files):
    out = Path(output)
    if out.is_symlink():
        raise ValueError("refusing symlink output")
    if out.exists():
        if not out.is_dir():
            raise ValueError("output must be a directory")
        entries = list(out.rglob("*"))
        if any(p.is_symlink() for p in entries):
            raise ValueError("refusing symlinks in existing output")
        existing = {p.relative_to(out).as_posix(): p for p in entries if p.is_file()}
        if set(existing) == set(files) and all(existing[k].read_bytes() == v for k, v in files.items()):
            return "unchanged"
        raise ValueError("output already exists with different content; choose a new batch directory")
    out.mkdir(parents=True, exist_ok=False)
    for relative, content in files.items():
        destination = out / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target:
            target.write(content)
    return "created"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--since", type=date.fromisoformat)
    parser.add_argument("--until", type=date.fromisoformat)
    parser.add_argument("--keyword")
    args = parser.parse_args()
    if args.since and args.until and args.since > args.until:
        parser.error("--since must not be after --until")
    try:
        files, report = build_files(args.input, args.since, args.until, args.keyword)
        result = write_files(args.output, files)
    except (ValueError, OSError) as error:
        # OS error messages can contain sensitive local paths; do not emit them.
        print(str(error) if isinstance(error, ValueError) else "local I/O failed; no automatic cleanup performed", file=sys.stderr)
        return 2
    print(json_text(dict(result=result, **report)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
