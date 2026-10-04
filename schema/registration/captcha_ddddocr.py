"""Image captcha solver via ddddocr (NTUH ValidNumerImage).

Google reCAPTCHA v3 tokens are NOT solved here — supply via env
``NTUH_RECAPTCHA_TOKEN`` or a browser helper that runs grecaptcha.execute.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

_ocr = None


def _get_ocr():
    global _ocr
    if _ocr is None:
        import ddddocr

        _ocr = ddddocr.DdddOcr(show_ad=False)
    return _ocr


def solve_image_captcha(image_bytes: bytes) -> str:
    """Return guessed captcha text (case-insensitive on NTUH)."""
    if not image_bytes:
        raise ValueError("empty captcha image")
    ocr = _get_ocr()
    text = (ocr.classification(image_bytes) or "").strip()
    # NTUH hint: 不分大小寫 — normalize to lower for stability
    text = text.replace(" ", "")
    log.info("ddddocr captcha guess len=%s", len(text))
    return text


def captcha_meta(image_bytes: bytes, *, src: str | None = None) -> dict[str, Any]:
    guess = solve_image_captcha(image_bytes)
    return {
        "kind": "image",
        "engine": "ddddocr",
        "src": src,
        "guess": guess,
        "image_bytes_len": len(image_bytes),
    }
