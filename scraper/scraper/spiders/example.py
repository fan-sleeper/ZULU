import json
import os
from datetime import UTC, datetime
from pathlib import Path

import scrapy
from playwright.sync_api import sync_playwright
from urllib.parse import parse_qs, urlparse


class PromedSpider(scrapy.Spider):
    name = "example"

    custom_settings = {
        "ROBOTSTXT_OBEY": False,
        "DOWNLOAD_DELAY": 0.1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "LOG_LEVEL": "INFO",
    }

    def __init__(self, *args, mode="backfill", max_pages=None, start_page=1, **kwargs):
        super().__init__(*args, **kwargs)

        self.mode = mode.lower()
        self.start_page = int(start_page)
        self.item_count = 0
        self.key_refreshed = False

        if self.mode not in {"backfill", "incremental"}:
            raise ValueError("mode must be either 'backfill' or 'incremental'.")

        if max_pages is not None:
            self.max_pages = int(max_pages)
        elif self.mode == "incremental":
            self.max_pages = 10
        else:
            self.max_pages = None

        self.key_path = Path(__file__).resolve().parents[1] / "promed_key.txt"
        self.api_key = self.load_api_key()

        self.logger.info(
            "Spider started with mode=%s, start_page=%s, max_pages=%s",
            self.mode,
            self.start_page,
            self.max_pages,
        )

    def load_api_key(self):
        if not self.key_path.exists():
            raise FileNotFoundError("promed_key.txt not found. Run get_key.py first.")

        key = self.key_path.read_text(encoding="utf-8").strip()
        if not key:
            raise ValueError("promed_key.txt is empty.")

        return key

    def refresh_api_key(self):
        email = os.getenv("PROMED_EMAIL")
        password = os.getenv("PROMED_PASSWORD")

        if not email or not password:
            raise RuntimeError("PROMED_EMAIL and PROMED_PASSWORD must be set.")

        self.logger.warning("Refreshing ProMED API key...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()

            captured = {"key": None}

            def on_request(request):
                url = request.url
                if "multi_search" in url and "x-typesense-api-key=" in url:
                    qs = parse_qs(urlparse(url).query)
                    key = qs.get("x-typesense-api-key", [None])[0]
                    if key:
                        captured["key"] = key

            page.on("request", on_request)

            page.goto("https://www.promedmail.org/", wait_until="domcontentloaded")
            page.get_by_role("link", name="Search", exact=True).click()

            page.wait_for_selector("#username", timeout=30000)
            page.fill("#username", email)
            page.fill("#password", password)
            page.locator("button[type='submit']").click()

            page.wait_for_url("**/search", timeout=60000)
            page.wait_for_timeout(5000)

            browser.close()

        new_key = captured["key"]
        if not new_key:
            raise RuntimeError("Could not capture refreshed Typesense API key.")

        self.key_path.write_text(new_key, encoding="utf-8")
        self.api_key = new_key
        self.key_refreshed = True

        self.logger.info("Successfully refreshed ProMED API key")

    def start_requests(self):
        yield self.make_request(page=self.start_page)

    def make_request(self, page):
        api_url = (
            "https://vcil9zn2w7dhj8kpp.a1.typesense.net/multi_search"
            f"?x-typesense-api-key={self.api_key}"
        )

        search_fields = (
            "full_text,post_title,subject_line,"
            "moderator_comments,places,diseases,species"
        )

        payload = {
            "searches": [
                {
                    "collection": "alerts",
                    "facet_by": "issue_date,network",
                    "filter_by": "network:=[`ProMED Mail`]",
                    "highlight_full_fields": search_fields,
                    "max_facet_values": 50,
                    "min_len_1typo": 6,
                    "min_len_2typo": 9,
                    "page": page,
                    "q": "*",
                    "query_by": search_fields,
                    "sort_by": "issue_date:desc",
                },
                {
                    "collection": "alerts",
                    "facet_by": "network",
                    "highlight_full_fields": search_fields,
                    "max_facet_values": 50,
                    "min_len_1typo": 6,
                    "min_len_2typo": 9,
                    "page": 1,
                    "per_page": 0,
                    "q": "*",
                    "query_by": search_fields,
                },
            ]
        }

        return scrapy.Request(
            url=api_url,
            method="POST",
            body=json.dumps(payload),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "text/plain",
                "Origin": "https://www.promedmail.org",
                "Referer": "https://www.promedmail.org/",
            },
            callback=self.parse,
            cb_kwargs={"page": page},
            dont_filter=True,
        )

    def extract_names(self, items):
        if not items:
            return []
        return [item.get("name") for item in items if item.get("name")]

    def extract_regions(self, places):
        if not places:
            return []

        regions = []
        seen = set()

        for place in places:
            location = place.get("location", {})
            continent = location.get("continent")
            if continent and continent not in seen:
                seen.add(continent)
                regions.append(continent)

        return regions

    def extract_locations(self, places):
        if not places:
            return []

        cleaned_locations = []

        for place in places:
            location = place.get("location", {})
            country = location.get("country") or place.get("country")
            region = location.get("region")
            locality = location.get("locality")

            cleaned_locations.append(
                [
                    country or "",
                    region or "",
                    locality or "",
                ]
            )

        return cleaned_locations

    def format_date(self, timestamp):
        if not timestamp:
            return None
        return datetime.fromtimestamp(timestamp, UTC).date().isoformat()

    def should_continue(self, page, hits):
        if not hits:
            self.logger.info("Stopping: no more hits on page %s", page)
            return False

        if self.max_pages is not None:
            last_page = self.start_page + self.max_pages - 1
            if page >= last_page:
                self.logger.info(
                    "Stopping: reached max_pages=%s from start_page=%s",
                    self.max_pages,
                    self.start_page,
                )
                return False

        return True

    def response_looks_like_auth_failure(self, response, data):
        if response.status in {401, 403}:
            return True

        text = response.text.lower()
        if "api key" in text and (
            "invalid" in text or "expired" in text or "unauthorized" in text
        ):
            return True

        if isinstance(data, dict):
            message = str(data.get("message", "")).lower()
            if "api key" in message and (
                "invalid" in message or "expired" in message or "unauthorized" in message
            ):
                return True

        return False

    def parse(self, response, page):
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            data = {}

        if response.status != 200 or self.response_looks_like_auth_failure(response, data):
            if not self.key_refreshed:
                self.logger.warning(
                    "Auth failure on page %s. Refreshing key and retrying once.",
                    page,
                )
                self.refresh_api_key()
                yield self.make_request(page)
                return

            self.logger.error("Page %s failed even after key refresh", page)
            self.logger.error("Status: %s", response.status)
            self.logger.error(response.text)
            return

        hits = data.get("results", [{}])[0].get("hits", [])
        self.logger.info("Page %s: scraped %s alerts", page, len(hits))

        for hit in hits:
            doc = hit["document"]
            self.item_count += 1

            external_id = str(doc.get("alert_id"))
            title = doc.get("post_title")
            diseases = self.extract_names(doc.get("diseases"))
            species = self.extract_names(doc.get("species"))
            places = doc.get("places")
            regions = self.extract_regions(places)
            locations = self.extract_locations(places)
            date = self.format_date(doc.get("issue_date"))

            self.logger.info(
                "data #%s | page=%s | id=%s | title=%s | diseases=%s | species=%s | regions=%s | locations=%s",
                self.item_count,
                page,
                external_id,
                title,
                diseases,
                species,
                regions,
                locations,
            )

            yield {
                "model": "core.alert",
                "pk": self.item_count,
                "fields": {
                    "external_id": external_id,
                    "date": date,
                    "title": title,
                    "diseases": diseases,
                    "species": species,
                    "regions": regions,
                    "locations": locations,
                },
            }

        if self.should_continue(page, hits):
            yield self.make_request(page + 1)