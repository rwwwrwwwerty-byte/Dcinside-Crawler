from __future__ import annotations

import argparse
import html
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE_URL = "https://gall.dcinside.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/153.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    "Referer": BASE_URL + "/",
}

GALLERY_PATHS = [
    ("main", "/board/lists/", "/board/view/", "G"),
    ("minor", "/mgallery/board/lists/", "/mgallery/board/view/", "M"),
    ("mini", "/mini/board/lists/", "/mini/board/view/", "MI"),
]


def out(msg: str) -> None:
    print(msg, flush=True)


class DcCrawler:
    def __init__(self, gallery_id: str, delay: float = 0.4, timeout: float = 15.0):
        self.gallery_id = gallery_id
        self.delay = max(0.0, delay)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.gallery_kind: Optional[str] = None
        self.list_path: Optional[str] = None
        self.view_path: Optional[str] = None
        self.gallery_type_code: Optional[str] = None

    def sleep(self) -> None:
        if self.delay:
            time.sleep(self.delay + random.uniform(0, min(0.15, self.delay * 0.25)))

    def detect_gallery(self) -> None:
        out(f"[1/3] 갤러리 확인 중: {self.gallery_id}")
        for kind, list_path, view_path, code in GALLERY_PATHS:
            url = urljoin(BASE_URL, list_path)
            r = self.session.get(url, params={"id": self.gallery_id, "page": 1}, timeout=self.timeout)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            rows = soup.select("tr.ub-content.us-post[data-no], tr.us-post[data-no]")
            if rows:
                self.gallery_kind = kind
                self.list_path = list_path
                self.view_path = view_path
                self.gallery_type_code = code
                out(f"      확인 완료: {kind} gallery")
                return
        raise RuntimeError("갤러리를 찾지 못했습니다. gallery_id를 확인하세요.")

    def fetch_list_page(self, page: int) -> List[Dict[str, str]]:
        assert self.list_path and self.view_path
        url = urljoin(BASE_URL, self.list_path)
        r = self.session.get(url, params={"id": self.gallery_id, "page": page}, timeout=self.timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        rows = soup.select("tr.ub-content.us-post[data-no], tr.us-post[data-no]")
        found: List[Dict[str, str]] = []

        for row in rows:
            post_id = (row.get("data-no") or "").strip()
            if not post_id.isdigit():
                continue
            a = row.select_one("td.gall_tit a[href*='/view/'], a[href*='/view/']")
            if not a:
                continue
            href = a.get("href", "")
            post_url = urljoin(BASE_URL, href)
            title = clean_text(a.get_text(" ", strip=True))
            found.append({
                "post_id": post_id,
                "post_url": post_url,
                "post_title": title,
                "list_page": str(page),
            })
        return found

    def collect_candidates(self, start_page: int, end_page: int, max_posts: int) -> List[Dict[str, str]]:
        out(f"[2/3] 글 목록 수집 중: {start_page}~{end_page}페이지")
        candidates: List[Dict[str, str]] = []
        seen = set()
        total_pages = end_page - start_page + 1

        for idx, page in enumerate(range(start_page, end_page + 1), start=1):
            try:
                rows = self.fetch_list_page(page)
            except Exception as exc:
                out(f"      [경고] {page}페이지 실패: {exc}")
                continue

            for item in rows:
                if item["post_id"] in seen:
                    continue
                seen.add(item["post_id"])
                candidates.append(item)
                if max_posts > 0 and len(candidates) >= max_posts:
                    break

            pct = idx / total_pages * 100
            out(f"      페이지 {idx}/{total_pages} ({pct:5.1f}%) · 글 {len(candidates)}개 발견")
            if max_posts > 0 and len(candidates) >= max_posts:
                break
            self.sleep()

        return candidates

    def fetch_post(self, summary: Dict[str, str]) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
        r = self.session.get(summary["post_url"], timeout=self.timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        title = first_text(soup, [
            ".gallview_head .title_subject",
            ".view_content_wrap .title_subject",
            "span.title_subject",
        ]) or summary["post_title"]

        body = first_text(soup, [
            ".write_div",
            ".writing_view_box",
            ".view_content_wrap .write_div",
        ], preserve_lines=True)

        author_node = soup.select_one(".gall_writer")
        author = ""
        if author_node:
            author = (author_node.get("data-nick") or "").strip() or clean_text(author_node.get_text(" ", strip=True))

        date_node = soup.select_one(".gall_date")
        created = ""
        if date_node:
            created = (date_node.get("title") or "").strip() or clean_text(date_node.get_text(" ", strip=True))

        view_node = soup.select_one(".gall_count")
        views = numeric_from_text(view_node.get_text(" ", strip=True) if view_node else "")

        rec_node = soup.select_one(f"#recommend_view_up_{summary['post_id']}")
        recommends = numeric_from_text(rec_node.get_text(" ", strip=True) if rec_node else "")

        hidden = self.extract_hidden(soup)
        comments = self.fetch_comments(summary["post_url"], hidden, title)

        stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        post = {
            "gallery_id": self.gallery_id,
            "list_page": int(summary["list_page"]),
            "post_id": summary["post_id"],
            "post_url": summary["post_url"],
            "post_title": title,
            "post_body": body,
            "post_author": author,
            "post_created_at": created,
            "post_views": views,
            "post_recommends": recommends,
            "crawl_timestamp": stamp,
        }
        return post, comments

    def extract_hidden(self, soup: BeautifulSoup) -> Dict[str, str]:
        full = str(soup)
        gt = re.search(r'var\s+_GALLERY_TYPE_\s*=\s*"([^"]+)"', full)

        def val(css_id: str) -> str:
            n = soup.select_one(f"#{css_id}")
            return (n.get("value") or "").strip() if n else ""

        data = {
            "id": val("id") or self.gallery_id,
            "no": val("no"),
            "e_s_n_o": val("e_s_n_o"),
            "secret_article_key": val("secret_article_key"),
            "board_type": val("board_type"),
            "gallery_type": gt.group(1) if gt else (self.gallery_type_code or "G"),
        }
        if not data["no"] or not data["e_s_n_o"]:
            raise RuntimeError("댓글 요청에 필요한 값을 찾지 못했습니다.")
        return data

    def fetch_comments(
        self,
        post_url: str,
        values: Dict[str, str],
        post_title: str,
    ) -> List[Dict[str, object]]:
        result: List[Dict[str, object]] = []
        seen = set()
        page = 1

        while True:
            payload = {
                "id": values["id"],
                "no": values["no"],
                "cmt_id": values["id"],
                "cmt_no": values["no"],
                "focus_cno": "",
                "focus_pno": "",
                "e_s_n_o": values["e_s_n_o"],
                "comment_page": str(page),
                "sort": "D",
                "prevCnt": "",
                "board_type": values["board_type"],
                "_GALLTYPE_": values["gallery_type"],
                "secret_article_key": values["secret_article_key"],
            }
            headers = {
                "Referer": post_url,
                "Origin": BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
            }
            r = self.session.post(
                urljoin(BASE_URL, "/board/comment/"),
                data=payload,
                headers=headers,
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json()
            rows = data.get("comments") or []
            if not rows:
                break

            added = 0
            for item in rows:
                comment_id = str(item.get("no") or "")
                if not comment_id or comment_id in seen:
                    continue
                seen.add(comment_id)
                added += 1
                raw = str(item.get("memo") or "")
                txt = clean_comment(raw)
                if not txt:
                    continue
                depth = int(item.get("depth") or 0)
                result.append({
                    "gallery_id": self.gallery_id,
                    "post_id": values["no"],
                    "post_url": post_url,
                    "post_title": post_title,
                    "comment_id": comment_id,
                    "comment_author": clean_text(str(item.get("name") or "")),
                    "comment_created_at": clean_text(str(item.get("reg_date") or "")),
                    "comment_text": txt,
                    "is_reply": depth > 0,
                    "parent_comment_id": str(item.get("c_no") or "") if depth > 0 else "",
                })

            total = int(data.get("total_cnt") or 0)
            if len(seen) >= total or added == 0:
                break
            page += 1
            self.sleep()

        return result


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def clean_comment(raw: str) -> str:
    soup = BeautifulSoup(html.unescape(raw or ""), "html.parser")
    return clean_text(soup.get_text(" ", strip=True))


def numeric_from_text(text: str) -> str:
    m = re.search(r"-?\d[\d,]*", text or "")
    return m.group(0).replace(",", "") if m else ""


def first_text(soup: BeautifulSoup, selectors: List[str], preserve_lines: bool = False) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            sep = "\n" if preserve_lines else " "
            txt = node.get_text(sep, strip=True)
            return txt.strip() if preserve_lines else clean_text(txt)
    return ""


def save_excel(posts: List[Dict[str, object]], comments: List[Dict[str, object]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    posts_df = pd.DataFrame(posts)
    comments_df = pd.DataFrame(comments)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        posts_df.to_excel(writer, index=False, sheet_name="posts")
        comments_df.to_excel(writer, index=False, sheet_name="comments")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--gallery-id", default="jett")
    p.add_argument("--start-page", type=int, default=1)
    p.add_argument("--end-page", type=int, default=5)
    p.add_argument("--max-posts", type=int, default=100)
    p.add_argument("--delay", type=float, default=0.4)
    p.add_argument("--output", default="output/jett_comments.xlsx")
    args = p.parse_args()

    if args.start_page < 1 or args.end_page < args.start_page:
        raise SystemExit("페이지 범위가 잘못되었습니다.")

    crawler = DcCrawler(args.gallery_id, delay=args.delay)
    crawler.detect_gallery()
    candidates = crawler.collect_candidates(args.start_page, args.end_page, args.max_posts)

    total = len(candidates)
    if total == 0:
        raise SystemExit("수집할 글을 찾지 못했습니다.")

    out(f"[3/3] 본문 + 댓글 수집 시작: 총 {total}개 글")
    posts: List[Dict[str, object]] = []
    comments: List[Dict[str, object]] = []
    failures = 0
    started = time.time()

    for i, candidate in enumerate(candidates, start=1):
        try:
            post, rows = crawler.fetch_post(candidate)
            posts.append(post)
            comments.extend(rows)
            status = f"+댓글 {len(rows)}"
        except Exception as exc:
            failures += 1
            status = f"실패: {exc}"

        elapsed = max(0.001, time.time() - started)
        rate = i / elapsed
        remain = (total - i) / rate if rate > 0 else 0
        pct = i / total * 100

        out(
            f"      [{i:>4}/{total}] {pct:6.2f}% · "
            f"댓글 누적 {len(comments):>5}개 · "
            f"남은시간 약 {remain/60:5.1f}분 · "
            f"글 #{candidate['post_id']} · {status}"
        )
        crawler.sleep()

    output = Path(args.output)
    save_excel(posts, comments, output)

    out("")
    out("===== 완료 =====")
    out(f"글: {len(posts)}개")
    out(f"댓글/대댓글: {len(comments)}개")
    out(f"실패: {failures}개")
    out(f"파일: {output}")


if __name__ == "__main__":
    main()
