# bulk-image-downloader

**Bulk download images from a URL list with Python**, from a text file or a CSV. One file, standard library only, no browser and no API keys.

You already have a list or a CSV of image URLs (an export, a scrape, a spreadsheet). This tool reads them, downloads each image with retries and a concurrency limit, checks that the bytes really are an image (so a `photo.jpg` that comes back as an HTML error page is not saved), skips duplicates by SHA-256, and writes a `manifest.csv`. It does not crawl web pages for `<img>` tags.

Search-friendly phrases this README targets: *bulk download images from URL list python*, *download images from csv*, *python download images from list of urls*, *batch image downloader python*.

- Python 3.9 or newer, standard library only.
- Retries HTTP 429 and 5xx (honours `Retry-After`), skips 404s without retrying, spaces requests to the same host, and caps parallel downloads.
- MIT licensed.

## Quick start

```bash
# one URL per line
python3 bulk_image_downloader.py -i urls.txt -o images

# CSV with a url column (or image_url / link / src); otherwise the first column
python3 bulk_image_downloader.py -i urls.csv -o images --concurrency 8

# skip tiny files
python3 bulk_image_downloader.py -i urls.txt --min-kb 2
```

Files go to `images/<host>/<name>-<first 10 hex of sha256>.<ext>`, so two different files called `logo.png` never overwrite each other. The extension comes from the file's bytes, not the URL. A JSON summary is printed at the end; progress lines go to stderr (`-q` hides them).

## Input formats

**Text file**: one URL per line. Blank lines and `#` comments are ignored. Duplicate URLs in the file are kept once.

**CSV**: if the header has a `url`, `image_url`, `link` or `src` column, that column is used; otherwise the first column is used. A UTF-8 BOM is fine.

```csv
id,url,note
1,https://upload.wikimedia.org/wikipedia/commons/c/c3/Python-logo-notext.svg,logo
2,https://www.python.org/static/img/python-logo.png,png
```

## Checks on every file

- **Is it really an image?** The first bytes are checked for PNG, JPEG, GIF, WebP, AVIF, BMP, ICO and SVG. A misleading `Content-Type` (for example `application/octet-stream`) is fine when the bytes sniff as an image; an HTML error page is recorded as `not_an_image` and not saved.
- **Width and height** are read from the file header, without decoding the pixels.
- **Duplicates**: a file whose SHA-256 was already saved in this run is recorded as `duplicate` and points to the first copy.
- **Filters**: `--min-kb` and `--max-mb` (default 10).

## manifest.csv

| Column | Meaning |
|---|---|
| `url`, `final_url` | The URL you gave, and where it ended up after redirects |
| `status` | `saved`, `duplicate`, `too_small`, `too_large`, `not_an_image` or `error` |
| `file` | Path inside the output folder (for `duplicate`, the copy already saved) |
| `content_type`, `bytes`, `width`, `height`, `sha256` | What came back |
| `error` | For `error` rows, for example `HTTP 404` |

The exit code is 1 only when nothing was saved and at least one request failed. Exit code 2 means the input file contained no URLs.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `-i` / `--input` | (required) | Text file or CSV of URLs |
| `-o` / `--output` | `images` | Output directory |
| `--concurrency` | `4` | Parallel downloads |
| `--delay` | `0.25` | Minimum seconds between requests to one host |
| `--retries` | `3` | Retries on HTTP 429 and 5xx |
| `--timeout` | `30` | Per-request timeout (seconds) |
| `--min-kb` | `0` | Skip files smaller than this |
| `--max-mb` | `10` | Skip files larger than this |
| `-q` / `--quiet` | off | Hide progress on stderr |
| `--version` | | Print the version and exit |

## A real run

On November 5, 2026 (09:40 ET), against 8 Wikimedia Commons / free-media URLs (Special:FilePath thumbnails plus the Python logo SVG and the python.org PNG):

| Input URLs | Saved | Errors | Bytes |
|---|---|---|---|
| 8 | 8 | 0 | 164,886 |

Files landed under `images/<host>/` with a `manifest.csv` row for each URL (status, final URL after redirects, content type, size, width, height, SHA-256). Evidence for that run lives outside the published repo folder.

## Limits

- Direct image URLs only (text file or CSV). It does not crawl a web page for `<img>` tags. If you want to grab every image from a web page (srcset, lazy-load, og:image), use [page-image-downloader](https://github.com/fecklesspleb/page-image-downloader) instead. That tool also accepts direct image URLs, but this one is the list/CSV-only path with CSV column detection and parallel downloads.
- No JavaScript, no login, cookies or proxies.
- It is a local command-line tool, so it fetches whatever addresses you give it, including `localhost` and private network addresses. Don't run it as a public service on untrusted input.
- Malformed URLs get an `error` row and are not saved.
- Some image CDNs (including certain Wikimedia Commons `/thumb/.../NNpx-...` paths) reject exact pixel sizes that are not in their cache. Prefer the original file URL, or Commons `Special:FilePath/Name?width=...`, which redirects to a valid size.

## Source

https://github.com/fecklesspleb/bulk-image-downloader

## Tests

```bash
python3 -m unittest discover -s tests -v
```

22 tests, all offline. Last run (Nov 5, 2026, ET): 22 passed on Python 3.13 on Linux. They run against a local `ThreadingHTTPServer` on `127.0.0.1` with PNG, JPEG and GIF bytes built in the test file. They cover text and CSV input, image sniffing and sizes, HTML served under an image name, duplicates by hash, size filters, 404 not retried, 503 retries with backoff, safe filenames, concurrency, per-host delay, host-dir escape (`http://../a.png`), and the CLI's manifest and exit code.

## Be a good guest

Download images you have the right to use: your own site, openly licensed media, press kits, or pages whose terms allow it. Being able to download an image doesn't mean you may republish it. Keep a sensible `--delay` and `--concurrency` against hosts you don't run.

## License and disclaimers

MIT. See [LICENSE](LICENSE). This code and README were written with AI assistance. Not affiliated with any of the sites named. Author: fecklesspleb.

Hosted version (bills your own Apify account): the Apify Actor [bulk-image-downloader](https://apify.com/fecklesspleb/bulk-image-downloader).
