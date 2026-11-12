"""CR-rg-hydrate-fetch: the hydration stage's TRANSFORM — fetch the full advert for a
held posting with headless Playwright. The Adzuna API returns only a snippet and the
detail page 403s plain HTTP (Cloudflare); a real browser engine passes, and the advert
lives in one consistent container (section.adp-body). Other job pages are read from their
main content, not the site's navigation and footer (CR-rg-ad-page-chrome; advert_text).
The output is NOT trusted here —
rolegenie.hydration_problems validates it before any handover (the stage contract:
validate_input -> transform -> correctness-check -> persist). Playwright is imported
lazily so the rest of RoleGenie never loads it.

Install (not auto): pip install playwright && playwright install chromium.
"""
from __future__ import annotations

import re

# Mirror of contracts/rolegenie/domain.ts ADVERT_CONTAINERS (CR-rg-ad-page-chrome), in order:
# the Adzuna detail page's advert container first, then the page's main content.
ADVERT_CONTAINERS = ("section.adp-body", "main", "[role=main]", "article")
# Adzuna emits two URL forms for the same advert id: the detail page
# (/jobs/details/<id>) and a click-tracking redirect (/jobs/land/ad/<id>). Only the
# detail page is fetchable and parseable; both carry the id, so normalise to it.
_ADZUNA_ID = re.compile(r"adzuna\.co\.uk/jobs/(?:details|land/ad)/(\d+)")


def details_url(url: str) -> str | None:
    """The Adzuna detail-page URL for a stored posting URL (detail or land/ad form),
    or None if the URL is not an Adzuna advert we can hydrate."""
    m = _ADZUNA_ID.search(url or "")
    return f"https://www.adzuna.co.uk/jobs/details/{m.group(1)}" if m else None
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _text(el) -> str:
    return (el.inner_text() or "").strip() if el else ""


def advert_text(page) -> str:
    """The advert's text on a loaded page (the ADVERT_CONTAINERS rule). Adzuna's container
    is returned whenever present, as before. Otherwise the first of main / [role=main] /
    article whose text is a clean advert is returned, but only when the page body is clean
    too, so an expiry or anti-bot phrase outside the container still reaches the validator.
    Else the page body, as before: a container is never a guess that drops ad lines (R1)."""
    from rolegenie import hydration_problems  # lazy: rolegenie imports this module lazily

    adzuna, *rest = ADVERT_CONTAINERS
    el = page.query_selector(adzuna)
    if el:
        return _text(el)
    body = (page.inner_text("body") or "").strip()
    if not hydration_problems(body):
        for sel in rest:
            text = _text(page.query_selector(sel))
            if text and not hydration_problems(text):
                return text
    return body


def fetch_advert(url: str, timeout_ms: int = 30000, settle_ms: int = 4000) -> tuple[str, int | None]:
    """Return (advert_text, http_status). advert_text follows advert_text(page): the advert
    container's or main content's text when the page has one, else the page body text (the
    correctness check decides if that is a real advert). Raises RuntimeError if
    Playwright/Chromium is not available."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment guard
        raise RuntimeError("playwright not installed (pip install playwright; "
                           "playwright install chromium)") from exc
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=_UA, viewport={"width": 1280, "height": 900},
                                      locale="en-GB")
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            status = resp.status if resp else None
            page.wait_for_timeout(settle_ms)  # let any JS/anti-bot challenge settle
            return advert_text(page), status
        finally:
            browser.close()
