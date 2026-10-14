"""Публикация HTML в S3 с проверкой чтения."""

import hashlib
from urllib.parse import quote
import boto3
import requests
from botocore.config import Config


class Storage:
    def __init__(self, config):
        self.config = config
        self.bucket = config["bucket"]
        self.prefix = config.get("prefix", "sbs-capture").strip("/")
        if not self.prefix or ".." in self.prefix.split("/"):
            raise ValueError("A nonempty, safe S3 prefix is required")
        self.endpoint = config["endpoint"].rstrip("/")
        # Credentials come only from standard AWS environment/profile providers.
        self.client = boto3.client("s3", endpoint_url=self.endpoint,
            region_name=config.get("region", "ru-1"),
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=15, read_timeout=30))

    def publish(self, key, html):
        full_key = f"{self.prefix}/{key}"
        body = html.encode("utf-8")
        args = dict(Bucket=self.bucket, Key=full_key, Body=body,
                    ContentType="text/html; charset=utf-8", ContentDisposition="inline",
                    CacheControl="public, max-age=31536000, immutable",
                    Metadata={"sha256": hashlib.sha256(body).hexdigest()})
        if self.config.get("acl"):
            args["ACL"] = self.config["acl"]
        self.client.put_object(**args)
        public_base = self.config.get("public_base_url", f"{self.endpoint}/{self.bucket}").rstrip("/")
        url = public_base + "/" + quote(full_key, safe="/")
        # Do not report a URL as successful until anonymous readback matches.
        response = requests.get(url, timeout=(15, 45))
        response.raise_for_status()
        if response.content != body or "text/html" not in response.headers.get("Content-Type", ""):
            raise ValueError("S3 readback does not match uploaded HTML")
        return url
