"""Настройки по умолчанию из отдельного JSON-файла."""
import json
from pathlib import Path

DEFAULT_CONFIG = json.loads(
    (Path(__file__).resolve().parent / "resources" / "default_config.json").read_text(encoding="utf-8")
)
