"""Аргументы запуска и обработка входного файла."""

import argparse
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from .config import DEFAULT_CONFIG
from .models import Query
from .parser import parse
from .capture import Capture
from .assets import Assets
from .render import render
from .storage import Storage


def safe_error(error):
    text = str(error)
    for key, value in os.environ.items():
        if value and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            text = text.replace(value, "[redacted]")
    return text[:1000]


def main():
    ap = argparse.ArgumentParser(description="Скачать выдачу и опубликовать кликабельные HTML в S3")
    ap.add_argument("--input", required=True, help="JSON object, array or JSONL with query/region_id/device")
    ap.add_argument("--config", help="JSON configuration; built-in image settings by default")
    ap.add_argument("--service", choices=["images", "video"], help="Override service from config")
    ap.add_argument("--output", default="runs", help="Local run directory")
    ap.add_argument("--local-only", action="store_true", help="Skip upload, return local paths")
    ap.add_argument("--html", help="Repair a saved full HTML document; one input query only")
    ap.add_argument("--source-url", help="Original search URL for --html")
    args = ap.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else json.loads(json.dumps(DEFAULT_CONFIG))
        if not isinstance(config, dict):
            raise ValueError("Config must be a JSON object")
        # Mandatory for every export, including old configs with false.
        config["remove_blur"] = True
        if args.service:
            config["service"] = args.service
        text = Path(args.input).read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        tasks = [Query.load(x) for x in (data if isinstance(data, list) else [data])]
        if not tasks:
            raise ValueError("Input is empty")
        if args.html and (len(tasks) != 1 or not args.source_url):
            raise ValueError("--html needs exactly one query and --source-url")
        if config.get("service", "images") not in {"images", "video"}:
            raise ValueError("service must be images or video")
        storage = None
        if not args.local_only:
            storage = Storage(config["s3"])
            if storage.client._request_signer._credentials is None:
                raise ValueError("Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY or AWS_PROFILE")
    except Exception as error:
        print(safe_error(error), file=sys.stderr)
        return 2
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = Path(args.output).resolve() / run_id
    output.mkdir(parents=True, exist_ok=False)
    if storage:
        print('Режим: публикация HTML в S3; публичные ссылки будут в строках HTML: и urls.txt.',
              file=sys.stderr, flush=True)
    else:
        print('Режим: локальное сохранение (--local-only); публичные ссылки не создаются.',
              file=sys.stderr, flush=True)
    service = config.get("service", "images")
    results, assets = [], Assets()
    capture, playwright = None, None
    try:
        if not args.html:
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            capture = Capture(playwright, config)
        for index, task in enumerate(tasks, 1):
            result = {"input_index": index - 1, **task.as_dict(), "service": service, "status": "error"}
            try:
                if args.html:
                    soup, items = parse(Path(args.html).read_text(encoding="utf-8"), service, args.source_url)
                    info = {"captured_url": args.source_url, "mode": "repair"}
                else:
                    soup, items, info = capture.run(task)
                html, audit = render(soup, items, task, service, info["captured_url"], assets)
                digest = hashlib.sha256(json.dumps(task.as_dict(), sort_keys=True).encode()).hexdigest()[:12]
                name = f"{index:06d}-{digest}"
                path = output / f"{name}.html"
                path.write_text(html, encoding="utf-8")
                (output / f"{name}.json").write_text(json.dumps({"request": task.as_dict(), "capture": info,
                    "audit": audit, "items": [asdict(x) for x in items]}, ensure_ascii=True, indent=2), encoding="utf-8")
                result.update(audit=audit, html_path=str(path))
                if storage:
                    result["s3_url"] = storage.publish(f"{run_id}/{service}/{name}.html", html)
                    print(f'HTML: {result["s3_url"]}', file=sys.stderr, flush=True)
                result["status"] = "ok"
            except Exception as error:
                result["error"] = safe_error(error)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            (output / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            if storage:
                (output / 'urls.txt').write_text(
                    ''.join(r['s3_url'] + '\n' for r in results if r.get('s3_url')),
                    encoding='utf-8')
    except Exception as error:
        print(safe_error(error), file=sys.stderr)
        return 2
    finally:
        if capture:
            capture.close()
        if playwright:
            playwright.stop()
    print(f"Results: {output / 'results.json'}", file=sys.stderr)
    if storage:
        print(f"HTML links: {output / 'urls.txt'}", file=sys.stderr)
    return 1 if any(r["status"] != "ok" for r in results) else 0
