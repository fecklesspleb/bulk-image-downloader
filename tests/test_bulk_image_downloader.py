#!/usr/bin/env python3
"""Offline tests for bulk_image_downloader, using a local ThreadingHTTPServer."""

import csv
import hashlib
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import bulk_image_downloader as bid  # noqa: E402


def png(width, height, pad=0):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    out += chunk(b"IDAT", zlib.compress(raw))
    if pad:
        out += chunk(b"tEXt", b"pad\x00" + b"x" * pad)
    return out + chunk(b"IEND", b"")


def gif(width, height):
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00" + b"\x3b"


def jpeg(width, height):
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


LOGO = png(120, 40, pad=3000)
PIXEL = gif(1, 1)
PHOTO = jpeg(640, 480)


class Site:
    """In-memory site map: path -> (status, content_type, body_or_callable, headers)."""

    def __init__(self):
        self.routes = {}
        self.hits = {}
        self.lock = threading.Lock()

    def add(self, path, status=200, ctype="image/png", body=b"", headers=None):
        self.routes[path] = (status, ctype, body, headers or {})

    def hit(self, path):
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1


SITE = Site()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        SITE.hit(path)
        if path not in SITE.routes:
            body = b"missing"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        status, ctype, body, headers = SITE.routes[path]
        if callable(body):
            body = body()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if status != 204:
            self.wfile.write(body)


def start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SITE.routes.clear()
        SITE.hits.clear()
        SITE.add("/logo.png", 200, "image/png", LOGO)
        SITE.add("/logo-copy.png", 200, "image/png", LOGO)  # same bytes, different URL
        SITE.add("/photo.jpg", 200, "image/jpeg", PHOTO)
        SITE.add("/pixel.gif", 200, "image/gif", PIXEL)
        SITE.add("/broken.jpg", 200, "text/html", b"<html><body>not an image</body></html>")
        SITE.add("/octet.png", 200, "application/octet-stream", png(32, 32))
        SITE.add("/big.png", 200, "image/png", png(10, 10, pad=50_000))
        SITE.add("/slow-ok.png", 200, "image/png", png(8, 8))
        SITE.add("/missing.png", 404, "text/plain", b"nope")

        # flaky 503 then 200
        state = {"n": 0}
        lock = threading.Lock()

        def flaky():
            with lock:
                state["n"] += 1
                n = state["n"]
            if n < 3:
                return b"try again"
            return png(16, 16)

        # We need status to change too , use a custom wrapper via mutable route
        cls._flaky_state = state
        SITE.add("/flaky.png", 503, "text/plain", b"try again")

        cls.httpd = start_server()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        SITE.hits.clear()
        # reset flaky to 503
        SITE.add("/flaky.png", 503, "text/plain", b"try again")
        self._tmpdir = tempfile.TemporaryDirectory()
        self.out = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def url(self, path):
        return self.base + path

    def write_list(self, name, lines):
        path = os.path.join(self.out, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def run_dl(self, urls, concurrency=2, min_bytes=0, max_bytes=10 * 1024 * 1024, retries=3, delay=0):
        fetcher = bid.Fetcher(delay=delay, timeout=5, retries=retries, sleep=lambda _s: None)
        # For flaky retries we still need real sleep=0; Fetcher.sleep(0) is fine
        fetcher.sleep = lambda s: None
        dl = bid.Downloader(
            self.out,
            fetcher,
            min_bytes=min_bytes,
            max_bytes=max_bytes,
            concurrency=concurrency,
            log=lambda m: None,
        )
        return dl.run(urls)


class TestSniffAndSize(Base):
    def test_sniff_png_jpeg_gif(self):
        self.assertEqual(bid.sniff_type(LOGO), "png")
        self.assertEqual(bid.sniff_type(PHOTO), "jpeg")
        self.assertEqual(bid.sniff_type(PIXEL), "gif")
        self.assertIsNone(bid.sniff_type(b"<html>"))

    def test_sizes(self):
        self.assertEqual(bid.image_size(LOGO), (120, 40))
        self.assertEqual(bid.image_size(PHOTO), (640, 480))
        self.assertEqual(bid.image_size(PIXEL), (1, 1))

    def test_svg_pixel_and_non_pixel(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="32"></svg>'
        self.assertEqual(bid.sniff_type(svg), "svg")
        self.assertEqual(bid.image_size(svg), (64, 32))
        svg_ex = b'<svg xmlns="http://www.w3.org/2000/svg" width="34ex" height="10ex" viewBox="0 0 5664 100"></svg>'
        self.assertEqual(bid.image_size(svg_ex), (None, None))


class TestLoadUrls(Base):
    def test_text_file_comments_and_dupes(self):
        path = self.write_list(
            "urls.txt",
            ["# comment", self.url("/logo.png"), "", self.url("/logo.png"), self.url("/photo.jpg")],
        )
        urls = bid.load_urls(path)
        self.assertEqual(urls, [self.url("/logo.png"), self.url("/photo.jpg")])

    def test_csv_with_url_column(self):
        path = os.path.join(self.out, "urls.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "url", "note"])
            w.writerow(["1", self.url("/logo.png"), "logo"])
            w.writerow(["2", self.url("/photo.jpg"), "photo"])
        urls = bid.load_urls(path)
        self.assertEqual(urls, [self.url("/logo.png"), self.url("/photo.jpg")])

    def test_csv_first_column_fallback(self):
        path = os.path.join(self.out, "plain.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["link", "meta"])
            w.writerow([self.url("/logo.png"), "a"])
        urls = bid.load_urls(path)
        self.assertEqual(urls, [self.url("/logo.png")])


class TestDownload(Base):
    def test_saves_and_manifest_fields(self):
        rows = self.run_dl([self.url("/logo.png"), self.url("/photo.jpg")])
        statuses = {r["url"]: r["status"] for r in rows}
        self.assertEqual(statuses[self.url("/logo.png")], "saved")
        self.assertEqual(statuses[self.url("/photo.jpg")], "saved")
        saved = [r for r in rows if r["status"] == "saved"]
        for r in saved:
            self.assertTrue(os.path.isfile(os.path.join(self.out, r["file"])))
            self.assertEqual(len(r["sha256"]), 64)
            self.assertTrue(r["file"].endswith(r["sha256"][:10] + os.path.splitext(r["file"])[1]))

    def test_duplicate_by_hash(self):
        rows = self.run_dl([self.url("/logo.png"), self.url("/logo-copy.png")])
        by = {r["url"]: r for r in rows}
        self.assertEqual(by[self.url("/logo.png")]["status"], "saved")
        self.assertEqual(by[self.url("/logo-copy.png")]["status"], "duplicate")
        self.assertEqual(by[self.url("/logo-copy.png")]["file"], by[self.url("/logo.png")]["file"])
        # only one file on disk under host dir
        host = bid.host_dir(self.url("/logo.png"))
        files = [f for f in os.listdir(os.path.join(self.out, host)) if f.endswith(".png")]
        self.assertEqual(len(files), 1)

    def test_not_an_image_html(self):
        rows = self.run_dl([self.url("/broken.jpg")])
        self.assertEqual(rows[0]["status"], "not_an_image")
        self.assertEqual(os.listdir(self.out), [])  # nothing saved (no host dirs)

    def test_sniff_beats_wrong_content_type(self):
        rows = self.run_dl([self.url("/octet.png")])
        self.assertEqual(rows[0]["status"], "saved")
        self.assertTrue(rows[0]["file"].endswith(".png"))

    def test_too_small_filter(self):
        rows = self.run_dl([self.url("/pixel.gif")], min_bytes=100)
        self.assertEqual(rows[0]["status"], "too_small")

    def test_too_large_filter(self):
        rows = self.run_dl([self.url("/big.png")], max_bytes=1000)
        self.assertEqual(rows[0]["status"], "too_large")

    def test_http_404_not_retried(self):
        rows = self.run_dl([self.url("/missing.png")], retries=3)
        self.assertEqual(rows[0]["status"], "error")
        self.assertIn("404", rows[0]["error"])
        self.assertEqual(SITE.hits.get("/missing.png"), 1)

    def test_retries_on_503(self):
        # custom handler path: mutate status after 2 failures using a callable body won't change status.
        # Instead, install a route object that tracks attempts via a wrapper Handler isn't flexible enough;
        # use a mutable list in a custom route by replacing do_GET for this path via SITE routes with callable status.
        attempts = {"n": 0}
        lock = threading.Lock()

        class FlakyHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                with lock:
                    attempts["n"] += 1
                    n = attempts["n"]
                if n < 3:
                    body = b"no"
                    self.send_response(503)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                body = png(16, 16)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            url = "http://127.0.0.1:%d/flaky.png" % httpd.server_address[1]
            sleeps = []
            fetcher = bid.Fetcher(delay=0, timeout=5, retries=3, sleep=lambda s: sleeps.append(s))
            dl = bid.Downloader(self.out, fetcher, concurrency=1, log=lambda m: None)
            rows = dl.run([url])
            self.assertEqual(rows[0]["status"], "saved")
            self.assertEqual(attempts["n"], 3)
            self.assertTrue(sleeps)  # backed off between retries
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_invalid_url(self):
        rows = self.run_dl(["not-a-url", "http://[oops"])
        self.assertTrue(all(r["status"] == "error" for r in rows))

    def test_safe_name_sanitizes(self):
        name = bid.safe_name("https://ex.com/a/b/weird name!!!.PNG", "abcdef0123456789", ".png")
        self.assertEqual(name, "weird-name-abcdef0123.png")
        self.assertNotIn("!", name)
        self.assertNotIn(" ", name)

    def test_concurrency_limit_runs(self):
        urls = [self.url("/logo.png"), self.url("/photo.jpg"), self.url("/octet.png")]
        rows = self.run_dl(urls, concurrency=3)
        self.assertEqual(sum(1 for r in rows if r["status"] == "saved"), 3)

    def test_cli_writes_manifest(self):
        path = self.write_list("in.txt", [self.url("/logo.png"), self.url("/broken.jpg")])
        out_dir = os.path.join(self.out, "cli-out")
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            code = bid.main(["-i", path, "-o", out_dir, "--concurrency", "2", "--delay", "0", "-q"])
        finally:
            sys.stdout = old
        self.assertEqual(code, 0)
        self.assertIn('"saved": 1', buf.getvalue())
        manifest = os.path.join(out_dir, "manifest.csv")
        self.assertTrue(os.path.isfile(manifest))
        with open(manifest, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        statuses = sorted(r["status"] for r in rows)
        self.assertEqual(statuses, ["not_an_image", "saved"])

    def test_cli_exit_1_when_all_error(self):
        path = self.write_list("bad.txt", [self.url("/missing.png")])
        out_dir = os.path.join(self.out, "cli-bad")
        # capture stdout
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            code = bid.main(["-i", path, "-o", out_dir, "--delay", "0", "-q"])
        finally:
            sys.stdout = old
        self.assertEqual(code, 1)
        summary = json.loads(buf.getvalue())
        self.assertEqual(summary["saved"], 0)



    def test_host_dir_rejects_dotdot(self):
        self.assertEqual(bid.host_dir("http://../a.png"), "files")
        self.assertEqual(bid.host_dir("http://./b.png"), "files")
        self.assertEqual(bid.host_dir("http://.../c.png"), "files")
        # normal hosts still work
        self.assertEqual(bid.host_dir("https://Example.COM/x.png"), "example.com")

    def test_host_dir_keeps_saved_path_inside_out(self):
        """Even if host_dir were wrong historically, saved files must stay under out_dir."""
        # Construct a row path the way Downloader does and assert resolve stays inside out.
        host = bid.host_dir("http://../a.png")
        name = bid.safe_name("http://../a.png", "abcdef0123456789ffff", ".png")
        rel = os.path.join(host, name)
        out = os.path.realpath(self.out)
        full = os.path.realpath(os.path.join(self.out, rel))
        self.assertTrue(full.startswith(out + os.sep), full)
        self.assertNotIn("..", rel.split(os.sep))


class TestHostSpacing(Base):
    def test_per_host_delay_under_concurrency(self):
        clocks = {"t": 0.0}
        sleeps = []

        def clock():
            return clocks["t"]

        def sleep(s):
            sleeps.append(s)
            clocks["t"] += s

        fetcher = bid.Fetcher(delay=0.5, timeout=5, retries=0, sleep=sleep, clock=clock)
        # Two sequential waits on same host should insert a delay on the second
        url = self.url("/logo.png")
        fetcher._wait_for_host(url)
        clocks["t"] += 0.1  # only 0.1s later
        fetcher._wait_for_host(url)
        self.assertTrue(any(s > 0 for s in sleeps))


if __name__ == "__main__":
    unittest.main()
