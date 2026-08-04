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
ASSET_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".pdf", ".zip", ".eps", ".ai", ".tif", ".tiff", ".doc", ".docx"}
GARAGA_HOSTS = {"www.garaga.com", "garaga.com"}
ASSET_HOST_HINTS = ("cmsgaraga.garaga.com", "cmsgaraga.s3.amazonaws.com", "garaga.s3.amazonaws.com")
RESIDENTIAL = {"princeton", "eastman", "cambridge", "california", "village-collection", "standard-plus", "acadia-138", "vantage", "regal", "top-tech", "h-tech"}
COMMERCIAL = {"g-1000", "g-2020", "g-4400", "g-5000", "s-24", "ssg", "ssi-24"}


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, connect=2, read=2, backoff_factor=.5, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=12, pool_maxsize=12))
    s.headers.update({
        "User-Agent": "LIFTX-Garaga-Asset-Archive/2.0 (+https://www.liftxdoor.com)",
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def normalize(url: str) -> str:
    p = urlparse(url)
    path = re.sub(r"/+", "/", p.path).rstrip("/") or "/"
    return urlunparse((p.scheme or "https", p.netloc.lower(), path, "", "", ""))


def is_library_page(url: str) -> bool:
    p = urlparse(url)
    ext = Path(unquote(p.path)).suffix.lower()
    return p.netloc.lower() in GARAGA_HOSTS and p.path.startswith("/digital-asset-library") and ext not in ASSET_EXTS


def is_asset(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower() in ASSET_HOST_HINTS or Path(unquote(p.path)).suffix.lower() in ASSET_EXTS


def special_page(page_url: str, title: str) -> bool:
    key = f"{urlparse(page_url).path} {title}".lower()
    return any(term in key for term in ("logo", "brand guide", "user guide", "partner brand", "garaga corporate", "pro dealer", "liftmaster"))


def page_folder(page_url: str, title: str) -> Path:
    parts = [p for p in urlparse(page_url).path.strip("/").split("/") if p][1:]
    slug = parts[-1] if parts else "root"
    key = f"{'/'.join(parts)} {title}".lower()
    if "logo" in key or "brand" in key or "user-guide" in key:
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
    return name or hashlib.sha1(url.encode()).hexdigest()[:16]


def infer_ext(content_type: str) -> str:
    return {
        "image/jpeg": ".jpg", "image/png": ".png", "image/svg+xml": ".svg",
        "image/webp": ".webp", "application/pdf": ".pdf", "application/zip": ".zip",
    }.get(content_type.split(";", 1)[0].lower(), "")


def discover() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    s = make_session()
    queue = deque([BASE_URL])
    visited: set[str] = set()
    assets: dict[str, dict[str, str]] = {}
    pages: list[dict[str, str]] = []

    while queue and len(visited) < 80:
        page_url = normalize(queue.popleft())
        if page_url in visited or not is_library_page(page_url):
            continue
        visited.add(page_url)
        try:
            r = s.get(page_url, timeout=(10, 25))
            r.raise_for_status()
            if "text/html" not in r.headers.get("content-type", ""):
                continue
        except Exception as exc:
            pages.append({"page_url": page_url, "title": "", "status": f"ERROR: {exc}"})
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(" ", strip=True) if soup.title else "")
        pages.append({"page_url": page_url, "title": title, "status": "OK"})
        is_special = special_page(page_url, title)

        for a in soup.find_all("a", href=True):
            raw = urljoin(page_url, a["href"])
            page_candidate = normalize(raw)
            if is_library_page(page_candidate) and page_candidate not in visited:
                queue.append(page_candidate)

            p = urlparse(raw)
            if p.scheme not in {"http", "https"}:
                continue
            text = " ".join(a.get_text(" ", strip=True).split())
            context = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else text
            low = f"{raw} {text} {context}".lower()
            web_asset = ("72dpi" in low or "72 dpi" in low or "web file" in low) and "300dpi" not in low and "300 dpi" not in low and "printable file" not in low
            special_asset = is_special and is_asset(raw) and urlparse(raw).netloc.lower() not in GARAGA_HOSTS
            if web_asset or special_asset:
                assets.setdefault(raw, {"source_page": page_url, "page_title": title, "link_text": text})

    return list(assets.values() | []), pages


def discover_assets() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    # Same discovery logic, retaining the URL as a field for deterministic packaging.
    s = make_session()
    queue = deque([BASE_URL])
    visited: set[str] = set()
    assets: dict[str, dict[str, str]] = {}
    pages: list[dict[str, str]] = []

    while queue and len(visited) < 80:
        page_url = normalize(queue.popleft())
        if page_url in visited or not is_library_page(page_url):
            continue
        visited.add(page_url)
        try:
            r = s.get(page_url, timeout=(10, 25))
            r.raise_for_status()
            if "text/html" not in r.headers.get("content-type", ""):
                continue
        except Exception as exc:
            pages.append({"page_url": page_url, "title": "", "status": f"ERROR: {exc}"})
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(" ", strip=True) if soup.title else "")
        pages.append({"page_url": page_url, "title": title, "status": "OK"})
        is_special = special_page(page_url, title)

        for a in soup.find_all("a", href=True):
            raw = urljoin(page_url, a["href"])
            candidate = normalize(raw)
            if is_library_page(candidate) and candidate not in visited:
                queue.append(candidate)

            p = urlparse(raw)
            if p.scheme not in {"http", "https"}:
                continue
            text = " ".join(a.get_text(" ", strip=True).split())
            context = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else text
            low = f"{raw} {text} {context}".lower()
            web_asset = ("72dpi" in low or "72 dpi" in low or "web file" in low) and not any(x in low for x in ("300dpi", "300 dpi", "printable file"))
            special_asset = is_special and is_asset(raw) and urlparse(raw).netloc.lower() not in GARAGA_HOSTS
            if web_asset or special_asset:
                assets.setdefault(raw, {
                    "source_url": raw,
                    "source_page": page_url,
                    "page_title": title,
                    "link_text": text,
                })

    print(f"Discovered {len(visited)} pages and {len(assets)} assets")
    return list(assets.values()), pages


def download_one(item: dict[str, str]) -> dict[str, str | int]:
    url = item["source_url"]
    folder = OUT_ROOT / page_folder(item["source_page"], item["page_title"])
    folder.mkdir(parents=True, exist_ok=True)
    name = filename_from_url(url)
    target = folder / name
    if target.exists():
        target = folder / f"{target.stem}-{hashlib.sha1(url.encode()).hexdigest()[:8]}{target.suffix}"

    size = 0
    sha = ""
    status = "OK"
    s = make_session()
    try:
        with s.get(url, timeout=(10, 35), stream=True, allow_redirects=True) as r:
            r.raise_for_status()
            if not target.suffix:
                ext = infer_ext(r.headers.get("content-type", ""))
                if ext:
                    target = target.with_suffix(ext)
            h = hashlib.sha256()
            with target.open("wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
            sha = h.hexdigest()
    except Exception as exc:
        status = f"ERROR: {exc}"
        target.unlink(missing_ok=True)

    return {
        **item,
        "relative_path": str(target.relative_to(OUT_ROOT)) if status == "OK" else "",
        "bytes": size,
        "sha256": sha,
        "status": status,
    }


def main() -> None:
    shutil.rmtree(OUT_ROOT, ignore_errors=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    Path("output").mkdir(exist_ok=True)

    assets, pages = discover_assets()
    rows: list[dict[str, str | int]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(download_one, item) for item in assets]
        for i, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"[{i}/{len(futures)}] {row['status']} {row['source_url']}")

    fields = ["source_page", "page_title", "link_text", "source_url", "relative_path", "bytes", "sha256", "status"]
    with (OUT_ROOT / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(sorted(rows, key=lambda x: str(x["source_url"])))
    with (OUT_ROOT / "source_pages.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["page_url", "title", "status"])
        w.writeheader(); w.writerows(pages)

    ok = sum(1 for r in rows if r["status"] == "OK")
    failed = len(rows) - ok
    total_bytes = sum(int(r["bytes"]) for r in rows)
    (OUT_ROOT / "README.txt").write_text(
        "Garaga Digital Asset Library — Web Archive\n"
        "==========================================\n\n"
        f"Source: {BASE_URL}\n"
        "Prepared for LIFTX website use.\n\n"
        "Included: public 72 dpi web imagery, official logo downloads, brand-guide resources, and a source manifest.\n"
        "Excluded: 300 dpi print imagery.\n\n"
        f"Downloaded: {ok}\nFailed: {failed}\nBytes: {total_bytes}\n\n"
        "Use assets in accordance with Garaga's current brand guide and terms.\n",
        encoding="utf-8",
    )
    shutil.make_archive(str(ZIP_BASE), "zip", root_dir=OUT_ROOT.parent, base_dir=OUT_ROOT.name)
    print(f"Created {ZIP_BASE.with_suffix('.zip')} ({ZIP_BASE.with_suffix('.zip').stat().st_size} bytes)")
    if ok == 0:
        raise SystemExit("No assets downloaded")


if __name__ == "__main__":
    main()
