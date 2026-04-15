"""
Shared helpers for sources that talk to Cloudflare-protected private APIs
(chatgpt.com, claude.ai). The goal is to look enough like a real browser
that Cloudflare's bot detection lets us through on the same session the
user already passed interactively.

Three building blocks:

1. ``CLOUDFLARE_OPTIONAL_COOKIES`` — cookie names to pass along when they
   exist in the user's browser (they are the proof CF already issued
   this session a clearance). Absence is tolerated; presence increases
   the odds of a clean response.
2. ``browser_headers()`` — the full set of headers a Chromium tab sends
   on a same-origin fetch. Minimal-header urllib requests get flagged as
   non-browser traffic and 403-challenged.
3. ``looks_like_cf_block()`` — distinguish a real auth 403 (JSON body)
   from a Cloudflare challenge page (HTML body). Callers raise different
   exception classes so the user is told the right thing.
"""

from __future__ import annotations


CLOUDFLARE_OPTIONAL_COOKIES: set[str] = {"cf_clearance", "__cf_bm"}

# Matches a stable recent Chromium release. The specific version doesn't
# matter much — CF mostly cares that it parses as a known UA template
# and that it's consistent with the Sec-Ch-Ua / Sec-Fetch-* headers
# below. If cf_clearance was issued to a different UA, CF will reject
# us regardless of this value; but then the user's next browser visit
# will mint a fresh cookie and we retry.
CHROMIUM_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)


def browser_headers(
    origin: str,
    *,
    referer: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build a header dict that looks like a same-origin Chromium fetch."""
    headers = {
        "User-Agent": CHROMIUM_USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": origin,
        "Referer": referer or f"{origin}/",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "Sec-Ch-Ua": '"Chromium";v="129", "Not.A/Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"macOS"',
    }
    if extra:
        headers.update(extra)
    return headers


def looks_like_cf_block(body: str) -> bool:
    """True if the response body is a Cloudflare challenge page rather
    than a real JSON error from the target API."""
    head = body.lstrip()[:500].lower()
    if head.startswith("<"):
        return True
    return "cloudflare" in head or "cf-mitigated" in head or "__cf_chl" in head
