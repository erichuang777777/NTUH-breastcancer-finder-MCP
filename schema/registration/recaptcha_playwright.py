"""Mint Google reCAPTCHA v3 tokens on NTUH RegForm pages via Playwright.

Must run on the same origin as the site key (reg.ntuh.gov.tw). Prefer
navigating to an existing RegForm?newx= URL so grecaptcha is already loaded.

Usage:
  from schema.registration.recaptcha_playwright import mint_recaptcha_token
  token = mint_recaptcha_token(page_url, site_key=..., action="submit")

Env:
  NTUH_RECAPTCHA_HEADLESS=0  → headed Chromium (debug)
  NTUH_RECAPTCHA_TIMEOUT_MS  → default 60000

Falls back cleanly if playwright / browser binaries missing.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# Documented NTUH WebReg site key (also scraped from page when possible)
DEFAULT_SITE_KEY = "6LfudEshAAAAAMe_Ny_0xh2rjwsIZCr3Blzr-J4T"
DEFAULT_ACTION = "submit"
_SITE_KEY_RE = re.compile(r"grecaptcha\.execute\(\s*'([^']+)'")


class RecaptchaMintError(Exception):
    """Playwright / grecaptcha.execute failed."""


def playwright_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def extract_site_key(html: str) -> str | None:
    m = _SITE_KEY_RE.search(html or "")
    return m.group(1) if m else None


def mint_recaptcha_token(
    page_url: str,
    *,
    site_key: str | None = None,
    action: str = DEFAULT_ACTION,
    headless: bool | None = None,
    timeout_ms: int | None = None,
) -> str:
    """Open page_url in Chromium, run grecaptcha.execute, return token string.

    Raises RecaptchaMintError on failure.
    """
    if not page_url or not page_url.startswith("http"):
        raise RecaptchaMintError(f"invalid page_url={page_url!r}")
    if headless is None:
        headless = (os.environ.get("NTUH_RECAPTCHA_HEADLESS") or "1").strip() != "0"
    if timeout_ms is None:
        try:
            timeout_ms = int(os.environ.get("NTUH_RECAPTCHA_TIMEOUT_MS") or "60000")
        except ValueError:
            timeout_ms = 60000

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RecaptchaMintError(
            "playwright not installed — pip install playwright && playwright install chromium"
        ) from e

    key = (site_key or "").strip() or DEFAULT_SITE_KEY
    act = (action or DEFAULT_ACTION).strip() or DEFAULT_ACTION

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=headless)
        except Exception as e:
            raise RecaptchaMintError(
                f"chromium launch failed ({e}); run: .venv/bin/playwright install chromium"
            ) from e
        try:
            context = browser.new_context(
                locale="zh-TW",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 "
                    "BreastCareResearchBot/0.1"
                ),
            )
            page = context.new_page()
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_ms)
            # Wait for grecaptcha API
            page.wait_for_function(
                "() => typeof window.grecaptcha !== 'undefined' "
                "&& typeof window.grecaptcha.execute === 'function'",
                timeout=timeout_ms,
            )
            # Prefer site key from page if present
            page_key = page.evaluate(
                """() => {
                  const html = document.documentElement.innerHTML;
                  const m = html.match(/grecaptcha\\.execute\\(\\s*'([^']+)'/);
                  return m ? m[1] : null;
                }"""
            )
            if page_key:
                key = page_key
            token = page.evaluate(
                """async ({siteKey, action}) => {
                  await new Promise((resolve) => grecaptcha.ready(resolve));
                  return await grecaptcha.execute(siteKey, {action});
                }""",
                {"siteKey": key, "action": act},
            )
        finally:
            browser.close()

    if not token or not isinstance(token, str) or len(token) < 20:
        raise RecaptchaMintError(f"empty/short token from grecaptcha.execute (got {token!r})")
    log.info("minted reCAPTCHA v3 token len=%s site_key=%s…", len(token), key[:12])
    return token


def try_mint_recaptcha_token(
    page_url: str,
    *,
    site_key: str | None = None,
    action: str = DEFAULT_ACTION,
) -> dict[str, Any]:
    """Non-raising wrapper → {ok, token|None, error|None, site_key}."""
    try:
        tok = mint_recaptcha_token(page_url, site_key=site_key, action=action)
        return {
            "ok": True,
            "token": tok,
            "error": None,
            "site_key": site_key or DEFAULT_SITE_KEY,
            "token_len": len(tok),
        }
    except Exception as e:
        return {
            "ok": False,
            "token": None,
            "error": str(e),
            "site_key": site_key or DEFAULT_SITE_KEY,
            "token_len": 0,
        }


__all__ = [
    "DEFAULT_SITE_KEY",
    "DEFAULT_ACTION",
    "RecaptchaMintError",
    "playwright_available",
    "extract_site_key",
    "mint_recaptcha_token",
    "try_mint_recaptcha_token",
]
