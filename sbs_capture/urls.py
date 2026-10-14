"""Адреса поиска и источников."""

from urllib.parse import urlsplit, urlunsplit, urlencode
from .models import Query


def http_url(value, base="https://yandex.ru"):
    from urllib.parse import urljoin
    if not isinstance(value, str) or not value.strip():
        return ""
    value = urljoin(base, value)
    p = urlsplit(value)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        return ""
    return value


def search_url(task, config):
    service = config.get("service", "images")
    if service not in {"images", "video"}:
        raise ValueError("service must be images or video")
    host = config.get("host", "https://yandex.ru")
    p = urlsplit(host)
    if not http_url(host) or p.path not in {"", "/"} or p.query or p.fragment:
        raise ValueError("host must be an HTTP(S) origin, without path or parameters")
    mobile = task.device != "DESKTOP"
    path = config.get("mobile_path" if mobile else "desktop_path")
    path = path or f"/{service}/{'touch/' if mobile else ''}search"
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("Search path must be relative to host")
    q = dict(config.get("cgi", {}))
    if {"text", "lr"} & q.keys():
        raise ValueError("cgi cannot override query/region_id: use the input JSON")
    q.update(text=task.query, lr=str(task.region_id))
    ids = config.get("test_ids", [])
    if ids:
        q[config.get("test_id_param", "test-id")] = ",".join(map(str, ids))
    return urlunsplit((p.scheme, p.netloc, path, urlencode(q, doseq=True), ""))
