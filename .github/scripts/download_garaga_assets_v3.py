from __future__ import annotations

import csv
import hashlib
import re
import shutil
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.garaga.com/digital-asset-library"
OUT_ROOT = Path("output/Garaga_Digital_Assets_Web")
ZIP_BASE = Path("output/Garaga_Digital_Assets_Web")
GARAGA_HOSTS = {"www.garaga.com", "garaga.com"}
LEGACY_HOST = "cmsgaraga.garaga.com"
ASSET_HOSTS = {LEGACY_HOST, "cmsgaraga.s3.amazonaws.com", "garaga.s3.amazonaws.com", "d29j7agyuz1um3.cloudfront.net"}
ASSET_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf", ".zip", ".eps", ".ai", ".tif", ".tiff", ".doc", ".docx"}
RESIDENTIAL = {"princeton", "eastman", "cambridge", "california", "village-collection", "standard-plus", "acadia-138", "vantage", "regal", "top-tech", "h-tech"}
COMMERCIAL = {"g-1000", "g-2020", "g-4400", "g-5000", "s-24", "ssg", "ssi-24"}


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=.35, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    session.mount("http://", HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12))
    session.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12))
    session.headers.update({
        "User-Agent": "LIFTX-Garaga-Asset-Archive/3.0 (+https://www.liftxdoor.com)",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def normalize_page(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), path, "", "", ""))


def extension(url: str) -> str:
    return Path(unquote(urlparse(url).path)).suffix.lower()


def is_library_page(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() in GARAGA_HOSTS and parsed.path.startswith("/digital-asset-library") and extension(url) not in ASSET_EXTS


def is_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() in ASSET_HOSTS or extension(url) in ASSET_EXTS


def is_special_page(page_url: str, title: str) -> bool:
    key = f"{urlparse(page_url).path} {title}".lower()
    return any(term in key for term in ("logo", "brand guide", "user guide", "partner brand", "garaga corporate", "pro dealer", "liftmaster"))


def page_folder(page_url: str, title: str) -> Path:
    parts = [part for part in urlparse(page_url).path.strip("/").split("/") if part][1:]
    slug = parts[-1] if parts else "root"
    key = f"{'/'.join(parts)} {title}".lower()
    if any(term in key for term in ("logo", "brand", "user-guide")):
        return Path("logos-and-brand-guide") / slug
    if "agricultural" in key:
        return Path("agricultural") / slug
    if "commercial" in key or slug in COMMERCIAL:
        return Path("commercial") / slug
    if slug in RESIDENTIAL:
        return Path("residential") / slug
    if "color" in slug or "panel" in slug:
        return Path("residential") / "reference" / slug
    return Path("misc") / slug


def filename_from_url(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name).strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name or hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def infer_ext(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/svg+xml": ".svg",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
    }.get(content_type.split(";", 1)[0].lower(), "")


def resolved_download_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if parsed.netloc.lower() == LEGACY_HOST and parsed.scheme == "https":
        return urlunparse(("http", parsed.netloc, parsed.path, parsed.params, parsed.query, ""))
    return source_url


def discover_assets() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    session = make_session()
    queue = deque([BASE_URL])
    visited: set[str] = set()
    assets: dict[str, dict[str, str]] = {}
    pages: list[dict[str, str]] = []

    while queue and len(visited) < 80:
        page_url = normalize_page(queue.popleft())
        if page_url in visited or not is_library_page(page_url):
            continue
        visited.add(page_url)

        try:
            response = session.get(page_url, timeout=(10, 25))
            response.raise_for_status()
            if "text/html" not in response.headers.get("content-type", ""):
                continue
        except Exception as exc:
            pages.append({"page_url": page_url, "title": "", "status": f"ERROR: {exc}"})
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else (soup.title.get_text(" ", strip=True) if soup.title else "")
        pages.append({"page_url": page_url, "title": title, "status": "OK"})
        special = is_special_page(page_url, title)

        for anchor in soup.find_all("a", href=True):
            href = (anchor.get("href") or "").strip()
            if not href or href.lower().startswith(("javascript:", "mailto:", "tel:")) or "href=" in href.lower():
                continue
            absolute = urljoin(page_url, href)
            candidate = normalize_page(absolute)
            if is_library_page(candidate) and candidate not in visited:
                queue.append(candidate)

            parsed = urlparse(absolute)
            if parsed.scheme not in {"http", "https"} or not is_asset_url(absolute):
                continue
            text = " ".join(anchor.get_text(" ", strip=True).split())
            parent_text = " ".join(anchor.parent.get_text(" ", strip=True).split()) if anchor.parent else text
            context = f"{absolute} {text} {parent_text}".lower()
            web_asset = any(term in context for term in ("72dpi", "72 dpi", "web file")) and not any(term in context for term in ("300dpi", "300 dpi", "printable file"))
            special_asset = special and parsed.netloc.lower() not in GARAGA_HOSTS
            if web_asset or special_asset:
                assets.setdefault(absolute, {
                    "source_url": absolute,
                    "resolved_url": resolved_download_url(absolute),
                    "source_page": page_url,
                    "page_title": title,
                    "link_text": text,
                })

    print(f"Discovered {len(visited)} library pages and {len(assets)} candidate assets")
    return list(assets.values()), pages


def download_one(item: dict[str, str]) -> dict[str, str | int]:
    source_url = item["source_url"]
    download_url = item["resolved_url"]
    folder = OUT_ROOT / page_folder(item["source_page"], item["page_title"])
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / filename_from_url(source_url)
    if target.exists():
        target = folder / f"{target.stem}-{hashlib.sha1(source_url.encode()).hexdigest()[:8]}{target.suffix}"

    size = 0
    digest = ""
    status = "OK"
    session = make_session()
    try:
        with session.get(download_url, timeout=(10, 40), stream=True, allow_redirects=True) as response:
            response.raise_for_status()
            if not target.suffix:
                inferred = infer_ext(response.headers.get("content-type", ""))
                if inferred:
                    target = target.with_suffix(inferred)
            hasher = hashlib.sha256()
            with target.open("wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if chunk:
                        handle.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
            digest = hasher.hexdigest()
    except Exception as exc:
        status = f"ERROR: {exc}"
        target.unlink(missing_ok=True)

    return {
        **item,
        "relative_path": str(target.relative_to(OUT_ROOT)) if status == "OK" else "",
        "bytes": size,
        "sha256": digest,
        "status": status,
    }


def main() -> None:
    shutil.rmtree(OUT_ROOT, ignore_errors=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    Path("output").mkdir(exist_ok=True)

    assets, pages = discover_assets()
    rows: list[dict[str, str | int]] = []
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(download_one, item) for item in assets]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"[{index}/{len(futures)}] {str(row['status']).split(':', 1)[0]} {row['source_url']}")

    fields = ["source_page", "page_title", "link_text", "source_url", "resolved_url", "relative_path", "bytes", "sha256", "status"]
    with (OUT_ROOT / "manifest.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: str(row["source_url"])))
    with (OUT_ROOT / "source_pages.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["page_url", "title", "status"])
        writer.writeheader()
        writer.writerows(pages)

    successful = sum(1 for row in rows if row["status"] == "OK")
    failed = len(rows) - successful
    total_bytes = sum(int(row["bytes"]) for row in rows)
    (OUT_ROOT / "README.txt").write_text(
        "Garaga Digital Asset Library — Web Archive\n"
        "==========================================\n\n"
        f"Source: {BASE_URL}\n"
        "Prepared for LIFTX website use.\n\n"
        "Included: public 72 dpi web imagery, official logo downloads, brand-guide resources, and source manifests.\n"
        "Excluded: 300 dpi print imagery.\n\n"
        f"Downloaded: {successful}\nFailed: {failed}\nBytes: {total_bytes}\n\n"
        "Use assets in accordance with Garaga's current brand guide and terms.\n",
        encoding="utf-8",
    )

    shutil.make_archive(str(ZIP_BASE), "zip", root_dir=OUT_ROOT.parent, base_dir=OUT_ROOT.name)
    archive = ZIP_BASE.with_suffix(".zip")
    print(f"SUMMARY downloaded={successful} failed={failed} archive_bytes={archive.stat().st_size}")
    if successful == 0:
        raise SystemExit("No assets downloaded")


if __name__ == "__main__":
    main()
