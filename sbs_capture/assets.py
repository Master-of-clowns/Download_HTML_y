"""Загрузка ресурсов видео и обработка CSS."""

import base64
import re
import requests
from urllib.parse import urljoin


class Assets:
    def __init__(self):
        self.cache = {}

    def fetch(self, url):
        if url in self.cache:
            return self.cache[url]
        with requests.get(url, timeout=(10, 25), stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 12 * 1024 * 1024:
                    raise ValueError("Asset exceeds 12 MiB")
                chunks.append(chunk)
            data = b"".join(chunks)
            mime = response.headers.get("Content-Type", "").split(";")[0]
        self.cache[url] = (data, mime)
        return data, mime

    def image(self, url):
        data, mime = self.fetch(url)
        if not mime.startswith("image/") or mime == "image/svg+xml":
            raise ValueError("Expected a raster image")
        return "data:" + mime + ";base64," + base64.b64encode(data).decode()

    def css(self, url):
        data, mime = self.fetch(url)
        if mime not in {"text/css", "text/plain", "application/octet-stream"}:
            raise ValueError("Expected CSS")
        text = data.decode("utf-8")
        return absolute_css(text, url)


def absolute_css(text, base):
    def replace(m):
        value = m.group(1).strip(" \t\r\n\"'")
        if value.startswith(("data:", "#")):
            return m.group(0)
        return 'url("' + urljoin(base, value).replace('"', '%22') + '")'
    # Remove source blur before embedding, including inline !important rules
    # and CSS variables that might otherwise beat the mandatory override.
    text = re.sub(r"\bblur\([^()]*\)", "blur(0)", text, flags=re.IGNORECASE)
    return re.sub(r"url\(([^)]+)\)", replace, text)
