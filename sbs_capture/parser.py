"""Разбор HTML выдачи картинок и видео."""

import json
from bs4 import BeautifulSoup
from .models import Item
from .urls import http_url


class ParseError(ValueError):
    pass


def states(soup):
    for node in soup.select("[data-state], [data-bem]"):
        for attr in ("data-state", "data-bem"):
            try:
                yield json.loads(node.get(attr, ""))
            except (ValueError, TypeError):
                pass


def image_item(raw, position, base):
    viewer = raw.get("viewerData", {})
    snippet = raw.get("snippet") or viewer.get("snippet") or {}
    previews = viewer.get("preview") or []
    page = snippet.get("url") or raw.get("page_url")
    if not page:
        page = next((p.get("siteHref") for p in previews if p.get("siteHref")), "")
    original = raw.get("origUrl") or raw.get("image_url")
    thumb = raw.get("image") or raw.get("thumb", {}).get("url") or viewer.get("thumb", {}).get("url")
    original = original or next((p.get("url") for p in previews if p.get("url")), thumb)
    return Item(str(raw.get("id", position)), position,
                str(snippet.get("title") or raw.get("alt") or ""),
                http_url(thumb, base), http_url(original, base), http_url(page, base),
                bool(raw.get("censored") or raw.get("censoredShock") or
                     viewer.get("censored") or viewer.get("censoredShock")))


def parse_images(soup, base):
    for state in states(soup):
        items = state.get("initialState", {}).get("serpList", {}).get("items", {})
        entities = items.get("entities", {})
        keys = items.get("keys", list(entities))
        if entities:
            return [image_item(entities[k], i, base) for i, k in enumerate(keys) if k in entities]
    # Legacy BEM layout. Keep DOM order, including repeated images.
    found = []
    for node in soup.select("[data-bem]"):
        try:
            raw = json.loads(node["data-bem"]).get("serp-item")
        except ValueError:
            continue
        if raw:
            found.append(image_item(raw, len(found), base))
    return found


def parse(html, service, base):
    soup = BeautifulSoup(html, "html.parser")
    if service == "images":
        items = parse_images(soup, base)
    else:
        items = parse_videos(soup, base)
    if not items:
        raise ParseError("No result records found. Check for CAPTCHA, empty results or changed markup; no S3 page published.")
    missing = [x.position for x in items if not x.page_url or not x.thumbnail_url]
    if missing:
        raise ParseError(f"Missing source URL or thumbnail at positions {missing[:10]}; cannot publish a misleading snapshot")
    return soup, items


def parse_videos(soup, base):
    # DOM is authoritative after hydration. The initial data-state can be empty.
    runtime_node = soup.select_one("#UniAppVideo-PreloadedState")
    try:
        runtime_state = json.loads(runtime_node.get_text()) if runtime_node else {}
    except ValueError:
        runtime_state = {}
    clips = runtime_state.get("clips", {}).get("items", {})
    dups = runtime_state.get("clips", {}).get("dups", {})
    dom_items = []
    for card in soup.select(".VideoSnippet"):
        title = card.select_one(".VideoSnippet-Title")
        host = card.select_one(".VideoHostExtended-Host[href]")
        poster = card.select_one("video[poster], img[src]")
        if not title or not poster:
            raise ParseError("Video card lacks title or poster")
        import re
        match = re.search(r"/preview/([^?/#]+)", title.get("href", ""))
        raw = (dups.get(match.group(1)) or clips.get(match.group(1), {})) if match else {}
        parent = clips.get(raw.get("parentClipId"), raw)
        page_url = host.get("href") if host else raw.get("host", {}).get("href") or raw.get("url")
        if not page_url:
            raise ParseError("Video card lacks a source link in DOM and structured data")
        dom_items.append(Item(match.group(1) if match else str(len(dom_items)), len(dom_items),
                              title.get_text(" ", strip=True),
                              http_url(poster.get("poster") or poster.get("src"), base),
                              "", http_url(page_url, base), bool(parent.get("censored") or parent.get("censoredShock"))))
    if dom_items:
        return dom_items
    result = []
    candidates = [x.get("preloadedState", {}) for x in states(soup)]
    runtime = soup.select_one("#UniAppVideo-PreloadedState")
    if runtime:
        try:
            candidates.insert(0, json.loads(runtime.get_text()))
        except ValueError:
            pass
    for state in candidates:
        clips = state.get("clips", {}).get("items", {})
        for entry in state.get("pages", {}).get("search", {}).get("serpItems", []):
            if entry.get("type") == "videoSnippet":
                raw = clips.get(entry.get("props", {}).get("videoId"))
                if raw:
                    result.append(raw)
        if result:
            break
    return normalize_videos(result, base)


def normalize_videos(records, base):
    items = []
    for i, raw in enumerate(records):
        video = raw.get("video", raw)
        url = video.get("host", {}).get("href") or video.get("url") or video.get("page_url")
        thumb = video.get("thumb") or video.get("thumbnail") or video.get("preview")
        if isinstance(thumb, dict):
            thumb = thumb.get("url") or thumb.get("posterSrc")
        items.append(Item(str(video.get("videoId") or video.get("id", i)), i, str(video.get("cleanTitle") or video.get("title", "")),
                          http_url(thumb, base), "", http_url(url, base)))
    return items
