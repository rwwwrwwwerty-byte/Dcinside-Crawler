from __future__ import annotations

import argparse
import csv
import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; UniversalCrawler/1.0; +https://github.com/)"
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".zip", ".rar", ".7z", ".mp3", ".wav", ".mp4", ".avi",
    ".mov", ".css", ".js", ".woff", ".woff2", ".ttf", ".exe", ".dmg",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_url(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return None

    full = urljoin(base, href)
    full, _ = urldefrag(full)
    p = urlparse(full)

    if p.scheme not in {"http", "https"}:
        return None

    path_lower = p.path.lower()
    if any(path_lower.endswith(ext) for ext in SKIP_EXTENSIONS):
        return None

    return full


def same_site(a: str, b: str) -> bool:
    aa = urlparse(a).netloc.lower().removeprefix("www.")
    bb = urlparse(b).netloc.lower().removeprefix("www.")
    return aa == bb


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def extract_page(html: str, url: str, selector: str = "") -> tuple[dict, list[str]]:
    soup = BeautifulSoup(html, "html.parser")

    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""

    description = ""
    desc = soup.select_one('meta[name="description"], meta[property="og:description"]')
    if desc:
        description = clean_text(desc.get("content") or "")

    # Remove common non-content elements for the default extraction mode.
    for node in soup.select("script, style, noscript, template"):
        node.decompose()

    selected_text = ""
    if selector.strip():
        try:
            nodes = soup.select(selector.strip())
        except Exception as exc:
            raise RuntimeError(f"CSS 선택자 오류: {exc}")
        selected_text = "\n".join(clean_text(n.get_text(" ", strip=True)) for n in nodes)
    else:
        main = (
            soup.select_one("main")
            or soup.select_one("article")
            or soup.select_one('[role="main"]')
            or soup.body
        )
        if main:
            for node in main.select("nav, footer, aside"):
                node.decompose()
            selected_text = clean_text(main.get_text(" ", strip=True))

    links: list[str] = []
    seen = set()
    for a in soup.select("a[href]"):
        u = normalize_url(url, a.get("href") or "")
        if u and u not in seen:
            seen.add(u)
            links.append(u)

    row = {
        "url": url,
        "title": title,
        "description": description,
        "text": selected_text[:100000],
        "link_count": len(links),
    }
    return row, links


class RobotsCache:
    def __init__(self, session: requests.Session):
        self.session = session
        self.cache: dict[str, Optional[RobotFileParser]] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin not in self.cache:
            robots_url = origin + "/robots.txt"
            try:
                r = self.session.get(robots_url, timeout=10)
                if r.status_code == 200:
                    rp = RobotFileParser()
                    rp.set_url(robots_url)
                    rp.parse(r.text.splitlines())
                    self.cache[origin] = rp
                else:
                    self.cache[origin] = None
            except Exception:
                self.cache[origin] = None

        rp = self.cache[origin]
        return True if rp is None else rp.can_fetch(UA, url)


def crawl_http(
    start_url: str,
    max_pages: int,
    selector: str,
    restrict_domain: bool,
    delay: float,
    respect_robots: bool,
) -> list[dict]:
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    })
    robots = RobotsCache(session)

    queue = deque([start_url])
    queued = {start_url}
    visited = set()
    rows: list[dict] = []

    while queue and len(visited) < max_pages:
        url = queue.popleft()
        if url in visited:
            continue

        if respect_robots and not robots.allowed(url):
            log(f"[skip] robots.txt 차단: {url}")
            visited.add(url)
            continue

        index = len(visited) + 1
        log(f"[{index}/{max_pages}] {index/max_pages*100:6.2f}% · 대기 {len(queue)} · {url}")

        visited.add(url)
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            content_type = (r.headers.get("content-type") or "").lower()
            if "text/html" not in content_type:
                rows.append({
                    "url": url,
                    "status": r.status_code,
                    "title": "",
                    "description": "",
                    "text": "",
                    "link_count": 0,
                    "error": f"HTML 아님: {content_type}",
                })
                continue

            row, links = extract_page(r.text, r.url, selector)
            row["status"] = r.status_code
            row["error"] = ""
            rows.append(row)

            for link in links:
                if restrict_domain and not same_site(start_url, link):
                    continue
                if link not in visited and link not in queued:
                    queue.append(link)
                    queued.add(link)

        except Exception as exc:
            rows.append({
                "url": url,
                "status": "",
                "title": "",
                "description": "",
                "text": "",
                "link_count": 0,
                "error": str(exc),
            })
            log(f"        실패: {exc}")

        if delay > 0:
            time.sleep(delay)

    return rows


def crawl_browser(
    start_url: str,
    max_pages: int,
    selector: str,
    restrict_domain: bool,
    delay: float,
) -> list[dict]:
    from playwright.sync_api import sync_playwright

    queue = deque([start_url])
    queued = {start_url}
    visited = set()
    rows: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=UA, locale="ko-KR")
        page = context.new_page()

        while queue and len(visited) < max_pages:
            url = queue.popleft()
            if url in visited:
                continue

            index = len(visited) + 1
            log(f"[{index}/{max_pages}] {index/max_pages*100:6.2f}% · 대기 {len(queue)} · {url}")
            visited.add(url)

            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass

                final_url = page.url
                html = page.content()
                row, links = extract_page(html, final_url, selector)
                row["status"] = response.status if response else ""
                row["error"] = ""
                rows.append(row)

                for link in links:
                    if restrict_domain and not same_site(start_url, link):
                        continue
                    if link not in visited and link not in queued:
                        queue.append(link)
                        queued.add(link)

            except Exception as exc:
                rows.append({
                    "url": url,
                    "status": "",
                    "title": "",
                    "description": "",
                    "text": "",
                    "link_count": 0,
                    "error": str(exc),
                })
                log(f"        실패: {exc}")

            if delay > 0:
                time.sleep(delay)

        browser.close()

    return rows


def save(rows: list[dict], output_dir: str) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    fields = ["url", "status", "title", "description", "text", "link_count", "error"]

    with (out / "pages.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})

    with (out / "pages.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="범용 공개 웹 크롤러")
    parser.add_argument("--url", required=True)
    parser.add_argument("--mode", choices=["http", "browser"], default="http")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--selector", default="")
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--ignore-robots", action="store_true")
    parser.add_argument("--output-dir", default="universal_output")
    args = parser.parse_args()

    start_url = normalize_url(args.url, args.url)
    if not start_url:
        raise SystemExit("올바른 http/https URL을 입력하세요.")

    max_pages = max(1, min(args.max_pages, 1000))
    restrict_domain = not args.allow_external

    log("===== Universal Web Crawler =====")
    log(f"시작 URL : {start_url}")
    log(f"모드     : {args.mode}")
    log(f"최대 페이지: {max_pages}")
    log(f"같은 사이트만: {restrict_domain}")
    log(f"CSS 선택자: {args.selector or '(자동 본문 추출)'}")
    log("")

    if args.mode == "browser":
        rows = crawl_browser(
            start_url,
            max_pages,
            args.selector,
            restrict_domain,
            args.delay,
        )
    else:
        rows = crawl_http(
            start_url,
            max_pages,
            args.selector,
            restrict_domain,
            args.delay,
            respect_robots=not args.ignore_robots,
        )

    save(rows, args.output_dir)

    log("")
    log("===== 완료 =====")
    log(f"수집 페이지: {len(rows)}")
    log(f"성공: {sum(1 for r in rows if not r.get('error'))}")
    log(f"실패: {sum(1 for r in rows if r.get('error'))}")
    log(f"CSV: {args.output_dir}/pages.csv")
    log(f"JSONL: {args.output_dir}/pages.jsonl")


if __name__ == "__main__":
    main()
