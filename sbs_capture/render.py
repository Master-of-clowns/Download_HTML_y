"""Сохранение HTML и обязательное отключение блюра."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin
from .urls import http_url
from .parser import ParseError
from .assets import absolute_css
from .templates import NATIVE_CSS, NATIVE_JS, VIDEO_CSS


def clear_blur_flags(value):
    """Disable image censorship in every saved native state, without a toggle."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"censored", "censoredShock", "censored_shock", "isCensored",
                       "blurred", "isBlurred"}:
                value[key] = False
            elif key == "images_shock_blur":
                value[key] = "0" if isinstance(child, str) else 0
            else:
                clear_blur_flags(child)
    elif isinstance(value, list):
        for child in value:
            clear_blur_flags(child)


def render(soup, items, task, service, base, assets):
    if not soup.html or not soup.head or not soup.body:
        raise ParseError("Source is not a full HTML document")
    if service == "images":
        return render_native(soup, items, task, base, assets)
    bound = []
    cards = soup.select(".VideoSnippet")
    if len(cards) != len(items):
        raise ParseError("Video record/card count differs")
    for card, item in zip(cards, items):
        links = card.select(".VideoSnippet-Thumb, .VideoSnippet-Title, .VideoHostExtended-Host")
        if len(links) < 2:
            raise ParseError("Video card lacks clickable title and thumbnail")
        for a in links:
            a.name = "a"
            a["href"] = item.page_url
            a["target"] = "_blank"
            a["rel"] = "noopener noreferrer"
            a["tabindex"] = "0"
            a.attrs.pop("aria-hidden", None)
            a["data-sbs-video"] = str(item.position)
        poster = card.select_one("video, img")
        img = soup.new_tag("img", src=item.thumbnail_url, alt=item.title)
        img["class"] = poster.get("class", [])
        img["style"] = "width:100%;height:100%;object-fit:cover"
        poster.replace_with(img)
        bound.extend(links)

    # Remove original app runtime and tracking: video links point directly to their source.
    for node in soup.select("script, iframe, object, embed, noframes, base, link[rel=preload], link[rel=modulepreload], link[rel=prefetch], .YndxBug, #yndxbug, .TechTool, .DevToolsPanel, .MessageBox_layout_tooltip, [role=tooltip]"):
        node.decompose()
    for meta in soup.select("meta[http-equiv]"):
        meta.decompose()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on") or (attr.startswith("data-") and not attr.startswith("data-sbs-")) or attr in {"nonce", "integrity", "srcset", "ping"}:
                del node[attr]
        if node.get("style"):
            node["style"] = absolute_css(node["style"], base)
        for attr in ("href", "src", "action", "poster"):
            value = node.get(attr)
            if not value or value.startswith(("#", "data:image/")):
                continue
            safe = http_url(value, base)
            if safe:
                node[attr] = safe
            else:
                node.attrs.pop(attr, None)
        if node.name == "a" and node.get("href", "").startswith(("https:", "http:")):
            node["target"], node["rel"] = "_blank", "noopener noreferrer"
    for style in soup.select("style"):
        style.string = absolute_css(style.get_text(), base)
    links = soup.select('link[rel="stylesheet"][href]')
    with ThreadPoolExecutor(max_workers=5) as pool:
        css = list(pool.map(assets.css, [x["href"] for x in links]))
    for link, text in zip(links, css):
        style = soup.new_tag("style")
        style.string = text
        link.replace_with(style)

    # Cache the displayed images in the HTML: no expiring thumbnail links on S3.
    images = [img for img in soup.select("img[src]") if not img["src"].startswith("data:")]
    urls = list(dict.fromkeys(img["src"] for img in images))
    with ThreadPoolExecutor(max_workers=6) as pool:
        encoded = dict(zip(urls, pool.map(assets.image, urls)))
    for img in images:
        img["src"] = encoded[img["src"]]

    soup.html['data-sbs-unblur'] = 'true'
    own_style = soup.new_tag("style")
    own_style.string = VIDEO_CSS
    soup.head.append(own_style)
    banner = soup.new_tag("div", id="sbs-banner")
    banner.string = f"Сохранённая выдача · {task.query} · регион {task.region_id} · {task.device}"
    soup.body.insert(0, banner)
    referrer = soup.new_tag("meta", attrs={"name": "referrer", "content": "no-referrer"})
    soup.head.insert(0, referrer)
    csp = ("default-src 'none'; img-src data: https: http:; style-src 'unsafe-inline' https:; "
           "script-src 'none'; font-src data: https:; connect-src 'none'; "
           "base-uri 'none'; form-action 'none'; object-src 'none'")
    soup.head.insert(0, soup.new_tag("meta", attrs={"http-equiv": "Content-Security-Policy", "content": csp}))
    return str(soup), {"items": len(items), "source_links": len(items), "bound_links": len(bound),
                       "embedded_images": len(images), "embedded_stylesheets": len(links),
                       "censored_items": sum(x.censored for x in items),
                       "source_blur_preserved": False, "remove_blur": True}


def render_native(soup, items, task, base, assets):

    root = soup.select_one('[id^="ImagesApp"][data-state]')
    if root is None or not soup.select('script[data-as="webpack-runtime"][src]'):
        raise ParseError("Native image viewer runtime is missing; capture the original HTML again")
    state = json.loads(root['data-state'])
    records = state['initialState']['serpList']['items']['entities']
    # Match yandex old: keep native thumbnail and original URLs. The browser
    # loads them when displaying the saved page; export never fetches them.
    for item in items:
        record = records[item.id]
        viewer = record.get('viewerData', {})
        record['censored'] = False
        viewer['censored'] = False
    clear_blur_flags(state)
    # Source JSON may contain an unpaired UTF-16 surrogate. Keep it escaped
    # so the HTML remains valid UTF-8 and JSON.parse receives the same value.
    root['data-state'] = json.dumps(state, ensure_ascii=True, separators=(',', ':'))

    # Do not strip application state, native scripts, event handlers or image
    # links: these are required for the production viewer and its information.
    for node in soup.select('base, meta[http-equiv], iframe, #init-metrika, #aab-d'):
        node.decompose()
    for node in soup.find_all(True):
        # BEM nodes and other native roots can carry their own censor flags.
        for attr in ('data-state', 'data-bem'):
            if attr not in node.attrs:
                continue
            try:
                native_data = json.loads(node[attr])
            except (ValueError, TypeError):
                continue
            clear_blur_flags(native_data)
            node[attr] = json.dumps(native_data, ensure_ascii=True, separators=(',', ':'))
        if node.get('class'):
            node['class'] = [c for c in node['class'] if not c.endswith('_js_inited')]
        for attr in ('src', 'href', 'action', 'poster'):
            value = node.get(attr)
            if value and not value.startswith(('data:', '#', 'javascript:')):
                node[attr] = urljoin(base, value)
        if node.get('style'):
            node['style'] = absolute_css(node['style'], base)
    for script in soup.select('script[src]'):
        if any(x in script['src'] for x in ('/ads/', 'mc.yandex.', '/ulitochka/', '/safeframe-')):
            script.decompose()
    for script in soup.select('script:not([src])'):
        if '/ulitochka/' in script.get_text() or 'butterfly/v1/' in script.get_text():
            script.decompose()
    for style in soup.select('style'):
        style.string = absolute_css(style.get_text(), base)
    links = soup.select('link[rel="stylesheet"][href]')
    # Keep stylesheet links just like the reference; no asset download here.

    soup.html['data-sbs-device'] = task.device
    soup.html['data-sbs-source'] = base
    soup.html['data-sbs-unblur'] = 'true'
    style = soup.new_tag('style', id='sbs-native-style')
    style.string = NATIVE_CSS
    soup.head.append(style)
    script = soup.new_tag('script', id='sbs-native-adapter')
    script.string = NATIVE_JS
    soup.head.insert(0, script)
    soup.head.insert(0, soup.new_tag('meta', attrs={'name': 'referrer', 'content': 'no-referrer'}))
    return str(soup), {'items': len(items), 'source_links': len(items),
                      'embedded_images': 0, 'embedded_stylesheets': 0,
                      'linked_images': len(items), 'linked_stylesheets': len(links),
                      'image_loading': 'browser',
                      'viewer': 'yandex-native', 'censored_items': sum(i.censored for i in items),
                      'source_blur_preserved': False, 'remove_blur': True}
