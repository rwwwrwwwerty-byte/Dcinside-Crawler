import os
import json
import datetime
import time
import logging
from typing import List, Set, Optional

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
import requests
import dataclasses

from DCArticleProcessor import DCArticleProcessor, ArticleData, make_url_for_article


# ====== Configuration ======
DATE_FORMAT = "%Y.%m.%d"


# ====== Logging Configuration ======
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


def load_collected_gall_no(articles_jsonl_file: str) -> Set[int]:
    """Load collected article IDs (gall_no) from the JSONL file."""

    collected_gall_no: Set[int] = set()
    try:
        with open(articles_jsonl_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    article = json.loads(line)
                    collected_gall_no.add(int(article['gall_no']))
                except json.JSONDecodeError:
                    continue
        return collected_gall_no
    except:
        logger.warning(f"Failed to load collected IDs from {articles_jsonl_file}.")
        return set()


def save_data_in_batch(
    jsonl_path: str,
    batch: List[ArticleData]
) -> None:
    """Save the batch of articles to JSONL file.
    
    The articles will be saved in the following format:
    {
        "gall_no": int,
        "date": "YYYY.MM.DD",
        "title": str,
        "view_count": int,
        "content": str,
        "recommend_count": int,
        "nonrecommend_count": int,
        "comments": [
            {
                "text": str,
                "replies": List[str]
            }
        ]
    }
    """
    os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)
    with open(jsonl_path, 'a', encoding='utf-8') as f:
        for article in batch:
            # Convert datetime to string format
            if isinstance(article.date, datetime.datetime):
                article.date = article.date.strftime(DATE_FORMAT)
            
            # Ensure all numeric fields are converted to proper type
            article.gall_no = int(article.gall_no)
            article.view_count = int(article.view_count)
            article.recommend_count = int(article.recommend_count)
            article.nonrecommend_count = int(article.nonrecommend_count)
            # Remove header field from article data
            article_dict = dataclasses.asdict(article)
            article_dict.pop('header', None)
            json.dump(article_dict, f, ensure_ascii=False)
            f.write('\n')


"""Crawl and save articles using DCArticleProcessor"""
class DCArticleCrawler:
    def __init__(self,
                 gallery_id: str,
                 gall_type: str, # 'main', 'minor', or 'mini'
                 start_gall_no: int = None,
                 end_gall_no: int = None,
                 start_date: str = None, # Format: 'YYYY.MM.DD'
                 end_date: str = None, # Format: 'YYYY.MM.DD'
                 is_crawl_comments: bool = True,
                 refresh_time_for_comment: float = 0.5,
                 sleep_between_requests: float = 0.5,
                 excluded_heads: Optional[List[str]] = None,
                 is_headless: bool = True,
                 maximum_batch_size: int = 100,
                 jsonl_path: Optional[str] = None):

        self.gallery_id = gallery_id
        self.gall_type = gall_type

        self.start_gall_no = start_gall_no
        self.end_gall_no = end_gall_no
        self.start_date = start_date
        self.end_date = end_date

        self.is_crawl_comments = is_crawl_comments
        self.refresh_time_for_comment = refresh_time_for_comment
        self.sleep_between_requests = sleep_between_requests

        # TODO: Implement excluded_heads functionality when minor/mini galleries are supported
        self.excluded_heads = excluded_heads
        self.is_headless = is_headless

        self.maximum_batch_size = maximum_batch_size
        self.jsonl_path = jsonl_path

        self.driver = self._init_driver()
        self.headers = {'User-Agent' : 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)AppleWebKit/537.36 (KHTML, like Gecko) Chrome/73.0.3683.86 Safari/537.36'}
    
    def check_gallery_conditions(self):
        """Check if the current gallery meets the conditions."""
        url_for_check = make_url_for_article(gallery_type=self.gall_type, gallery_id=self.gallery_id, gall_no=1)
        url_for_check = url_for_check.replace('&no=1', '').replace('view', 'lists')
        
        response = requests.get(url_for_check, headers=self.headers)
        if response.status_code == 404:
            raise ValueError(f"Gallery {self.gallery_id} with gall_type {self.gall_type} does not exist.")

    def select_recent_gall_no(self, soup: BeautifulSoup) -> int:
        """Select the most recent gall_no if crawl_article_based_on_date is True.
        As we cannot get gall_no from date info, we will use the most recent gall_no
        and check if it is within the date range."""
        
        post_rows = soup.select('tr.us-post[data-no]')
        for row in post_rows:
            data_type = row.get('data-type')
            if data_type and data_type == 'icon_notice':
                continue
            recent_gall_no = row.get('data-no')
            if recent_gall_no and recent_gall_no.isdigit():
                recent_gall_no = int(recent_gall_no)
                return recent_gall_no
        
        raise ValueError("No recent regular gallery post number found.")

    def check_scrapping_conditions(self):
        """Check if the current article meets the scrapping conditions."""
        if bool(self.start_gall_no) ^ bool(self.end_gall_no):
            raise ValueError("`start_gall_no` and `end_gall_no` must be provided together.")
        if bool(self.start_date) ^ bool(self.end_date):
            raise ValueError("`start_date` and `end_date` must be provided together.")
        
        has_gall_no_range = (self.start_gall_no is not None and self.end_gall_no is not None)
        has_date_range = (self.start_date    is not None and self.end_date    is not None)
        if not (has_gall_no_range or has_date_range):
            raise ValueError(
                """Scrapping conditions must be provided.
                Please provide either range of gall_no (start_gall_no & end_gall_no)
                or date range (start_date & end_date)."""
            )
        
        if has_gall_no_range and has_date_range:
            raise ValueError(
                "Both gall_no range and date range provided. Please provide only one of them."
            )

        if has_gall_no_range:
            # Condition given by gall_no range
            # Check if gall_no range is valid
            if self.start_gall_no > self.end_gall_no:
                raise ValueError("`start_gall_no` must be less than or equal to `end_gall_no`.")
            else:
                self.gall_no = self.start_gall_no
                self.crawl_article_based_on_gall_no = True
        else:
            # Condition given by date range
            # Check if date range is valid
            try:
                self.start_date = datetime.datetime.strptime(self.start_date, DATE_FORMAT)
                self.end_date = datetime.datetime.strptime(self.end_date, DATE_FORMAT)
            except ValueError:
                raise ValueError(f"Invalid date format. Please use {DATE_FORMAT}.")
            
            if self.start_date > self.end_date:
                raise ValueError("`start_date` must be less than or equal to `end_date`.")
            else:
                temp_url_for_initial_gall_no = make_url_for_article(gallery_type=self.gall_type, gallery_id=self.gallery_id, gall_no=1)
                temp_url_for_initial_gall_no = temp_url_for_initial_gall_no.replace('&no=1', '').replace('view', 'lists')
                temp_data = requests.get(temp_url_for_initial_gall_no, headers=self.headers)
                temp_soup = BeautifulSoup(temp_data.text, 'html.parser')
                recent_gall_no = self.select_recent_gall_no(temp_soup)
                self.gall_no = recent_gall_no
                self.crawl_article_based_on_gall_no = False

    def _init_driver(self) -> webdriver.Chrome:
        """Initialize Selenium with the Chromium installed by Playwright/Codespaces."""
        import glob

        chromium_patterns = [
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux/chrome"),
            os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"),
            os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"),
            os.path.expanduser("~/.cache/ms-playwright/**/chrome"),
        ]

        chromium_paths = []
        for pattern in chromium_patterns:
            chromium_paths.extend(glob.glob(pattern, recursive=True))

        chromium_paths = [
            path for path in chromium_paths
            if os.path.isfile(path) and os.access(path, os.X_OK)
        ]

        if not chromium_paths:
            raise RuntimeError(
                "Playwright Chromium을 찾을 수 없습니다. "
                "터미널에서 'python -m playwright install --with-deps chromium'을 실행하세요."
            )

        chromium_binary = chromium_paths[0]
        logger.info(f"Using Chromium binary: {chromium_binary}")

        options = webdriver.ChromeOptions()
        options.binary_location = chromium_binary

        if self.is_headless:
            options.add_argument("--headless=new")

        # Required/recommended for browser execution in GitHub Codespaces.
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")

        # Selenium Manager resolves a compatible ChromeDriver for this Chromium.
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(10)

        return driver
    
    def run(self):
        # Check if the gallery conditions are valid
        self.check_gallery_conditions()
        
        # Check if the scrapping conditions are valid
        # e.g. gall_no range or date range
        self.check_scrapping_conditions()
        
        # Collected gall_no for checking duplicates
        collected_gall_no = load_collected_gall_no(self.jsonl_path)
        
        logger.info(f"Initialized DCArticleCrawler for {self.gallery_id} with gall_type {self.gall_type}.")
        if self.start_gall_no is not None:
            logger.info(f"start gall_no: {self.start_gall_no}, end gall_no: {self.end_gall_no}.")
        else:
            logger.info(f"start date: {self.start_date}, end date: {self.end_date}.")

        batch = []
        
        try:
            if self.crawl_article_based_on_gall_no is True:
                logger.info("Crawling articles based on gall_no range.")
                # Crawl articles based on gall_no range
                while self.gall_no <= self.end_gall_no:
                    if self.gall_no in collected_gall_no:
                        logger.info(f"Article {self.gall_no} already collected. Skipping...")
                        self.gall_no += 1
                        continue
                    
                    if self.is_crawl_comments is True:
                        article_processor = DCArticleProcessor(
                            gallery_id=self.gallery_id,
                            gall_type=self.gall_type,
                            gall_no=self.gall_no,
                            headers=self.headers,
                            is_crawl_comments=True,
                            refresh_time_for_comment=self.refresh_time_for_comment,
                            driver=self.driver
                        )
                    else:
                        article_processor = DCArticleProcessor(
                            gallery_id=self.gallery_id,
                            gall_type=self.gall_type,
                            gall_no=self.gall_no,
                            headers=self.headers,
                            is_crawl_comments=False,
                            refresh_time_for_comment=self.refresh_time_for_comment,
                            driver=self.driver
                        )
                    logger.info(f"Processing article {self.gall_no}...")

                    article_data: Optional[ArticleData] = article_processor.process_article()
                    # logger.info(f"article_data: {article_data}")
                    if article_data is not None:
                        batch.append(article_data)
                        logger.info(f"Collected article {self.gall_no}.")
                    
                    if len(batch) >= self.maximum_batch_size:
                        save_data_in_batch(self.jsonl_path, batch)
                        logger.info(f"Saved {len(batch)} articles to {self.jsonl_path}.")
                        batch.clear()
                    
                    self.gall_no += 1
                    time.sleep(self.sleep_between_requests)
                save_data_in_batch(self.jsonl_path, batch)
                logger.info(f"Saved {len(batch)} articles to {self.jsonl_path}.")
                batch.clear()
            else:
                logger.info("Crawling articles based on date range.")
                
                while True:
                    article_processor = DCArticleProcessor(
                        gallery_id=self.gallery_id,
                        gall_type=self.gall_type,
                        gall_no=self.gall_no,
                        headers=self.headers,
                        is_crawl_comments=False,
                        refresh_time_for_comment=self.refresh_time_for_comment,
                        driver=self.driver
                    )
                    article_data: Optional[ArticleData] = article_processor.process_article()
                    if article_data is None:
                        self.gall_no -= 1
                        continue
                    
                    # Check if the article date is within the specified range
                    if self.start_date <= article_data.date <= self.end_date:
                        if self.gall_no in collected_gall_no:
                            logger.info(f"Article {self.gall_no} already collected. Skipping...")
                        else:
                            logger.info(f"Processing article {self.gall_no}...")
                            if self.is_crawl_comments is False:
                                # If not crawling comments, we can directly append the article data
                                batch.append(article_data)
                            else:
                                # If crawling comments, we need to process the article again with crawling comments
                                article_processor = DCArticleProcessor(
                                    gallery_id=self.gallery_id,
                                    gall_type=self.gall_type,
                                    gall_no=self.gall_no,
                                    headers=self.headers,
                                    is_crawl_comments=True,
                                    refresh_time_for_comment=self.refresh_time_for_comment,
                                    driver=self.driver
                                )
                                article_data = article_processor.process_article()
                                batch.append(article_data)
                            logger.info(f"Collected article {self.gall_no}.")
                            
                            if len(batch) >= self.maximum_batch_size:
                                save_data_in_batch(self.jsonl_path, batch)
                                logger.info(f"Saved {len(batch)} articles to {self.jsonl_path}.")
                                batch.clear()
                    elif article_data.date > self.end_date:
                        logger.info(f"Article {self.gall_no} is after the specified end date.")
                    elif article_data.date < self.start_date:
                        logger.error(f"No article between {self.start_date} and {self.end_date}. Stopping.")
                        break
                    
                    self.gall_no -= 1
                    time.sleep(self.sleep_between_requests)
                save_data_in_batch(self.jsonl_path, batch)
                logger.info(f"Saved {len(batch)} articles to {self.jsonl_path}.")
                batch.clear()
        except KeyboardInterrupt:
            save_data_in_batch(self.jsonl_path, batch)
            logger.info(f"Saved {len(batch)} articles to {self.jsonl_path}.")
            batch.clear()
        finally:
            self.driver.quit()
