#!/usr/bin/env python3
"""Bulk download images from a URL list with Python (text file or CSV).

Reads image URLs from a plain text file (one URL per line) or a CSV (the
``url`` column, or the first column), downloads each one with retries and a
concurrency limit, checks that the bytes really are an image, de-duplicates
by SHA-256, and writes a ``manifest.csv``.

Standard library only. MIT licensed.
https://github.com/fecklesspleb/bulk-image-downloader
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import http.client
import json
import os
import re
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

__version__ = "1.0.0"

USER_AGENT = (
    "bulk-image-downloader/%s (+https://github.com/fecklesspleb/bulk-image-downloader)"
    % __version__
)

MANIFEST_FIELDS = [
    "url",
    "final_url",
    "status",
    "file",
    "content_type",
    "bytes",
    "width",
    "height",
    "sha256",
    "error",
]

EXT_BY_TYPE = {
    "png": ".png",
    "jpeg": ".jpg",
    "gif": ".gif",
    "webp": ".webp",
    "svg": ".svg",
    "avif": ".avif",
    "bmp": ".bmp",
    "ico": ".ico",
}
TYPE_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/pjpeg": "jpeg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/avif": "avif",
    "image/bmp": "bmp",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}


# --------------------------------------------------------------------------
# Image sniffing and sizes (header only, no decoding)


def sniff_type(data: bytes):
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if len(data) >= 12 and data[4:8] == b"ftyp" and b"avif" in data[8:20]:
        return "avif"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith((b"\x00\x00\x01\x00", b"\x00\x00\x02\x00")):
        return "ico"
    head = data.lstrip()[:256].lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in head):
        return "svg"
    return None


def image_size(data: bytes, kind=None):
    kind = kind or sniff_type(data)
    if kind == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if kind == "gif" and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if kind == "bmp" and len(data) >= 26:
        return struct.unpack("<ii", data[18:26])
    if kind == "jpeg":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                break
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                return struct.unpack(">HH", data[i + 5 : i + 9])[::-1]
            if marker == 0xD8 or marker == 0xD9 or (0xD0 <= marker <= 0xD7):
                i += 2
                continue
            if i + 3 >= len(data):
                break
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + length
        return None, None
    if kind == "webp" and len(data) >= 30:
        tag = data[12:16]
        if tag == b"VP8X" and len(data) >= 30:
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return w, h
        if tag == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            return struct.unpack("<HH", data[26:30])
        if tag == b"VP8L" and len(data) >= 25 and data[20:21] == b"\x2f":
            bits = struct.unpack("<I", data[21:25])[0]
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == "svg":
        text = data[:4096].decode("utf-8", "replace")
        m = re.search(r'\bviewBox\s*=\s*["\']\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)', text, re.I)
        view = (float(m.group(3)), float(m.group(4))) if m else (None, None)

        def px(attr):
            m = re.search(r'\b%s\s*=\s*["\']\s*([-\d.]+)\s*(px)?\s*["\']' % attr, text, re.I)
            if not m:
                return None
            if m.group(2) is None:
                # bare number is pixels; units other than px mean "unknown"
                unit = re.search(r'\b%s\s*=\s*["\']\s*[-\d.]+\s*([a-z%%]+)' % attr, text, re.I)
                if unit and unit.group(1).lower() not in ("px",):
                    return None
            return int(float(m.group(1)))

        w, h = px("width"), px("height")
        if w is not None and h is not None:
            return w, h
        if w is None and h is None and view[0] is not None:
            # only use viewBox when there is no non-pixel unit on width/height
            if re.search(r'\bwidth\s*=\s*["\'][^"\']*(?:em|ex|%|pt|pc|in|cm|mm)', text, re.I):
                return None, None
            return int(view[0]), int(view[1])
        return w, h
    if kind == "ico" and len(data) >= 8:
        return data[6] or 256, data[7] or 256
    return None, None


# --------------------------------------------------------------------------
# Input: text file or CSV


def load_urls(path: str):
    """Return a de-duplicated list of http(s) URLs from a text file or CSV.

    Blank lines and ``#`` comments are ignored in text files. For CSV, the
    ``url`` / ``URL`` / ``image_url`` column is used when present; otherwise
    the first column is used.
    """
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        # Heuristic: looks like CSV with a header that mentions url, or has commas
        # on the first non-empty line and more than one field.
        first = ""
        for line in sample.splitlines():
            if line.strip() and not line.strip().startswith("#"):
                first = line
                break
        is_csv = False
        if first:
            lower = first.lower()
            if "," in first and any(h in lower for h in ("url", "link", "image")):
                is_csv = True
            elif first.count(",") >= 1 and re.match(r'^"?[A-Za-z_][A-Za-z0-9_ ]*"?,', first):
                # header-ish first row
                try:
                    dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
                    is_csv = csv.Sniffer().has_header(sample) or dialect.delimiter in ",\t;"
                except csv.Error:
                    is_csv = False
        urls = []
        seen = set()
        if is_csv:
            reader = csv.DictReader(f)
            if reader.fieldnames:
                fields = list(reader.fieldnames)
                key = None
                for candidate in ("url", "URL", "image_url", "imageUrl", "link", "src"):
                    if candidate in fields:
                        key = candidate
                        break
                if key is None:
                    # case-insensitive match, else first column
                    lower_map = { (n or "").strip().lower(): n for n in fields }
                    for candidate in ("url", "image_url", "imageurl", "link", "src", "href"):
                        if candidate in lower_map:
                            key = lower_map[candidate]
                            break
                if key is None:
                    key = fields[0]
                for row in reader:
                    u = (row.get(key) or "").strip()
                    if u and not u.startswith("#"):
                        if u not in seen:
                            seen.add(u)
                            urls.append(u)
            else:
                f.seek(0)
                for row in csv.reader(f):
                    if not row:
                        continue
                    u = (row[0] or "").strip()
                    if u and not u.startswith("#") and u.lower() != "url":
                        if u not in seen:
                            seen.add(u)
                            urls.append(u)
        else:
            for line in f:
                u = line.strip()
                if not u or u.startswith("#"):
                    continue
                # allow a single-column CSV-ish line with quotes
                if u.startswith('"') and u.endswith('"') and u.count('"') == 2:
                    u = u[1:-1]
                if u not in seen:
                    seen.add(u)
                    urls.append(u)
    return urls


def url_problem(url: str):
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port
    except ValueError as e:
        return "invalid URL: %s" % e
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "invalid URL: no http(s) host"
    return None


def safe_name(url: str, sha: str, kind_ext: str):
    """``<last path part, cleaned>-<first 10 hex of sha256><ext>``."""
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    stem = os.path.basename(path.rstrip("/")) or "image"
    stem, ext = os.path.splitext(stem)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:60] or "image"
    ext = kind_ext or (ext.lower() if re.match(r"^\.[a-z0-9]{1,5}$", ext.lower()) else ".bin")
    return "%s-%s%s" % (stem, sha[:10], ext)


def host_dir(url: str):
    """Return a single path segment for the URL host under the output folder.

    Never returns ``.`` or ``..`` (or a string made only of dots), so a bizarre
    netloc like ``..`` cannot escape the output directory.
    """
    host = urllib.parse.urlsplit(url).netloc.lower()
    host = re.sub(r"[^A-Za-z0-9.-]+", "_", host) or "files"
    if set(host) <= {"."}:
        host = "files"
    return host


# --------------------------------------------------------------------------
# HTTP with retries and per-host spacing


class Fetcher:
    """HTTP GET with a User-Agent, per-host gap, and retries on 429/5xx."""

    def __init__(self, delay=0.25, timeout=30, retries=3, opener=None, sleep=time.sleep, clock=time.monotonic):
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.opener = opener or urllib.request.build_opener()
        self.sleep = sleep
        self.clock = clock
        self._last = {}
        self._lock = threading.Lock()

    def _wait_for_host(self, url):
        host = urllib.parse.urlsplit(url).netloc.lower()
        with self._lock:
            last = self._last.get(host)
            now = self.clock()
            wait = 0.0
            if last is not None:
                wait = self.delay - (now - last)
            # reserve the slot now so concurrent workers on the same host space out
            self._last[host] = now + (wait if wait > 0 else 0)
        if wait > 0:
            self.sleep(wait)

    def get(self, url, max_bytes):
        """Return ``(final_url, content_type, body, too_big)``."""
        attempt = 0
        while True:
            self._wait_for_host(url)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                    length = resp.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        return resp.geturl(), ctype, b"", True
                    body = resp.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        return resp.geturl(), ctype, b"", True
                    if length and length.isdigit() and len(body) < int(length):
                        raise http.client.IncompleteRead(body, int(length) - len(body))
                    return resp.geturl(), ctype, body, False
            except urllib.error.HTTPError as e:
                if (e.code == 429 or 500 <= e.code < 600) and attempt < self.retries:
                    retry_after = e.headers.get("Retry-After") if e.headers else None
                    wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2 ** attempt
                    self.sleep(min(wait, 60))
                    attempt += 1
                    continue
                raise


def _err(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "HTTP %s" % exc.code
    if isinstance(exc, http.client.IncompleteRead):
        return "incomplete read"
    return "%s: %s" % (type(exc).__name__, exc)


# --------------------------------------------------------------------------
# Downloader


class Downloader:
    def __init__(
        self,
        out_dir,
        fetcher,
        min_bytes=0,
        max_bytes=10 * 1024 * 1024,
        concurrency=4,
        log=None,
    ):
        self.out_dir = out_dir
        self.fetcher = fetcher
        self.min_bytes = min_bytes
        self.max_bytes = max_bytes
        self.concurrency = max(1, int(concurrency))
        self.log = log or (lambda msg: None)
        self.rows = []
        self.by_sha = {}
        self.saved = 0
        self._lock = threading.Lock()

    def _row(self, **kw):
        row = dict.fromkeys(MANIFEST_FIELDS, "")
        row.update(kw)
        with self._lock:
            self.rows.append(row)
        return row

    def process_one(self, url):
        problem = url_problem(url)
        if problem:
            self._row(url=url, status="error", error=problem)
            self.log("error %s: %s" % (url, problem))
            return
        try:
            final, ctype, body, too_big = self.fetcher.get(url, self.max_bytes)
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as e:
            self._row(url=url, status="error", error=_err(e))
            self.log("error %s: %s" % (url, _err(e)))
            return
        base = dict(url=url, final_url=final, content_type=ctype)
        if too_big:
            self._row(status="too_large", **base)
            self.log("too_large %s" % url)
            return
        kind = sniff_type(body) or TYPE_BY_MIME.get(ctype)
        # Content-type / bytes check: must look like an image.
        if not kind or kind not in EXT_BY_TYPE:
            # HTML error pages saved as .jpg are the usual failure mode.
            if ctype in ("text/html", "application/xhtml+xml") or body[:512].lstrip().lower().startswith(
                (b"<!doctype html", b"<html")
            ):
                self._row(status="not_an_image", bytes=len(body), **base)
            else:
                self._row(status="not_an_image", bytes=len(body), **base)
            self.log("not_an_image %s (%s)" % (url, ctype or "unknown"))
            return
        # Prefer sniff over a misleading Content-Type (e.g. application/octet-stream).
        width, height = image_size(body, kind)
        sha = hashlib.sha256(body).hexdigest()
        base.update(bytes=len(body), width=width or "", height=height or "", sha256=sha)
        with self._lock:
            if sha in self.by_sha:
                existing = self.by_sha[sha]
                self.rows.append(
                    dict(
                        dict.fromkeys(MANIFEST_FIELDS, ""),
                        status="duplicate",
                        file=existing,
                        **base,
                    )
                )
                self.log("duplicate %s -> %s" % (url, existing))
                return
            if len(body) < self.min_bytes:
                self.rows.append(dict(dict.fromkeys(MANIFEST_FIELDS, ""), status="too_small", **base))
                self.log("too_small %s (%d bytes)" % (url, len(body)))
                return
            name = safe_name(final or url, sha, EXT_BY_TYPE.get(kind))
            host = host_dir(final or url)
            rel = os.path.join(host, name)
            os.makedirs(os.path.join(self.out_dir, host), exist_ok=True)
            path = os.path.join(self.out_dir, rel)
            with open(path, "wb") as f:
                f.write(body)
            self.by_sha[sha] = rel
            self.saved += 1
            self.rows.append(dict(dict.fromkeys(MANIFEST_FIELDS, ""), status="saved", file=rel, **base))
        self.log("saved %s (%d bytes)" % (rel, len(body)))

    def run(self, urls):
        if self.concurrency <= 1 or len(urls) <= 1:
            for url in urls:
                self.process_one(url)
        else:
            with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                futures = [pool.submit(self.process_one, url) for url in urls]
                for fut in as_completed(futures):
                    # surface unexpected bugs
                    fut.result()
        return self.rows


def write_manifest(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})


def summarize(rows):
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "total": len(rows),
        "saved": counts.get("saved", 0),
        "by_status": counts,
        "bytes_saved": sum(int(r["bytes"] or 0) for r in rows if r["status"] == "saved"),
    }


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="bulk_image_downloader",
        description="Bulk download images from a URL list (text file or CSV). Standard library only.",
    )
    p.add_argument("-i", "--input", required=True, help="Text file (one URL per line) or CSV with a url column")
    p.add_argument("-o", "--output", default="images", help="Output directory (default: images)")
    p.add_argument("--concurrency", type=int, default=4, help="Parallel downloads (default: 4)")
    p.add_argument("--delay", type=float, default=0.25, help="Minimum seconds between requests to one host")
    p.add_argument("--retries", type=int, default=3, help="Retries on HTTP 429 and 5xx (default: 3)")
    p.add_argument("--timeout", type=float, default=30, help="Per-request timeout in seconds")
    p.add_argument("--min-kb", type=float, default=0, help="Skip files smaller than this many KB")
    p.add_argument("--max-mb", type=float, default=10, help="Skip files larger than this many MB (default: 10)")
    p.add_argument("-q", "--quiet", action="store_true", help="Hide progress lines on stderr")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = p.parse_args(argv)

    urls = load_urls(args.input)
    if not urls:
        sys.stderr.write("No URLs found in %s\n" % args.input)
        return 2

    os.makedirs(args.output, exist_ok=True)
    log = (lambda msg: None) if args.quiet else (lambda msg: sys.stderr.write(msg + "\n"))
    fetcher = Fetcher(delay=args.delay, timeout=args.timeout, retries=args.retries)
    dl = Downloader(
        args.output,
        fetcher,
        min_bytes=int(args.min_kb * 1024),
        max_bytes=int(args.max_mb * 1024 * 1024),
        concurrency=args.concurrency,
        log=log,
    )
    rows = dl.run(urls)
    # Keep manifest order roughly aligned with input order
    order = {u: i for i, u in enumerate(urls)}
    rows.sort(key=lambda r: order.get(r.get("url"), 10**9))
    manifest_path = os.path.join(args.output, "manifest.csv")
    write_manifest(manifest_path, rows)
    summary = summarize(rows)
    summary["manifest"] = manifest_path
    summary["output"] = args.output
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    if summary["saved"] == 0 and any(r["status"] == "error" for r in rows):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
