"""Получение исходной выдачи."""

import time
import requests
from urllib.parse import parse_qs, urlsplit
from playwright.sync_api import TimeoutError as BrowserTimeout
from .urls import search_url
from .parser import ParseError, parse
from .pagination import collect_desktop


class Capture:
    def __init__(self, playwright, config):
        self.p = playwright
        self.config = config
        self.browser = (playwright.chromium.launch(headless=True)
                        if config.get("service", "images") == "video" else None)

    def close(self):
        if self.browser:
            self.browser.close()

    def run(self, task):
        service = self.config.get("service", "images")
        url = search_url(task, self.config)
        profiles = {"ANDROID": "Pixel 7", "IPHONE": "iPhone 13"}
        desktop_viewport = ({"width": 1920, "height": 1080} if service == 'images'
                            else {"width": 1365, "height": 900})
        profile = dict(self.p.devices[profiles[task.device]]) if task.device in profiles else {"viewport": desktop_viewport}
        profile.pop("default_browser_type", None)
        profile.update(locale="ru-RU", timezone_id="Europe/Moscow")
        if service == "images":
            # Preserve the server document and its native application startup.
            # Serializing an initialized DOM breaks BEM routing on reopening.
            ua = profile.get("user_agent") or self.p.devices["Desktop Chrome"]["user_agent"]
            def fetch_page(page_url):
                response = requests.get(page_url, headers={"User-Agent": ua, "Accept-Language": "ru-RU,ru;q=0.9"},
                                        timeout=(15, int(self.config.get("timeout_seconds", 60))))
                response.raise_for_status()
                requested = parse_qs(urlsplit(page_url).query, keep_blank_values=True)
                final = parse_qs(urlsplit(response.url).query, keep_blank_values=True)
                for key, values in requested.items():
                    if final.get(key) != values:
                        raise ValueError(f'Redirect dropped or changed requested parameter: {key}')
                page_soup, page_items = parse(response.text, service, response.url)
                return page_soup, page_items, response.url

            minimum = self.config.get('desktop_min_items', 90)
            max_pages = self.config.get('desktop_max_pages', 5)
            if task.device == 'DESKTOP':
                if type(minimum) is not int or minimum <= 0 or type(max_pages) is not int or max_pages <= 0:
                    raise ValueError('desktop_min_items and desktop_max_pages must be positive integers')
            soup, items, captured_url = fetch_page(url)
            info = {"requested_url": url, "captured_url": captured_url,
                    "viewport": profile["viewport"], "mode": "original-html"}
            if task.device == 'DESKTOP':
                soup, items, pagination = collect_desktop(soup, items, captured_url, fetch_page, minimum, max_pages)
                info.update(pagination)
            return soup, items, info
        context = self.browser.new_context(**profile)
        page = context.new_page()
        timeout = int(self.config.get("timeout_seconds", 60))
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            if response is None or response.status >= 400:
                raise ValueError(f"Search returned HTTP {response.status if response else 'no response'}")
            selector = ".SerpItem a img, .serp-item a img" if service == "images" else ".VideoSnippet .VideoSnippet-Title"
            try:
                page.wait_for_selector(selector, state="attached", timeout=timeout * 1000)
            except BrowserTimeout:
                raise ParseError("No rendered result cards. CAPTCHA, empty result or unsupported layout; no page published") from None
            # Wait until layout and structured data agree, rather than a fixed screenshot delay.
            deadline, last_error = time.monotonic() + timeout, None
            while time.monotonic() < deadline:
                try:
                    html = page.content()
                    soup, items = parse(html, service, page.url)
                    if service == "images" and len(soup.select(".SerpItem, .serp-item")) < len(items):
                        raise ParseError("Result cards still rendering")
                    break
                except ParseError as error:
                    last_error = error
                    page.wait_for_timeout(250)
            else:
                raise last_error or ParseError("Result hydration timed out")
            page.wait_for_timeout(int(self.config.get("settle_ms", 800)))
            html = page.content()
            soup, items = parse(html, service, page.url)
            final_params = parse_qs(urlsplit(page.url).query)
            if final_params.get("text", [""])[0] != task.query or final_params.get("lr", [""])[0] != str(task.region_id):
                raise ValueError("Redirect changed query or region_id")
            ids = self.config.get("test_ids", [])
            if ids and final_params.get(self.config.get("test_id_param", "test-id"), [""])[0] != ",".join(map(str, ids)):
                raise ValueError("Redirect dropped or changed requested test IDs")
            return soup, items, {"requested_url": url, "captured_url": page.url,
                                 "viewport": profile["viewport"], "browser": "chromium"}
        finally:
            context.close()
