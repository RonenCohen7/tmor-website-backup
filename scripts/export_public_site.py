#!/usr/bin/env python3
"""Download the public T.MORE site into backup/ for the later React rebuild.

Reads the WordPress REST dump in backup/wp-json, then saves full HTML,
the media library, stylesheets that define the look, and a link inventory.
Safe to re-run: existing files are skipped.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "backup"
WP = BACKUP / "wp-json"
HTML_DIR = BACKUP / "html"
MEDIA_DIR = BACKUP / "media"
ASSET_DIR = BACKUP / "assets"
SITEMAP_DIR = BACKUP / "sitemaps"

ORIGIN = "https://www.t-more.co.il"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 tmore-backup"
WORKERS = 8

CONTENT_FILES = {
    "pages": "page",
    "posts": "post",
    "courses": "course",
    "jobs": "job",
    "pdf": "pdf",
    "products": "product",
    "services": "service",
    "workshops": "workshop",
}


def ascii_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/%")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&%") if parts.query else ""
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, ""))


def fetch_bytes(url: str, timeout: int = 90) -> tuple[int, bytes, str]:
    req = urllib.request.Request(ascii_url(url), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            data = response.read()
            final = response.geturl()
            return response.status, data, final
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp else b""
        return exc.code, body, url


def strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def safe_slug(slug: str, fallback: str) -> str:
    cleaned = (slug or fallback).strip("/") or fallback
    cleaned = cleaned.replace("/", "-")
    return cleaned[:180]


def load_items() -> list[dict]:
    items = []
    for filename, kind in CONTENT_FILES.items():
        path = WP / f"{filename}.json"
        for item in json.loads(path.read_text(encoding="utf-8")):
            title = strip_tags((item.get("title") or {}).get("rendered") or "")
            slug = safe_slug(item.get("slug") or "", f"{kind}-{item.get('id')}")
            if kind == "page" and item.get("link", "").rstrip("/") == ORIGIN:
                slug = "home"
            items.append(
                {
                    "id": item.get("id"),
                    "type": item.get("type") or kind,
                    "kind": kind,
                    "status": item.get("status"),
                    "slug": slug,
                    "title": title,
                    "link": item.get("link"),
                    "date": item.get("date"),
                    "modified": item.get("modified"),
                    "parent": item.get("parent") or 0,
                    "excerpt": strip_tags((item.get("excerpt") or {}).get("rendered") or ""),
                    "yoast_title": (item.get("yoast_head_json") or {}).get("title"),
                    "yoast_description": (item.get("yoast_head_json") or {}).get("description"),
                    "featured_media": item.get("featured_media") or 0,
                }
            )
    for filename, kind in (("categories", "category"), ("tags", "tag")):
        for item in json.loads((WP / f"{filename}.json").read_text(encoding="utf-8")):
            items.append(
                {
                    "id": item.get("id"),
                    "type": kind,
                    "kind": kind,
                    "status": "archive",
                    "slug": safe_slug(item.get("slug") or "", f"{kind}-{item.get('id')}"),
                    "title": strip_tags(item.get("name") or ""),
                    "link": item.get("link"),
                    "date": None,
                    "modified": None,
                    "parent": item.get("parent") or 0,
                    "excerpt": strip_tags(item.get("description") or ""),
                    "yoast_title": None,
                    "yoast_description": None,
                    "featured_media": 0,
                }
            )
    # Footer links that are not real content pages. Kept so the link map shows them.
    for href, title in (
        (f"{ORIGIN}/%d7%a1%d7%93%d7%a0%d7%90%d7%95%d7%aa-%d7%95%d7%94%d7%a8%d7%a6%d7%90%d7%95%d7%aa/", "סדנאות והרצאות (קישור פוטר)"),
        (f"{ORIGIN}/%d7%9e%d7%90%d7%9e%d7%a8%d7%99%d7%9d/", "מאמרים (קישור פוטר)"),
        (
            f"{ORIGIN}/services/%d7%a4%d7%99%d7%aa%d7%95%d7%97-%d7%a4%d7%aa%d7%a8%d7%95%d7%a0%d7%95%d7%aa-%d7%9c%d7%9e%d7%99%d7%93%d7%94-%d7%93%d7%99%d7%92%d7%99%d7%98%d7%9c%d7%99%d7%99%d7%9d/",
            "פיתוח פתרונות למידה דיגיטליים (קישור פוטר)",
        ),
    ):
        items.append(
            {
                "id": None,
                "type": "footer-link",
                "kind": "footer-link",
                "status": "unknown",
                "slug": safe_slug(urllib.parse.unquote(href.rstrip("/").split("/")[-1]), "footer"),
                "title": title,
                "link": href,
                "date": None,
                "modified": None,
                "parent": 0,
                "excerpt": "",
                "yoast_title": None,
                "yoast_description": None,
                "featured_media": 0,
            }
        )
    return items


def download_one(item: dict) -> dict:
    url = item["link"]
    dest_dir = HTML_DIR / item["kind"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{item['slug']}.html"
    if dest.exists() and dest.stat().st_size > 0:
        data = dest.read_bytes()
        status, final = 200, url
    else:
        status, data, final = fetch_bytes(url)
    record = {
        "url": url,
        "final_url": final,
        "status": status,
        "bytes": len(data),
        "file": str(dest.relative_to(ROOT)) if status == 200 and data else None,
    }
    if status == 200 and data:
        dest.write_bytes(data)
    item_out = dict(item)
    item_out.update(record)
    if status == 200:
        text = data.decode("utf-8", "replace")
        item_out["internal_links"] = extract_internal_links(text)
        item_out["forms"] = extract_forms(text)
        item_out["asset_urls"] = extract_asset_urls(text)
    else:
        item_out["internal_links"] = []
        item_out["forms"] = []
        item_out["asset_urls"] = []
    return item_out


def extract_internal_links(page: str) -> list[str]:
    hrefs = re.findall(r"""href=["']([^"'#]+)""", page, flags=re.I)
    found = []
    seen = set()
    for href in hrefs:
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        absolute = urllib.parse.urljoin(ORIGIN + "/", href)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.netloc not in ("www.t-more.co.il", "t-more.co.il"):
            continue
        clean = urllib.parse.urlunparse(parsed._replace(fragment=""))
        if clean not in seen:
            seen.add(clean)
            found.append(clean)
    return found


def extract_forms(page: str) -> list[dict]:
    forms = []
    for match in re.finditer(r"<form\b([^>]*)>([\s\S]*?)</form>", page, flags=re.I):
        attrs, body = match.group(1), match.group(2)
        name = re.search(r'name="([^"]*)"', attrs)
        fields = []
        for field in re.finditer(
            r"<(input|textarea|select)\b([^>]*)>(?:([\s\S]*?)</\1>)?",
            body,
            flags=re.I,
        ):
            tag, fattrs = field.group(1), field.group(2)
            field_name = re.search(r'name="([^"]*)"', fattrs)
            field_type = re.search(r'type="([^"]*)"', fattrs)
            placeholder = re.search(r'placeholder="([^"]*)"', fattrs)
            label = None
            if field_name:
                ident = re.search(r'id="([^"]*)"', fattrs)
                if ident:
                    lab = re.search(
                        rf'<label[^>]*for="{re.escape(ident.group(1))}"[^>]*>([\s\S]*?)</label>',
                        body,
                    )
                    if lab:
                        label = strip_tags(lab.group(1))
            fields.append(
                {
                    "tag": tag.lower(),
                    "type": field_type.group(1) if field_type else tag.lower(),
                    "name": html_lib.unescape(field_name.group(1)) if field_name else None,
                    "label": label,
                    "placeholder": html_lib.unescape(placeholder.group(1)) if placeholder else None,
                    "required": "required" in fattrs,
                }
            )
        forms.append(
            {
                "name": html_lib.unescape(name.group(1)) if name else None,
                "fields": fields,
            }
        )
    return forms


def extract_asset_urls(page: str) -> list[str]:
    urls = set(re.findall(r"""https://www\.t-more\.co\.il/wp-content/[^"'\\\s>]+""", page))
    cleaned = []
    for url in urls:
        url = html_lib.unescape(url).split("?")[0]
        if any(url.endswith(ext) for ext in (".css", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".mp4", ".pdf", ".woff", ".woff2")):
            cleaned.append(url)
    return sorted(cleaned)


def save_bytes(url: str, dest: Path) -> tuple[str, int, int]:
    if dest.exists() and dest.stat().st_size > 0:
        return url, 200, dest.stat().st_size
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        status, data, _final = fetch_bytes(url, timeout=120)
    except Exception as exc:
        print(f"  fail {url} {exc}", flush=True)
        return url, 0, 0
    if status == 200 and data:
        dest.write_bytes(data)
        return url, status, len(data)
    return url, status, 0


def media_jobs() -> list[tuple[str, Path]]:
    media = json.loads((WP / "media.json").read_text(encoding="utf-8"))
    jobs = []
    seen = set()
    for item in media:
        url = (item.get("source_url") or "").split("?")[0]
        if not url or url in seen:
            continue
        seen.add(url)
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc not in ("www.t-more.co.il", "t-more.co.il"):
            continue
        rel = urllib.parse.unquote(parsed.path.lstrip("/"))
        jobs.append((url, MEDIA_DIR / rel))
    return jobs


def asset_jobs(pages: list[dict]) -> list[tuple[str, Path]]:
    jobs = []
    seen = set()
    for page in pages:
        for url in page.get("asset_urls") or []:
            if url in seen:
                continue
            seen.add(url)
            parsed = urllib.parse.urlparse(url)
            rel = urllib.parse.unquote(parsed.path.lstrip("/"))
            # Theme and generated Elementor CSS live with the media tree.
            jobs.append((url, MEDIA_DIR / rel))
    theme_css = [
        f"{ORIGIN}/wp-content/themes/hello-elementor/assets/css/reset.css",
        f"{ORIGIN}/wp-content/themes/hello-elementor/assets/css/theme.css",
        f"{ORIGIN}/wp-content/themes/hello-elementor/assets/css/header-footer.css",
        f"{ORIGIN}/wp-content/uploads/elementor/css/post-7.css",
        f"{ORIGIN}/wp-content/uploads/elementor/css/post-9.css",
        f"{ORIGIN}/wp-content/uploads/elementor/css/post-71.css",
        f"{ORIGIN}/wp-content/uploads/elementor/css/post-1329.css",
        f"{ORIGIN}/wp-content/uploads/elementor/css/custom-frontend.min.css",
    ]
    for url in theme_css:
        parsed = urllib.parse.urlparse(url)
        rel = urllib.parse.unquote(parsed.path.lstrip("/"))
        jobs.append((url, MEDIA_DIR / rel))
    return jobs


def download_sitemaps() -> None:
    SITEMAP_DIR.mkdir(parents=True, exist_ok=True)
    index_url = f"{ORIGIN}/sitemap_index.xml"
    status, data, _final = fetch_bytes(index_url)
    if status != 200:
        print("sitemap index", status)
        return
    (SITEMAP_DIR / "sitemap_index.xml").write_bytes(data)
    locs = re.findall(r"<loc>(.*?)</loc>", data.decode("utf-8", "replace"))
    for loc in locs:
        name = loc.rstrip("/").split("/")[-1]
        status, body, _final = fetch_bytes(loc)
        print("sitemap", name, status, len(body))
        if status == 200:
            (SITEMAP_DIR / name).write_bytes(body)


def run_pool(jobs: list[tuple[str, Path]], label: str) -> dict:
    ok = 0
    failed = []
    total_bytes = 0
    print(f"{label}: {len(jobs)} files", flush=True)
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(save_bytes, url, dest) for url, dest in jobs]
        for index, future in enumerate(as_completed(futures), start=1):
            url, status, size = future.result()
            total_bytes += size
            if status == 200 and size:
                ok += 1
            else:
                failed.append({"url": url, "status": status})
            if index % 50 == 0 or index == len(jobs):
                print(f"  {label} {index}/{len(jobs)} ok={ok} failed={len(failed)}", flush=True)
    return {"ok": ok, "failed": failed, "bytes": total_bytes}


def design_notes() -> dict:
    css_files = list((MEDIA_DIR / "wp-content/uploads/elementor/css").glob("*.css")) if (MEDIA_DIR / "wp-content/uploads/elementor/css").exists() else []
    colors = Counter()
    fonts = Counter()
    for path in css_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for color in re.findall(r"#[0-9A-Fa-f]{3,8}", text):
            colors[color.lower()] += 1
        for font in re.findall(r"font-family:([^;}{]+)", text):
            fonts[font.strip().strip('"').strip("'")] += 1
    return {
        "fonts": [{"family": name, "count": count} for name, count in fonts.most_common(12)],
        "colors": [{"color": name, "count": count} for name, count in colors.most_common(20)],
    }


def main() -> None:
    started = time.time()
    download_sitemaps()
    items = load_items()
    print(f"pages to fetch: {len(items)}", flush=True)
    pages = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(download_one, item) for item in items]
        for index, future in enumerate(as_completed(futures), start=1):
            pages.append(future.result())
            if index % 20 == 0 or index == len(items):
                print(f"  html {index}/{len(items)}", flush=True)

    pages.sort(key=lambda item: (item["kind"], item["title"]))
    media_report = run_pool(media_jobs(), "media")
    asset_report = run_pool(asset_jobs(pages), "assets")

    link_counter = Counter()
    external = Counter()
    forms = []
    for page in pages:
        for link in page.get("internal_links") or []:
            link_counter[link] += 1
        for form in page.get("forms") or []:
            forms.append({"page": page["link"], "title": page["title"], "form": form})

    inventory = []
    for page in pages:
        inventory.append({key: page[key] for key in page if key not in ("internal_links", "asset_urls", "forms")})

    (BACKUP / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
    (BACKUP / "internal-links.json").write_text(
        json.dumps(
            [{"url": url, "count": count} for url, count in link_counter.most_common()],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (BACKUP / "forms.json").write_text(json.dumps(forms, ensure_ascii=False, indent=2), encoding="utf-8")
    (BACKUP / "design.json").write_text(json.dumps(design_notes(), ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "pages": len(pages),
        "html_ok": sum(1 for page in pages if page["status"] == 200),
        "html_failed": [{"url": page["url"], "status": page["status"]} for page in pages if page["status"] != 200],
        "media": {"ok": media_report["ok"], "failed": len(media_report["failed"]), "bytes": media_report["bytes"]},
        "assets": {"ok": asset_report["ok"], "failed": len(asset_report["failed"]), "bytes": asset_report["bytes"]},
        "media_failures": media_report["failed"][:40],
        "asset_failures": asset_report["failed"][:40],
        "seconds": round(time.time() - started, 1),
    }
    (BACKUP / "download-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("pages", "html_ok", "media", "assets", "seconds")}, ensure_ascii=False, indent=2))
    print("html failed", len(report["html_failed"]))


if __name__ == "__main__":
    main()
