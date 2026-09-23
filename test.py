from DCArticleCrawler import DCArticleCrawler

# DCInside Jett minor gallery: collect today's posts including comments/replies.
crawler = DCArticleCrawler(
    gallery_id="jett",
    gall_type="minor",
    start_date="2026.09.23",
    end_date="2026.09.23",
    is_crawl_comments=True,
    is_headless=True,
    sleep_between_requests=1.0,
    maximum_batch_size=10,
    jsonl_path="data/jett_articles.jsonl"
)

crawler.run()
