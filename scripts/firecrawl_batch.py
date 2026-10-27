#!/usr/bin/env python3
"""Single-worker public-link collector for an explicitly authorized local Firecrawl."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener

from clean_firecrawl import extract_response
from organize_exports import canonical_url, build_files, write_files

MAX_RESPONSE = 8 * 1024 * 1024


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def private_write(path, value):
    # Checkpoint replacement is atomic; caller holds the per-output process lock.
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(encoded(value))
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def load_manifest(path):
    rows, seen, count = [], set(), 0
    with Path(path).open(encoding="utf-8-sig") as source:
        for line in source:
            if not line.strip():
                continue
            count += 1
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) - {"url", "discovery_source"}:
                raise ValueError("manifest accepts only url and discovery_source")
            url, identity = canonical_url(row.get("url"))
            discovery = row.get("discovery_source", "")
            if not isinstance(discovery, str):
                raise ValueError("invalid discovery_source")
            if identity not in seen:
                rows.append(dict(url=url, discovery_source=discovery))
                seen.add(identity)
    return rows, count


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_api(api_url):
    u = urlsplit(api_url)
    if (u.scheme != "http" or u.hostname != "127.0.0.1" or u.username or u.password
            or u.path not in {"", "/"} or u.query or u.fragment or not u.port):
        raise ValueError("API must be explicit http://127.0.0.1:PORT; no hosted fallback")
    return api_url.rstrip("/")


def local_scrape(api_url, url):
    api_url = validate_api(api_url)
    body = encoded(dict(url=url, formats=["markdown", "html", "rawHtml"], proxy="basic", timeout=60000, maxAge=0))
    request = Request(api_url + "/v2/scrape", data=body, headers={"Content-Type": "application/json"})
    # Do not let shell proxy variables send this local request outside the machine.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=70) as response:
            raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            return dict(success=False, code="OVERSIZED_RESPONSE")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("non-object response")
        return result
    except HTTPError as error:
        # Do not persist server error text, request headers, or redirect locations.
        return dict(success=False, code="HTTP_ERROR", data=dict(metadata=dict(statusCode=error.code)))
    except (URLError, TimeoutError, OSError, ValueError, HTTPException):
        return dict(success=False, code="TRANSPORT_OR_JSON_ERROR")


def collect(rows, output, api_url, expected_account, *, limit=5, sample_verified=False,
            interval=5, resume=False, input_rows=None, scraper=local_scrape, sleeper=time.sleep):
    api_url = validate_api(api_url)
    if not expected_account.strip():
        raise ValueError("expected account is required")
    if not rows:
        raise ValueError("manifest has no public article links")
    if not 1 <= limit <= 100 or (limit > 5 and not sample_verified):
        raise ValueError("run 3-5 real samples before explicitly enabling a larger batch")
    if not 1 <= interval <= 60:
        raise ValueError("interval must be between 1 and 60 seconds; not a safety guarantee")
    selected = rows[:limit]
    output = Path(output)
    if output.is_symlink():
        raise ValueError("refusing symlink output")
    if output.exists() and not resume:
        raise ValueError("output exists; choose a new private batch or explicitly resume")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(p.is_symlink() for p in output.rglob("*")):
        raise ValueError("refusing symlinks in output")
    lock = os.open(output / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        signature = hashlib.sha256(encoded(dict(rows=selected, account=expected_account, api=api_url))).hexdigest()
        state_path = output / "checkpoint.json"
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get("signature") != signature:
                raise ValueError("resume manifest, limit, account, or endpoint changed")
            if state.get("stopped_reason"):
                raise ValueError("previous batch stopped; review the cause before a separate authorized task")
        else:
            if resume:
                raise ValueError("no checkpoint to resume")
            state = dict(signature=signature, completed=[], stopped_reason=None)
            private_write(state_path, state)
        raw_dir = output / ".firecrawl"
        raw_dir.mkdir(exist_ok=True, mode=0o700)
        attempted = 0
        for index in range(len(state["completed"]), len(selected)):
            row = selected[index]
            raw_path = raw_dir / f"{index:04d}.json"
            recovered = raw_path.exists()
            if recovered:
                # An interrupt between raw save and checkpoint must not repeat the request.
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
                fetched_at = datetime.fromtimestamp(raw_path.stat().st_mtime, timezone.utc).isoformat()
            else:
                if attempted:
                    sleeper(interval)
                fetched_at = datetime.now(timezone.utc).isoformat()
                payload = scraper(api_url, row["url"])
                attempted += 1
                private_write(raw_path, payload)
            try:
                record, quality = extract_response(payload, row["url"], expected_account, fetched_at)
            except (ValueError, TypeError, AttributeError, RecursionError):
                record, quality = extract_response(dict(success=False), row["url"], expected_account, fetched_at)
                quality["reason"] = "invalid_response_structure"
            record["collector"] = "firecrawl-local/basic"
            item = dict(record=record, quality=quality, raw_file=str(raw_path.relative_to(output)),
                        raw_sha256=hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                        recovered_saved_response=recovered,
                        discovery_source=row["discovery_source"])
            state["completed"].append(item)
            reason = quality["reason"]
            if record["status"] == "blocked" or reason == "account_mismatch":
                state["stopped_reason"] = reason
            elif record["status"] == "error":
                # Conservatively halt on ambiguous API errors; no retry storm or mode escalation.
                state["stopped_reason"] = "api_or_response_error"
            elif record["status"] == "missing_body":
                # An empty cleaned page can hide a verification template. Review
                # the saved response offline instead of probing the next article.
                state["stopped_reason"] = "body_missing_requires_review"
            private_write(state_path, state)
            if state["stopped_reason"]:
                break
        records = [x["record"] for x in state["completed"]]
        normalized = output / "normalized.jsonl"
        fd = os.open(normalized, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as target:
            for record in records:
                target.write(encoded(record))
        statuses = Counter(r["status"] for r in records)
        report = dict(input_rows=input_rows if input_rows is not None else len(rows),
                      unique_discovered=len(rows), selected=len(selected), processed=len(records),
                      not_selected=max(0, len(rows) - len(selected)),
                      pending=len(selected) - len(records), statuses=dict(statuses),
                      stopped_reason=state["stopped_reason"],
                      metadata_incomplete=sum(not all(x["record"][k] for k in ("title", "account", "published_at"))
                                              for x in state["completed"]),
                      articles_requiring_review=sum(bool(x["quality"]["warnings"]) for x in state["completed"]),
                      requested_account=expected_account, sample_verified_by_operator=sample_verified,
                      updated_at=datetime.now(timezone.utc).isoformat(),
                      history_list_verified=False, latest_100_verified=False,
                      coverage="provided-link-set-only; not proof of latest-100 coverage",
                      offline_media_guaranteed=False)
        private_write(output / "batch-report.json", report)
        archive_files, _ = build_files(normalized)
        # If interrupted before archive creation, resume can finish it; different archives are not overwritten.
        write_files(output / "archive", archive_files)
        return report
    finally:
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--api-url", default="http://127.0.0.1:3002")
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--sample-verified", action="store_true")
    parser.add_argument("--authorize-public-fetch", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if not args.authorize_public_fetch:
        parser.error("explicit --authorize-public-fetch is required; local parsing needs no such flag")
    try:
        rows, count = load_manifest(args.manifest)
        report = collect(rows, args.output, args.api_url, args.expected_account,
                         limit=args.limit, sample_verified=args.sample_verified,
                         interval=args.interval, resume=args.resume, input_rows=count)
    except (ValueError, OSError, TypeError):
        print("Batch failed: inspect private inputs/checkpoint; no automatic retry or cleanup.")
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["stopped_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
