"""python3 run.py input.json [result.json]"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

# Automatically use the environment installed beside this script (macOS/Linux/Windows).
HOME_DIR = Path(__file__).resolve().parent
VENV = HOME_DIR / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
if __name__ == "__main__" and PYTHON.is_file() and Path(sys.prefix).resolve() != VENV.resolve():
    os.execv(str(PYTHON), [str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

try:
    import boto3
    import httpx
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Установите зависимости: python3 -m pip install -r requirements.txt")

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
    if body[position:position + len(tag)] == tag:
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
    cfg = {key: os.getenv(key, default) for key, default in {
        "S3_BUCKET": "", "S3_ENDPOINT_URL": "https://s3.twcstorage.ru",
        "AWS_DEFAULT_REGION": "ru-1", "S3_PREFIX": "yandex-video",
        "S3_URL_MODE": "presigned", "S3_PUBLIC_BASE_URL": "", "YANDEX_PROXY_URL": "",
    }.items()}
    if not cfg["S3_BUCKET"] or "/" in cfg["S3_BUCKET"]:
        raise ParserError("Укажите S3_BUCKET в .env или окружении.")
    if cfg["S3_URL_MODE"] not in {"presigned", "public"}:
        raise ParserError("S3_URL_MODE: presigned или public.")
    for name in ("S3_ENDPOINT_URL", "S3_PUBLIC_BASE_URL"):
        url = urlsplit(cfg[name])
        if cfg[name] and (url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment):
            raise ParserError(f"{name}: нужен HTTPS-адрес без ключей и параметров.")
    for name, default in {"S3_URL_TTL_SECONDS": 86400, "FETCH_TIMEOUT_SECONDS": 30,
                          "FETCH_ATTEMPTS": 3, "MAX_HTML_BYTES": 20971520}.items():
        try:
            cfg[name] = int(os.getenv(name, str(default)))
        except ValueError:
            raise ParserError(f"{name}: требуется целое число.") from None
        if cfg[name] <= 0 or (name == "S3_URL_TTL_SECONDS" and cfg[name] > 604800):
            raise ParserError(f"Недопустимое значение {name}.")
    if cfg["YANDEX_PROXY_URL"] and urlsplit(cfg["YANDEX_PROXY_URL"]).scheme not in {"http", "https"}:
        raise ParserError("YANDEX_PROXY_URL: нужен HTTP/HTTPS-прокси.")
    return cfg


def fetch_html(row, client, cfg, can_configure=True):
    for attempt in range(cfg["FETCH_ATTEMPTS"]):
        try:
            with client.stream("GET", "https://yandex.ru/video/search",
                               params={"text": row["query"], "lr": row["region_id"], "show_sensitive": "on"},
                               headers={"User-Agent": UA[row["device"]], "Accept-Language": "ru-RU,ru;q=0.9", "Accept": "text/html"}) as response:
                if response.status_code >= 500 and attempt + 1 < cfg["FETCH_ATTEMPTS"]:
                    time.sleep(2 ** attempt)
                    continue
                if response.status_code != 200:
                    raise ParserError(f"Яндекс вернул HTTP {response.status_code}; HTML не сохранён.")
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
                if ("captcha" in response.url.path.casefold()
                        or any(word in title for word in ("captcha", "ой!", "oh!", "доступ ограничен", "подтвердите"))
                        or re.search(r'<form\b[^>]*action\s*=\s*[\"\'][^\"\']*(?:checkcaptcha|showcaptcha)|\bid\s*=\s*[\"\'](?:js-)?(?:smart|checkbox)-?captcha', text, re.I)):
                    raise ParserError("Яндекс показал капчу или ограничил доступ; HTML не сохранён.")
                if (response.url.host not in {"yandex.ru", "www.yandex.ru", "ya.ru"}
                        or response.url.path.rstrip("/") not in {"/video/search", "/video/touch/search"}
                        or response.url.params.get("lr") != str(row["region_id"])):
                    raise ParserError("Яндекс перенаправил на другую страницу или потерял регион.")
                types = response.headers.get_list("content-type", split_commas=True)
                content_type = types[0] if types else ""
                if ("text/html" not in content_type.lower() or not re.search(rb"<(?:html|!doctype\s+html)\b", body, re.I)
                        or not re.search(r"video-serp|VideoSearch|/video/(?:touch/)?search", text)):
                    raise ParserError("Ответ Яндекса не похож на HTML выдачи видео.")
                state = unescape(text).replace('\\"', '"')
                flags = re.findall(r'"showSensitive"\s*:\s*(true|false)', state)
                # The last value is hydrated state; initial markup can contain default false.
                if not flags or flags[-1] != "true":
                    if not flags or not can_configure:
                        raise ParserError("Не удалось подтвердить отключение размытия 18+; HTML не сохранён.")
                    key = re.search(r'"sk"\s*:\s*"([^"\s]+)"', state)
                    if not key:
                        raise ParserError("Яндекс не предоставил ключ настройки размытия 18+.")
                    setting = client.get("https://yandex.ru/portal/set/tune", follow_redirects=False,
                                         params={"sk": key[1], "fields": "show_sensitive", "show_sensitive": "on", "retpath": str(response.url)},
                                         headers={"User-Agent": UA[row["device"]]})
                    if setting.status_code not in {200, 204, 302, 303}:
                        raise ParserError("Яндекс не принял настройку отключения размытия 18+.")
                    return fetch_html(row, client, cfg, can_configure=False)
                return prepare_html(body, str(response.url)), str(response.url), content_type
        except httpx.RequestError:
            if attempt + 1 == cfg["FETCH_ATTEMPTS"]:
                raise ParserError("Не удалось скачать HTML: проверьте сеть или прокси.") from None
            time.sleep(2 ** attempt)


def upload(row, snapshot, s3, cfg):
    body, source, content_type = snapshot
    digest = hashlib.sha256(json.dumps([row["query"], row["region_id"], row["device"]], ensure_ascii=False).encode()).hexdigest()[:20]
    key = "/".join(filter(None, [cfg["S3_PREFIX"].strip("/"), datetime.now(timezone.utc).strftime("%Y/%m/%d"), f"{digest}-{uuid4().hex}.html"]))
    location = {"Bucket": cfg["S3_BUCKET"], "Key": key}
    if cfg["S3_URL_MODE"] == "presigned":
        url = s3.generate_presigned_url("get_object", Params=location, ExpiresIn=cfg["S3_URL_TTL_SECONDS"])
    else:
        base = cfg["S3_PUBLIC_BASE_URL"].rstrip("/") or cfg["S3_ENDPOINT_URL"].rstrip("/") + "/" + quote(cfg["S3_BUCKET"], safe="")
        url = base + "/" + quote(key, safe="/")
    s3.put_object(**location, Body=body, ContentType=content_type, ContentDisposition="inline",
                  Metadata={"source-url": source, "region-id": str(row["region_id"]), "device": row["device"]})
    return {**row, "url": url}


def write_output(path, result):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
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
    parser.add_argument("output", nargs="?", type=Path, default=Path("result.json"), help="Результат (по умолчанию result.json)")
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
            s3 = boto3.client("s3", endpoint_url=cfg["S3_ENDPOINT_URL"], region_name=cfg["AWS_DEFAULT_REGION"],
                              config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, connect_timeout=10,
                                            read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3}))
            with httpx.Client(timeout=cfg["FETCH_TIMEOUT_SECONDS"], follow_redirects=True,
                              proxy=cfg["YANDEX_PROXY_URL"] or None, trust_env=False) as client:
                for index, row in enumerate(rows, 1):
                    print(f"Обрабатываю {index}/{len(rows)}…", file=sys.stderr, flush=True)
                    results.append(upload(row, fetch_html(row, client, cfg), s3, cfg))
        write_output(target, results if is_batch else results[0])
    except json.JSONDecodeError as exc:
        print(f"Ошибка JSON: строка {exc.lineno}, столбец {exc.colno}.", file=sys.stderr)
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
    except ParserError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    print(f"Готово: {target.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
