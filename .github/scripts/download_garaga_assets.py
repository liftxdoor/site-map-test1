from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import time
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_URL = "https://www.garaga.com/digital-asset-library"
OUT_ROOT = Path("output/Garaga_Digital_Assets_Web")
ZIP_BASE = Path("output/Garaga_Digital_Assets_Web")

RESIDENTIAL_SLUGS = {
    "princeton",
    "eastman",
    "cambridge",
    "california",
    "village-collection",
    "standard-plus",
    "acadia-138",
    "vantage",
    "regal",
    "top-tech",
    "h-tech",
    "color-chart-steel",
    "color-chart-aluminum",
    "panels",
}
COMMERCIAL_SLUGS = {
    "g-1000",
    "g-2020",
    "g-4400",
    "g-5000",
    "s-24",
    "ssg",
    "ssi-24",
}
ASSET_HOST_HINTS = (
    "cmsgaraga.garaga.com",
    "cmsgaraga.s3.amazonaws.com",
    "garaga.s3.amazonaws.com",
)
ASSET_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf",
    ".zip", ".eps", ".ai", ".tif", ".tiff", ".doc", ".docx",
}


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update({
        "User-Agent": "LIFTX-Garaga-Asset-Archive/1.0 (+https://www.liftxdoor.com)",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def normalize_page_url(url: str) -> str:
    parsed = urlparse(url)
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), path, "", "", ""))


def is_library_page(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower() in {"www.garaga.com", "garaga.com"} and parsed.path.startswith("/digital-asset-library")


def page_folder(page_url: str) -> Path:
    path = urlparse(page_url).path.strip("/")
    parts = path.split("/")[1:]  # remove digital-asset-library
    slug = parts[-1] if parts else "root"

    if "logos" in parts:
        return Path("logos") / slug
    if "other" in parts:
        return Path("other") / slug
    if "agricultural" in parts:
        return Path("agricultural") / slug
    if "commercial" in parts or slug in COMMERCIAL_SLUGS:
        return Path("commercial") / slug
    if slug in RESIDENTIAL_SLUGS:
        if slug.startswith("color-chart") or slug == "panels":
            return Path("residential") / "reference" / slug
        return Path("residential") / slug
    return Path("misc") / slug


def clean_filename(url: str, fallback_seed: str) -> str:
    parsed = urlparse(url)
    name = unquote(Path(parsed.path).name).strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = hashlib.sha1(fallback_seed.encode("utf-8")).hexdigest()[:16]
    return name


def unique_path(folder: Path, filename: str, url: str) -> Path:
    target = folder / filename
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return folder / f"{stem}-{digest}{suffix}"


def is_asset_url(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    ext = Path(unquote(parsed.path)).suffix.lower()
    return host in ASSET_HOST_HINTS or ext in ASSET_EXTENSIONS


def classify_link(page_url: str, anchor) -> tuple[bool, str]:
    href = (anchor.get("href") or "").strip()
    if not href:
        return False, ""

    absolute = urljoin(page_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in {"http", "https"}:
        return False, ""

    path = urlparse(page_url).path.lower()
    anchor_text = " ".join(anchor.get_text(" ", strip=True).split()).lower()
    context = " ".join(anchor.parent.get_text(" ", strip=True).split()).lower() if anchor.parent else anchor_text
    href_lower = absolute.lower()

    # Collection imagery: web-ready 72 dpi only.
    is_72 = "72 dpi" in context or "72dpi" in context or "web file" in context or "72dpi" in href_lower
    is_300 = "300 dpi" in context or "300dpi" in context or "printable file" in context or "300dpi" in href_lower
    if is_72 and not is_300:
        return True, absolute

    # Logos and brand-guide sections: retain all official downloadable formats.
    special_page = "/logos" in path or "/other" in path
    if special_page and is_asset_url(absolute):
        return True, absolute

    # Some special resources use descriptive anchor text instead of "Download".
    special_text = any(term in anchor_text for term in ("download", "logo", "brand guide", "user guide", "printing"))
    if special_page and special_text and urlparse(absolute).netloc.lower() not in {"www.garaga.com", "garaga.com"}:
        return True, absolute

    return False, ""


def infer_extension(response: requests.Response) -> str:
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/svg+xml": ".svg",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
        "application/zip": ".zip",
    }.get(content_type, "")


def main() -> None:
    if OUT_ROOT.exists():
        shutil.rmtree(OUT_ROOT)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    Path("output").mkdir(exist_ok=True)

    s = session()
    queue = deque([BASE_URL])
    visited: set[str] = set()
    assets: dict[str, dict[str, str]] = {}
    page_rows: list[dict[str, str]] = []

    while queue and len(visited) < 120:
        page_url = normalize_page_url(queue.popleft())
        if page_url in visited or not is_library_page(page_url):
            continue
        visited.add(page_url)

        try:
            response = s.get(page_url, timeout=45)
            response.raise_for_status()
        except Exception as exc:
            page_rows.append({"page_url": page_url, "title": "", "status": f"ERROR: {exc}"})
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        title = soup.find("h1")
        page_title = title.get_text(" ", strip=True) if title else (soup.title.get_text(" ", strip=True) if soup.title else "")
        page_rows.append({"page_url": page_url, "title": page_title, "status": "OK"})

        for anchor in soup.find_all("a", href=True):
            absolute = normalize_page_url(urljoin(page_url, anchor["href"]))
            if is_library_page(absolute) and absolute not in visited:
                queue.append(absolute)

            include, asset_url = classify_link(page_url, anchor)
            if include:
                asset_url = urljoin(page_url, asset_url)
                assets.setdefault(asset_url, {
                    "source_page": page_url,
                    "page_title": page_title,
                    "link_text": " ".join(anchor.get_text(" ", strip=True).split()),
                })

        time.sleep(0.15)

    manifest_rows: list[dict[str, str | int]] = []
    total = len(assets)
    print(f"Discovered {len(visited)} library pages and {total} candidate web/logo assets")

    for index, (asset_url, meta) in enumerate(sorted(assets.items()), start=1):
        folder = OUT_ROOT / page_folder(meta["source_page"])
        folder.mkdir(parents=True, exist_ok=True)
        filename = clean_filename(asset_url, asset_url)
        target = unique_path(folder, filename, asset_url)
        status = "OK"
        size = 0
        digest = ""

        try:
            with s.get(asset_url, timeout=90, stream=True, allow_redirects=True) as response:
                response.raise_for_status()
                if not target.suffix:
                    ext = infer_extension(response)
                    if ext:
                        target = unique_path(folder, target.name + ext, asset_url)
                hasher = hashlib.sha256()
                with target.open("wb") as f:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            f.write(chunk)
                            hasher.update(chunk)
                            size += len(chunk)
                digest = hasher.hexdigest()
        except Exception as exc:
            status = f"ERROR: {exc}"
            if target.exists():
                target.unlink()

        manifest_rows.append({
            "source_page": meta["source_page"],
            "page_title": meta["page_title"],
            "link_text": meta["link_text"],
            "source_url": asset_url,
            "relative_path": str(target.relative_to(OUT_ROOT)) if status == "OK" else "",
            "bytes": size,
            "sha256": digest,
            "status": status,
        })
        print(f"[{index}/{total}] {status.split(':', 1)[0]} {asset_url}")
        time.sleep(0.08)

    with (OUT_ROOT / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "source_page", "page_title", "link_text", "source_url",
            "relative_path", "bytes", "sha256", "status",
        ])
        writer.writeheader()
        writer.writerows(manifest_rows)

    with (OUT_ROOT / "source_pages.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["page_url", "title", "status"])
        writer.writeheader()
        writer.writerows(page_rows)

    successful = sum(1 for row in manifest_rows if row["status"] == "OK")
    failed = len(manifest_rows) - successful
    total_bytes = sum(int(row["bytes"]) for row in manifest_rows)

    (OUT_ROOT / "README.txt").write_text(
        "Garaga Digital Asset Library — Web Archive\n"
        "==========================================\n\n"
        f"Source: {BASE_URL}\n"
        "Prepared for LIFTX website use.\n\n"
        "Included:\n"
        "- Publicly provided 72 dpi / web-resolution collection imagery\n"
        "- Publicly provided logo and brand-guide downloads\n"
        "- manifest.csv with source URLs, local paths, file sizes, and SHA-256 hashes\n"
        "- source_pages.csv with crawled library pages\n\n"
        "Not included:\n"
        "- 300 dpi print-resolution collection imagery\n\n"
        f"Downloaded files: {successful}\n"
        f"Failed downloads: {failed}\n"
        f"Downloaded bytes: {total_bytes}\n\n"
        "Use assets in accordance with Garaga's current brand guide and terms.\n",
        encoding="utf-8",
    )

    shutil.make_archive(str(ZIP_BASE), "zip", root_dir=OUT_ROOT.parent, base_dir=OUT_ROOT.name)
    zip_path = ZIP_BASE.with_suffix(".zip")
    print(f"Created {zip_path} ({zip_path.stat().st_size} bytes)")

    if successful == 0:
        raise SystemExit("No assets downloaded")


if __name__ == "__main__":
    main()
