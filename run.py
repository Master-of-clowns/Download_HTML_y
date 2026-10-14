"""python3 run.py input.json [result.json]"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
from uuid import uuid4

# Automatically use the environment installed beside this script (macOS/Linux/Windows).
HOME_DIR = Path(__file__).resolve().parent
VENV = HOME_DIR / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if (
    __name__ == "__main__"
    and PYTHON.is_file()
    and Path(sys.prefix).resolve() != VENV.resolve()
):
    os.execv(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

try:
    import boto3
    import httpx
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from dotenv import load_dotenv
    from playwright.sync_api import sync_playwright, Error as BrowserError
except ImportError:
    sys.exit("Установите зависимости: python3 -m pip install -r requirements.txt")

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(HOME_DIR / ".browsers"))

UA = {
    "desktop": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "mobile": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "tablet": "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


class ParserError(Exception):
    pass


class DocumentBase(HTMLParser):
    """Locate real HTML tags without changing scripts, comments or original encoding."""

    def __init__(self, body):
        super().__init__()
        text = body.decode("latin-1")  # One character per byte for insertion offsets.
        self.lines = [0] + [i + 1 for i, char in enumerate(text) if char == "\n"]
        self.head, self.html, self.doctype, self.base = None, 0, 0, None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        line, column = self.getpos()
        end = self.lines[line - 1] + column + len(self.get_starttag_text())
        if tag == "head" and self.head is None:
            self.head = end
        elif tag == "html" and not self.html:
            self.html = end
        elif tag == "base" and self.base is None:
            self.base = dict(attrs).get("href")

    def handle_decl(self, declaration):
        if declaration.lower().startswith("doctype"):
            line, column = self.getpos()
            self.doctype = self.lines[line - 1] + column + len(declaration) + 3


def prepare_html(body, source_url):
    parsed = DocumentBase(body)
    url = urljoin(source_url, parsed.base) if parsed.base is not None else source_url
    tag = f'<base href="{escape(url, quote=True)}">'.encode("utf-8")
    position = parsed.head if parsed.head is not None else parsed.html or parsed.doctype
    if parsed.head is None:
        tag = b"<head>" + tag + b"</head>"
    if body[position : position + len(tag)] == tag:
        return body
    return body[:position] + tag + body[position:]


def read_input(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, (dict, list)):
        raise ParserError("Нужен JSON-объект или список объектов.")
    rows = data if isinstance(data, list) else [data]
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ParserError(f"Элемент {index}: требуется объект.")
        invalid = []
        query = row.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            invalid.append("query — непустая строка до 2000 символов")
        if type(row.get("region_id")) is not int or row["region_id"] <= 0:
            invalid.append("region_id — положительное целое число")
        if not isinstance(row.get("device"), str) or row["device"] not in UA:
            invalid.append("device — desktop/mobile/tablet")
        if invalid:
            raise ParserError(f"Элемент {index}: " + "; ".join(invalid))
    return rows, isinstance(data, list)


def settings():
    cfg = {
        key: os.getenv(key, default)
        for key, default in {
            "S3_BUCKET": "",
            "S3_ENDPOINT_URL": "https://s3.twcstorage.ru",
            "AWS_DEFAULT_REGION": "ru-1",
            "S3_PREFIX": "yandex-video",
            "S3_URL_MODE": "presigned",
            "S3_PUBLIC_BASE_URL": "",
            "YANDEX_PROXY_URL": "",
        }.items()
    }
    if not cfg["S3_BUCKET"] or "/" in cfg["S3_BUCKET"]:
        raise ParserError("Укажите S3_BUCKET в .env или окружении.")
    if cfg["S3_URL_MODE"] not in {"presigned", "public"}:
        raise ParserError("S3_URL_MODE: presigned или public.")
    for name in ("S3_ENDPOINT_URL", "S3_PUBLIC_BASE_URL"):
        url = urlsplit(cfg[name])
        if cfg[name] and (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ParserError(f"{name}: нужен HTTPS-адрес без ключей и параметров.")
    for name, default in {
        "S3_URL_TTL_SECONDS": 86400,
        "FETCH_TIMEOUT_SECONDS": 30,
        "FETCH_ATTEMPTS": 3,
        "MAX_HTML_BYTES": 20971520,
        "RENDER_TIMEOUT_SECONDS": 60,
    }.items():
        try:
            cfg[name] = int(os.getenv(name, str(default)))
        except ValueError:
            raise ParserError(f"{name}: требуется целое число.") from None
        if cfg[name] <= 0 or (name == "S3_URL_TTL_SECONDS" and cfg[name] > 604800):
            raise ParserError(f"Недопустимое значение {name}.")
    if cfg["YANDEX_PROXY_URL"] and urlsplit(cfg["YANDEX_PROXY_URL"]).scheme not in {
        "http",
        "https",
    }:
        raise ParserError("YANDEX_PROXY_URL: нужен HTTP/HTTPS-прокси.")
    return cfg


def fetch_html(row, client, cfg, can_configure=True):
    for attempt in range(cfg["FETCH_ATTEMPTS"]):
        try:
            with client.stream(
                "GET",
                "https://yandex.ru/video/search",
                params={
                    "text": row["query"],
                    "lr": row["region_id"],
                    "show_sensitive": "on",
                },
                headers={
                    "User-Agent": UA[row["device"]],
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Accept": "text/html",
                },
            ) as response:
                if response.status_code >= 500 and attempt + 1 < cfg["FETCH_ATTEMPTS"]:
                    time.sleep(2**attempt)
                    continue
                if response.status_code != 200:
                    raise ParserError(
                        f"Яндекс вернул HTTP {response.status_code}; HTML не сохранён."
                    )
                parts, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > cfg["MAX_HTML_BYTES"]:
                        raise ParserError("HTML превышает MAX_HTML_BYTES.")
                    parts.append(chunk)
                body = b"".join(parts)
                text = body.decode("utf-8", errors="replace")
                title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
                title = unescape(title[1]).casefold() if title else ""
                if (
                    "captcha" in response.url.path.casefold()
                    or any(
                        word in title
                        for word in (
                            "captcha",
                            "ой!",
                            "oh!",
                            "доступ ограничен",
                            "подтвердите",
                        )
                    )
                    or re.search(
                        r"<form\b[^>]*action\s*=\s*[\"\'][^\"\']*(?:checkcaptcha|showcaptcha)|\bid\s*=\s*[\"\'](?:js-)?(?:smart|checkbox)-?captcha",
                        text,
                        re.I,
                    )
                ):
                    raise ParserError(
                        "Яндекс показал капчу или ограничил доступ; HTML не сохранён."
                    )
                if (
                    response.url.host not in {"yandex.ru", "www.yandex.ru", "ya.ru"}
                    or response.url.path.rstrip("/")
                    not in {"/video/search", "/video/touch/search"}
                    or response.url.params.get("lr") != str(row["region_id"])
                ):
                    raise ParserError(
                        "Яндекс перенаправил на другую страницу или потерял регион."
                    )
                types = response.headers.get_list("content-type", split_commas=True)
                content_type = types[0] if types else ""
                if (
                    "text/html" not in content_type.lower()
                    or not re.search(rb"<(?:html|!doctype\s+html)\b", body, re.I)
                    or not re.search(
                        r"video-serp|VideoSearch|/video/(?:touch/)?search", text
                    )
                ):
                    raise ParserError("Ответ Яндекса не похож на HTML выдачи видео.")
                state = unescape(text).replace('\\"', '"')
                flags = re.findall(r'"showSensitive"\s*:\s*(true|false)', state)
                # The last value is hydrated state; initial markup can contain default false.
                if not flags or flags[-1] != "true":
                    if not flags or not can_configure:
                        raise ParserError(
                            "Не удалось подтвердить отключение размытия 18+; HTML не сохранён."
                        )
                    key = re.search(r'"sk"\s*:\s*"([^"\s]+)"', state)
                    if not key:
                        raise ParserError(
                            "Яндекс не предоставил ключ настройки размытия 18+."
                        )
                    setting = client.get(
                        "https://yandex.ru/portal/set/tune",
                        follow_redirects=False,
                        params={
                            "sk": key[1],
                            "fields": "show_sensitive",
                            "show_sensitive": "on",
                            "retpath": str(response.url),
                        },
                        headers={"User-Agent": UA[row["device"]]},
                    )
                    if setting.status_code not in {200, 204, 302, 303}:
                        raise ParserError(
                            "Яндекс не принял настройку отключения размытия 18+."
                        )
                    return fetch_html(row, client, cfg, can_configure=False)
                return (
                    prepare_html(body, str(response.url)),
                    str(response.url),
                    content_type,
                )
        except httpx.RequestError:
            if attempt + 1 == cfg["FETCH_ATTEMPTS"]:
                raise ParserError(
                    "Не удалось скачать HTML: проверьте сеть или прокси."
                ) from None
            time.sleep(2**attempt)


def launch_browser(playwright):
    try:
        return playwright.chromium.launch(headless=True, channel="chromium")
    except BrowserError as exc:
        if "Executable doesn't exist" not in str(exc):
            raise ParserError("Не удалось запустить Chromium.") from None
        print("Первый запуск: устанавливаю Chromium…", file=sys.stderr, flush=True)
        installed = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--no-shell"],
            capture_output=True,
        )
        if installed.returncode:
            raise ParserError("Не удалось установить Chromium: проверьте сеть.")
        return playwright.chromium.launch(headless=True, channel="chromium")


def standalone_html(snapshot):
    """Convert Chromium's rendered archive into one HTML with embedded assets."""
    message = BytesParser(policy=policy.default).parsebytes(snapshot.encode("utf-8"))
    parts = list(message.iter_parts())
    if not parts or parts[0].get_content_type() != "text/html":
        raise ParserError("Браузер не предоставил HTML снимка.")
    resources, cached, active = {}, {}, set()
    for part in parts[1:]:
        if part.get("Content-Location"):
            location = part["Content-Location"]
            resources[location] = part
            if location.startswith(("https://", "http://")):
                resources[location[location.index(":") + 1:]] = part
        if part.get("Content-ID"):
            resources["cid:" + part["Content-ID"].strip("<>")] = part
    pattern = re.compile("|".join(re.escape(url) for url in sorted(resources, key=len, reverse=True))) if resources else None

    def replace(text):
        return pattern.sub(lambda match: embed(match[0]), text) if pattern else text

    def embed(url):
        if url in cached:
            return cached[url]
        if url in active:
            raise ParserError("Циклическая ссылка в ресурсах страницы.")
        active.add(url)
        part = resources[url]
        body = part.get_payload(decode=True)
        mime = part.get_content_type()
        if mime in {"text/css", "text/html"}:
            body = replace(body.decode(part.get_content_charset() or "utf-8")).encode("utf-8")
            mime += ";charset=utf-8"
        cached[url] = "data:" + mime + ";base64," + base64.b64encode(body).decode("ascii")
        active.remove(url)
        return cached[url]

    html = parts[0].get_payload(decode=True).decode(parts[0].get_content_charset() or "utf-8")
    # HTML attributes escape ampersands in image URLs; CSS uses the raw URLs.
    for url, part in list(resources.items()):
        escaped = escape(url, quote=True)
        if escaped != url:
            resources[escaped] = part
    if resources:
        pattern = re.compile("|".join(re.escape(url) for url in sorted(resources, key=len, reverse=True)))
    html = replace(html)
    html = html.replace("</head>", "<style>.SettingsToolbarLangFilterDistributionOnboarding{display:none!important}</style></head>", 1)
    return html.encode("utf-8")


def verify_snapshot(body, browser, width, height, expected_cards):
    with browser.new_context(offline=True, viewport={"width": width, "height": height},
                             is_mobile=width != 1440, has_touch=width != 1440) as saved:
        page = saved.new_page()
        page.set_content(body.decode("utf-8"), wait_until="load", timeout=30000)
        if (page.locator(".VideoSnippet:visible").count() != expected_cards
                or page.locator(".Skeleton-Snippet:visible, script").count()):
            raise ParserError("Сохранённый HTML содержит незагруженные карточки.")
        loaded = page.evaluate("""async () => {
            const thumbs = [...document.querySelectorAll('.VideoThumb3')].filter(t => t.getBoundingClientRect().width > 0 && t.getBoundingClientRect().height > 0);
            const checks = thumbs.map(thumb => {
                const urls = [...thumb.querySelectorAll('img')].map(i => i.currentSrc || i.src);
                urls.push(...[...thumb.querySelectorAll('video[poster]')].map(v => v.poster));
                for (const node of [thumb, ...thumb.querySelectorAll('*')]) {
                    urls.push(...[...getComputedStyle(node).backgroundImage.matchAll(/url\\(["']?([^"')]+)["']?\\)/g)].map(m => m[1]));
                }
                const unique = [...new Set(urls.filter(Boolean))];
                if (!unique.length) return Promise.resolve(false);
                return Promise.all(unique.map(url => new Promise(resolve => {
                    const image = new Image();
                    const timer = setTimeout(() => resolve(false), 10000);
                    image.onload = () => {clearTimeout(timer); resolve(image.naturalWidth > 0)};
                    image.onerror = () => {clearTimeout(timer); resolve(false)};
                    image.src = url;
                }))).then(results => results.every(Boolean));
            });
            return (await Promise.all(checks)).every(Boolean);
        }""")
        if not loaded:
            raise ParserError("Не все превью сохранены в HTML; повторите скачивание.")


def render_html(row, client, cfg, browser):
    _, source, _ = fetch_html(row, client, cfg)
    device = row["device"]
    width, height = {"desktop": (1440, 1000), "mobile": (390, 844), "tablet": (1024, 1366)}[device]
    proxy = {}
    if cfg.get("YANDEX_PROXY_URL"):
        url = urlsplit(cfg["YANDEX_PROXY_URL"])
        server = url._replace(netloc=url.netloc.rsplit("@", 1)[-1]).geturl()
        proxy = {"proxy": {"server": server}}
        if url.username:
            proxy["proxy"].update(username=unquote(url.username), password=unquote(url.password or ""))
    with browser.new_context(
        user_agent=UA[device], viewport={"width": width, "height": height},
        is_mobile=device != "desktop", has_touch=device != "desktop", locale="ru-RU",
        **proxy,
    ) as context:
        context.add_cookies([
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": c.secure}
            for c in client.cookies.jar
        ])
        page = context.new_page()
        pending = set()
        page.on("request", lambda r: pending.add(r) if r.resource_type in {"image", "stylesheet", "font"} else None)
        page.on("requestfinished", lambda r: pending.discard(r))
        page.on("requestfailed", lambda r: pending.discard(r))
        timeout = cfg["RENDER_TIMEOUT_SECONDS"] * 1000
        try:
            response = page.goto(source, wait_until="domcontentloaded", timeout=timeout)
            current = httpx.URL(page.url)
            if (not response or response.status != 200 or current.host not in {"yandex.ru", "www.yandex.ru", "ya.ru"}
                    or current.path.rstrip("/") not in {"/video/search", "/video/touch/search"}
                    or current.params.get("lr") != str(row["region_id"])):
                raise ParserError("Браузер получил капчу, ошибку или потерял регион.")
            page.wait_for_function("""() => (document.querySelectorAll('.VideoSnippet').length > 0
                || document.title === 'Видео по вашему запросу не найдено')
                && !document.querySelector('.Skeleton-Snippet')""", timeout=timeout)
            previous, stable = 0, 0
            deadline = time.monotonic() + cfg["RENDER_TIMEOUT_SECONDS"]
            while time.monotonic() < deadline:
                count = page.locator(".VideoSnippet").count()
                stable = stable + 1 if count == previous and not pending else 0
                if stable >= 3:
                    break
                previous = count
                page.wait_for_timeout(500)
            else:
                raise ParserError("Карточки или ресурсы страницы не завершили загрузку.")
            # Visit every card on this page, without requesting another result page.
            for card in page.locator(".VideoSnippet:visible").all():
                card.scroll_into_view_if_needed(timeout=timeout)
                page.wait_for_timeout(80)
            page.wait_for_function("""() => [...document.querySelectorAll('.VideoSnippet img')].every(i => !i.getBoundingClientRect().width || (i.complete && i.naturalWidth > 0))
                && !document.querySelector('.Skeleton-Snippet')""", timeout=timeout)
            deadline = time.monotonic() + cfg["RENDER_TIMEOUT_SECONDS"]
            while pending:
                if time.monotonic() > deadline:
                    raise ParserError("Не все изображения и стили загрузились.")
                page.wait_for_timeout(100)
            flags = re.findall(r'"showSensitive"\s*:\s*(true|false)', unescape(page.content()).replace('\\"', '"'))
            if not flags or flags[-1] != "true":
                raise ParserError("Браузер не подтвердил отключение размытия 18+.")
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(300)
            archive = context.new_cdp_session(page).send("Page.captureSnapshot", {"format": "mhtml"})["data"]
            body = prepare_html(standalone_html(archive), page.url)
            if len(body) > cfg["MAX_HTML_BYTES"]:
                raise ParserError("Готовый HTML превышает MAX_HTML_BYTES.")
            verify_snapshot(body, browser, width, height, page.locator(".VideoSnippet:visible").count())
            return body, page.url, "text/html; charset=utf-8"
        except BrowserError:
            raise ParserError("Страница не завершила загрузку: проверьте сеть, прокси или капчу.") from None


def upload(row, snapshot, s3, cfg):
    body, source, content_type = snapshot
    digest = hashlib.sha256(
        json.dumps(
            [row["query"], row["region_id"], row["device"]], ensure_ascii=False
        ).encode()
    ).hexdigest()[:20]
    key = "/".join(
        filter(
            None,
            [
                cfg["S3_PREFIX"].strip("/"),
                datetime.now(timezone.utc).strftime("%Y/%m/%d"),
                f"{digest}-{uuid4().hex}.html",
            ],
        )
    )
    location = {"Bucket": cfg["S3_BUCKET"], "Key": key}
    if cfg["S3_URL_MODE"] == "presigned":
        url = s3.generate_presigned_url(
            "get_object", Params=location, ExpiresIn=cfg["S3_URL_TTL_SECONDS"]
        )
    else:
        base = cfg["S3_PUBLIC_BASE_URL"].rstrip("/") or cfg["S3_ENDPOINT_URL"].rstrip(
            "/"
        ) + "/" + quote(cfg["S3_BUCKET"], safe="")
        url = base + "/" + quote(key, safe="/")
    s3.put_object(
        **location,
        Body=body,
        ContentType=content_type,
        ContentDisposition="inline",
        Metadata={
            "source-url": source,
            "region-id": str(row["region_id"]),
            "device": row["device"],
        },
    )
    return {**row, "url": url}


def write_output(path, result):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Скачать HTML Яндекс Видео в S3")
    parser.add_argument("input", type=Path, help="Путь к входному JSON")
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=Path("result.json"),
        help="Результат (по умолчанию result.json)",
    )
    args = parser.parse_args(argv)
    source, target = args.input.expanduser(), args.output.expanduser()
    try:
        load_dotenv(HOME_DIR / ".env", override=False)
        if source.resolve() == target.resolve():
            raise ParserError("Входной и выходной файлы должны быть разными.")
        rows, is_batch = read_input(source)
        results = []
        if rows:
            cfg = settings()
            s3 = boto3.client(
                "s3",
                endpoint_url=cfg["S3_ENDPOINT_URL"],
                region_name=cfg["AWS_DEFAULT_REGION"],
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "path"},
                    connect_timeout=10,
                    read_timeout=30,
                    retries={"mode": "standard", "total_max_attempts": 3},
                ),
            )
            with sync_playwright() as playwright, httpx.Client(
                timeout=cfg["FETCH_TIMEOUT_SECONDS"],
                follow_redirects=True,
                proxy=cfg["YANDEX_PROXY_URL"] or None,
                trust_env=False,
            ) as client:
                browser = launch_browser(playwright)
                try:
                    for index, row in enumerate(rows, 1):
                        print(f"Обрабатываю {index}/{len(rows)}…", file=sys.stderr, flush=True)
                        results.append(upload(row, render_html(row, client, cfg, browser), s3, cfg))
                finally:
                    browser.close()
        write_output(target, results if is_batch else results[0])
    except json.JSONDecodeError as exc:
        print(
            f"Ошибка JSON: строка {exc.lineno}, столбец {exc.colno}.", file=sys.stderr
        )
        return 1
    except (BotoCoreError, ClientError):
        print("Ошибка S3: проверьте имя бакета, endpoint и AWS-ключи.", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Ошибка файла: проверьте путь, права и кодировку UTF-8.", file=sys.stderr)
        return 1
    except (ValueError, httpx.InvalidURL):
        print("Ошибка настройки: проверьте адреса S3 и прокси.", file=sys.stderr)
        return 1
    except BrowserError:
        print("Ошибка браузера: проверьте установку Chromium и доступ к сети.", file=sys.stderr)
        return 1
    except ParserError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Готово: {target.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
