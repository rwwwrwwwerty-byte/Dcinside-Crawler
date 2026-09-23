import argparse
import csv
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from DCArticleCrawler import DCArticleCrawler

KST = timezone(timedelta(hours=9))


def today_kst():
    return datetime.now(KST).strftime("%Y.%m.%d")


def jsonl_to_csv(jsonl_path: Path, csv_path: Path):
    fields = [
        "gall_no",
        "date",
        "title",
        "content",
        "view_count",
        "recommend_count",
        "nonrecommend_count",
        "comments_json",
    ]
    with jsonl_path.open("r", encoding="utf-8") as src, csv_path.open(
        "w", encoding="utf-8-sig", newline=""
    ) as dst:
        writer = csv.DictWriter(dst, fieldnames=fields)
        writer.writeheader()
        for line in src:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            writer.writerow(
                {
                    "gall_no": item.get("gall_no", ""),
                    "date": item.get("date", ""),
                    "title": item.get("title", ""),
                    "content": item.get("content", ""),
                    "view_count": item.get("view_count", ""),
                    "recommend_count": item.get("recommend_count", ""),
                    "nonrecommend_count": item.get("nonrecommend_count", ""),
                    "comments_json": json.dumps(
                        item.get("comments", []), ensure_ascii=False
                    ),
                }
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gallery-id", default="jett")
    parser.add_argument("--gallery-type", default="minor", choices=["main", "minor", "mini"])
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    args = parser.parse_args()

    start_date = args.start_date.strip() or today_kst()
    end_date = args.end_date.strip() or start_date

    out_dir = Path("output")
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_start = start_date.replace(".", "-")
    safe_end = end_date.replace(".", "-")
    base = f"{args.gallery_id}_{safe_start}_{safe_end}"
    jsonl_path = out_dir / f"{base}.jsonl"
    csv_path = out_dir / f"{base}.csv"

    crawler = DCArticleCrawler(
        gallery_id=args.gallery_id,
        gall_type=args.gallery_type,
        start_date=start_date,
        end_date=end_date,
        is_crawl_comments=True,
        is_headless=True,
        sleep_between_requests=1.0,
        maximum_batch_size=10,
        jsonl_path=str(jsonl_path),
    )
    crawler.run()

    if not jsonl_path.exists():
        raise SystemExit("결과 파일이 생성되지 않았습니다.")

    jsonl_to_csv(jsonl_path, csv_path)
    print(f"완료: {jsonl_path}")
    print(f"완료: {csv_path}")


if __name__ == "__main__":
    main()
