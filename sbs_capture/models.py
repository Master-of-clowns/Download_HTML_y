"""Запросы и записи выдачи."""

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Query:
    query: str
    region_id: int
    device: str

    @classmethod
    def load(cls, obj):
        if not isinstance(obj, dict):
            raise ValueError("Each input must be an object")
        text = obj.get("query")
        region = obj.get("region_id")
        device = str(obj.get("device", "")).upper()
        device = {"PC": "DESKTOP", "MOBILE": "ANDROID", "IOS": "IPHONE"}.get(device, device)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("query must be a nonempty string")
        if isinstance(region, bool) or not str(region).isdigit():
            raise ValueError("region_id must be a nonnegative integer")
        if device not in {"DESKTOP", "ANDROID", "IPHONE"}:
            raise ValueError("device must be DESKTOP, ANDROID or IPHONE")
        return cls(text, int(region), device)

    def as_dict(self):
        return asdict(self)


@dataclass
class Item:
    id: str
    position: int
    title: str
    thumbnail_url: str
    image_url: str
    page_url: str
    censored: bool = False
