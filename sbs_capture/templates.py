"""CSS и JavaScript, встраиваемые в сохранённую страницу."""
from pathlib import Path

RESOURCE_DIR = Path(__file__).resolve().parent / "resources"
NO_BLUR_CSS = (RESOURCE_DIR / "no_blur.css").read_text(encoding="utf-8")
NATIVE_CSS = NO_BLUR_CSS + (RESOURCE_DIR / "native.css").read_text(encoding="utf-8")
NATIVE_JS = (RESOURCE_DIR / "native.js").read_text(encoding="utf-8")
VIDEO_CSS = NO_BLUR_CSS + (RESOURCE_DIR / "video.css").read_text(encoding="utf-8")
