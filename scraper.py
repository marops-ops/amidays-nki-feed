#!/usr/bin/env python3
"""
NKI Nettstudier product feed generator.

Crawls nki.no via sitemap.xml, extracts product data from each course/program
page, classifies it, tracks price history to detect real sale prices, checks
availability, and writes an RSS 2.0 feed to docs/feed.xml.

=============================================================================
2026-09-12 REWRITE: NKI relaunched nki.no on a new stack (Next.js + Sanity
CMS, confirmed via _next/static chunk requests and cdn.sanity.io image URLs).
The old GTM dataLayer 'productDetailView' push, the "Utdanningsniva:/Pris:"
facts-box text, and the hero-<img>-after-<h1> pattern are ALL gone. This is a
from-scratch rewrite of the extraction layer; everything below the "Page
parsing" section (price history, XML building, tiering) is unchanged.

New source of truth: schema.org JSON-LD (<script type="application/ld+json">),
which is actually a step up in data quality over the old dataLayer approach:
  - Two shapes depending on content type:
      @type "Course"  -- plain kurs/enkeltfag/yrkesfag pages. Has name,
                          description (real, good SEO copy -- our generated
                          fallback description should rarely trigger now),
                          offers.price/priceCurrency/availability, and
                          hasCourseInstance.courseWorkload (ISO 8601
                          duration, e.g. "PT250H" = 250 hours). NO sku, NO
                          image field.
      @type "Product"  -- fagskole/"pakke"-style pages. Has all of the above
                          PLUS sku (the OLD PG-xxxx catalog ID survives here!)
                          and a direct, correct image URL (Sanity CDN).
  - A BreadcrumbList block whose 2nd item's URL query string
    (?level=X&subject=Y) is a clean, structured classification signal --
    replaces the old Utdanningsniva-text + CATEGORY_ENTITY_OVERRIDE hack
    entirely. Confirmed values: level=kurs, level=vgo (subject=yrkesfag /
    praksiskandidat / enkeltfag / studiekompetanse / realfag), and presumably
    level=fagskole / level=enkeltemner (not yet observed directly but
    follows the same pattern). Crucially, subject=yrkesfag vs
    subject=enkeltfag correctly disambiguates vocational-vs-academic VGO
    single subjects that the old Utdanningsniva field could NOT tell apart
    (e.g. "Kommunikasjon og samhandling for ambulansefag" -> subject=yrkesfag
    even though it lives under the same /videregaende/enkeltfag/ URL prefix
    as academic subjects like Biologi 1, subject=enkeltfag).
  - Discontinued courses (e.g. old "Tannlegeassistent") now return a real
    HTTP 404 instead of a live 200 page with an "ikke lengre aktiv" banner --
    simpler than before, the DISCONTINUED_MARKERS text check below is now
    just belt-and-suspenders for any stale page that might still 200.
  - og:image is now RELIABLE as an image fallback (unlike the old site,
    where it sometimes pointed at an unrelated photo) because actual <img>
    tags in the static HTML are lazy-loaded blank placeholders on this
    Next.js build -- there is no hero-image-in-DOM to scrape anymore, so we
    prefer the JSON-LD "image" field (Product type) and fall back to
    og:image (works for both types).

ID strategy (confirmed with Robin 2026-09-12): Product-type pages keep their
sku (old PG-xxxx ID, preserves price-history/pixel continuity). Course-type
pages have no sku at all anymore, so we use the URL path as the id instead
(e.g. "kurs/trening-som-medisin") -- stable, guaranteed unique, and simpler
than trying to fabricate one from the title.

2026-09-14 FIX -- sale-price regex was matching the SITE HEADER, not the
product: the "Foer/Naa" DOM check below originally ran against
soup.get_text() for the WHOLE page. Turns out every nki.no page's main
navigation ("Hovedmeny") renders a hidden "campaign" widget listing 2-3
currently-discounted packages (e.g. "Generell studiekompetanse (23/5)" --
this is genuine rendered DOM, not script/JSON payload, confirmed via raw
HTML fetch) *before* the page's own <h1> in document order. re.search()
only returns the first match, so on any page where that widget's Foer/Naa
text appeared before the product's own price block, we grabbed the WRONG
product's price entirely -- root cause of 127/136 feed items showing an
identical, bogus "35900 NOK" regular price regardless of their real price.
Fixed by scoping the Foer/Naa search to text *after* the <h1>
(_text_after_h1 below) -- confirmed via live re-fetch of a real sale page
(Biologi 2: real "Foer 5490 / Naa 4667" both occur after <h1>, the header
widget's unrelated 35900/39900/38900 entries all occur before it) and a
false-positive page (AI i arbeidslivet, no sale at all: header widget was
the ONLY source of "Foer" text on the whole page).

Sale detection: offers.price reflects the true current price directly
(unlike the old dataLayer bug where price never changed for a sale) --
resolve_price's baseline-diff logic catches real drops on its own. The DOM
Foer/Naa check (now correctly scoped, see above) is used only to recover
the regular price for first-run baseline-seeding.
=============================================================================

Field schema note (2026-07-08): the feed is deliberately shaped to match a
Hunch/Meta-oriented reference feed Robin uses for ad templates -- bare
(un-namespaced) custom_label_0/1/2, fb_product_category, feed_name,
internal_label, plus g:-namespaced item_group_id, a text-based
google_product_category, and a single-value product_type.

fb_product_category note (2026-07-09/11): must be a value from Meta's own
category taxonomy (separate numbering from Google's Product Taxonomy).
Default text value here is "Interests > Education > Distance education";
Robin also has a numeric override rule on the Hunch side.

Other longstanding design notes:
- sale_price is only ever emitted when the resolved current price is LOWER
  than the persisted baseline in data/price_history.json. First run
  establishes baselines with no sale_price anywhere.
- Lanekassen eligibility is intentionally NOT scraped/emitted (Robin's call).
- Availability is derived from the HTTP status of the same GET used to
  scrape the page (200 -> in stock, anything else -> out of stock).
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

USER_AGENT = "Amidays-FeedBot/1.0"
SITEMAP_URL = "https://www.nki.no/sitemap.xml"
BASE_URL = "https://www.nki.no"
REQUEST_TIMEOUT = 20
REQUEST_DELAY_SECONDS = 0.5  # be polite, avoid tripping 403/429
MAX_RETRIES = 2

OUTPUT_FEED_PATH = Path("docs/feed.xml")
PRICE_HISTORY_PATH = Path("data/price_history.json")

SALE_WINDOW_DAYS = 30  # rolling window while a price drop is active

FEED_TITLE = "NKI Nettstudier — Kurs og utdanning"
FEED_LINK = "https://www.nki.no"
FEED_DESCRIPTION = "Produktfeed for NKI Nettstudier. Nettstudier med fleksibel oppstart."
BRAND = "NKI"
DEFAULT_IMAGE = f"{BASE_URL}/assets/images/og-default.jpg"

FB_PRODUCT_CATEGORY = "Interests > Education > Distance education"

# Belt-and-suspenders only now (see module docstring) -- new site 404s
# discontinued courses instead of showing this banner on a 200 page.
DISCONTINUED_MARKERS = (
    "ikke lengre aktiv",
)

ENTITY_TYPE_DISPLAY: dict[str, str] = {
    "kurs": "Kurs",
    "enkeltemner": "Enkeltemner",
    "yrkesfag": "Yrkesfag",
    "vgo_teori": "Videregående",
    "fagskole": "Fagskole",
}

ALLOWED_PREFIXES = (
    "/kurs/",
    "/enkeltemner/",
    "/fagskole/",
    "/videregaende/",
)
EXCLUDED_EXACT_PATHS = {
    "/kurs",
    "/enkeltemner",
    "/fagskole",
    "/videregaende",
    "/videregaende/fagpakker",
    "/videregaende/realfag",
    "/videregaende/studiekompetanse",
    "/videregaende/yrkesfag",
    "/videregaende/enkeltfag",
}

# breadcrumb ?level=... -> entity_type, for levels that don't need a subject
# to disambiguate.
LEVEL_TO_ENTITY_TYPE = {
    "kurs": "kurs",
    "enkeltemner": "enkeltemner",
    "fagskole": "fagskole",
}
# breadcrumb ?level=vgo&subject=... -> entity_type. This is what replaces the
# old Utdanningsniva/CATEGORY_ENTITY_OVERRIDE ambiguity fix -- subject
# reliably distinguishes vocational vs academic VGO single subjects.
VGO_SUBJECT_TO_ENTITY_TYPE = {
    "yrkesfag": "yrkesfag",
    "praksiskandidat": "yrkesfag",
    "enkeltfag": "vgo_teori",
    "studiekompetanse": "vgo_teori",
    "realfag": "vgo_teori",
}
# Last-resort fallback if breadcrumb data is missing/malformed: guess from URL.
URL_FALLBACK_ENTITY_TYPE = (
    ("/fagskole/", "fagskole"),
    ("/videregaende/yrkesfag/", "yrkesfag"),
    ("/videregaende/enkeltfag/", "vgo_teori"),
    ("/videregaende/studiekompetanse/", "vgo_teori"),
    ("/videregaende/realfag/", "vgo_teori"),
    ("/kurs/", "kurs"),
    ("/enkeltemner/", "enkeltemner"),
)

LANEKASSEN_TEXT = "Lånekassegodkjent"  # not wired up, see module docstring

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("nki_feed")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Product:
    id: str
    title: str
    description: str
    link: str
    image_link: str
    in_stock: bool
    price: float
    sale_price: Optional[float]
    sale_price_effective_date: Optional[str]
    category: str
    entity_type: str
    duration_months: Optional[int]
    duration_text: Optional[str]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def fetch(url: str) -> Optional[requests.Response]:
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            return resp
        except requests.RequestException as exc:
            log.warning("Request failed (%s/%s) for %s: %s", attempt, MAX_RETRIES + 1, url, exc)
            time.sleep(1.5 * attempt)
    return None


# --------------------------------------------------------------------------- #
# Sitemap
# --------------------------------------------------------------------------- #


def get_candidate_urls() -> list[str]:
    resp = fetch(SITEMAP_URL)
    if resp is None or resp.status_code != 200:
        raise RuntimeError(f"Could not fetch sitemap: {SITEMAP_URL}")

    soup = BeautifulSoup(resp.content, "xml")
    urls = [loc.get_text(strip=True) for loc in soup.find_all("loc")]

    candidates = []
    for url in urls:
        path = url.replace(BASE_URL, "")
        if path in EXCLUDED_EXACT_PATHS:
            continue
        if any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES):
            candidates.append(url)

    log.info("Sitemap: %d total URLs, %d candidate product URLs", len(urls), len(candidates))
    return candidates


# --------------------------------------------------------------------------- #
# Page parsing (rewritten 2026-09-12 for the new nki.no -- see module docstring)
# --------------------------------------------------------------------------- #

_FACT_RE_TEMPLATE = r"{label}:\s*\n?\s*([^\n]+)"
# New site (2026-09) price/sale markup: no "Pris:" label anymore, "Før" and
# "Nå" appear in either order, format is "5 490 kr" (space thousands, no
# comma). Matched independently so order doesn't matter.
_PRICE_FOER_RE = re.compile(r"Før\s*\n?\s*([\d\s]+)\s*kr", re.IGNORECASE)
_PRICE_NAA_RE = re.compile(r"Nå\s*\n?\s*([\d\s]+)\s*kr", re.IGNORECASE)
_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _extract_ld_json_blocks(soup: BeautifulSoup) -> list[dict]:
    blocks = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        blocks.append(data)
    return blocks


def _find_first_ld_json(blocks: list[dict], types: tuple[str, ...]) -> Optional[dict]:
    for block in blocks:
        if isinstance(block, dict) and block.get("@type") in types:
            return block
    return None


def _parse_breadcrumb(breadcrumb: Optional[dict]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (level, subject, category_label). category_label is the 2nd
    breadcrumb item's display name (e.g. "Helse og livsstil" for kurs pages,
    "Yrkesfag" for VGO pages) -- used as our raw 'category' field downstream.
    """
    if not breadcrumb:
        return None, None, None
    items = breadcrumb.get("itemListElement") or []
    if len(items) < 2:
        return None, None, None
    second = items[1]
    category_label = second.get("name")
    item_url = second.get("item") or ""
    query = parse_qs(urlparse(item_url).query)
    level = (query.get("level") or [None])[0]
    subject = (query.get("subject") or [None])[0]
    return level, subject, category_label


def _is_discontinued(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in DISCONTINUED_MARKERS)


def _extract_fact(text: str, label: str) -> Optional[str]:
    pattern = _FACT_RE_TEMPLATE.format(label=re.escape(label))
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _parse_price_nok(text: str) -> Optional[float]:
    digits = re.sub(r"[^\d]", "", text)
    return float(digits) if digits else None


def _text_after_h1(soup: BeautifulSoup) -> str:
    """
    Text starting from the page's <h1> onward, in document order --
    deliberately EXCLUDES the site header/nav, which renders before the
    <h1> and contains a "campaign" widget listing 2-3 unrelated,
    currently-discounted packages with their own "Foer"/"Naa" price text
    (confirmed real DOM, not script payload -- see 2026-09-14 fix note in
    the module docstring). Without this scoping, _extract_price_info's
    re.search() (first-match-only) would grab that widget's price instead
    of the current product's own, on any page where the widget happens to
    render before the product's price block.

    Falls back to the full page text if no <h1> is found (shouldn't happen
    on a real product page, but fail open rather than crash).
    """
    h1 = soup.find("h1")
    if h1 is None:
        return soup.get_text("\n")
    parts = [h1.get_text(" ", strip=True)]
    for node in h1.find_all_next(string=True):
        parent_name = node.parent.name if node.parent else ""
        if parent_name in ("script", "style"):
            continue
        parts.append(str(node))
    return "\n".join(parts)


def _extract_price_info(text: str) -> dict:
    """
    Detects an active 'Foer/Naa' sale on the NEW site (2026-09): shown as a
    crossed-out "Før X kr" plus current "Nå Y kr", with a "-N%" badge, in
    either order (confirmed live on "Biologi 2": "Nå 4 667 kr / Før 5 490 kr
    / -14%"). offers.price in JSON-LD DOES already reflect the discounted
    price correctly on this site (unlike the old dataLayer bug), so this DOM
    check exists to recover the REGULAR price for baseline-seeding -- see
    resolve_price's regular_hint param.

    IMPORTANT: caller must pass text scoped to the current product (see
    _text_after_h1) -- do NOT pass the raw whole-page soup.get_text(), see
    2026-09-14 fix note in the module docstring.
    """
    foer_match = _PRICE_FOER_RE.search(text)
    naa_match = _PRICE_NAA_RE.search(text)
    if foer_match and naa_match:
        before = _parse_price_nok(foer_match.group(1))
        now = _parse_price_nok(naa_match.group(1))
        return {"current": now, "regular_dom": before}
    return {"current": None, "regular_dom": None}


def _parse_iso8601_duration(value: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """
    'PT250H' -> (months≈1, "250 timer"). hasCourseInstance.courseWorkload is
    the new site's duration field, ISO 8601 duration format. Only present on
    Course-type pages so far -- Product-type pages simply don't have it.
    """
    if not value:
        return None, None
    match = _ISO8601_DURATION_RE.match(value.strip())
    if not match:
        return None, value
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    total_hours = (
        parts["years"] * 8760
        + parts["months"] * 730
        + parts["weeks"] * 168
        + parts["days"] * 24
        + parts["hours"]
        + parts["minutes"] / 60
    )
    if not total_hours:
        return None, value
    months = max(1, round(total_hours / 730))
    if parts["hours"] and not any([parts["years"], parts["months"], parts["weeks"], parts["days"]]):
        text = f"{parts['hours']} timer"
    else:
        text = value
    return months, text


def _fallback_description(title: str, entity_type: Optional[str], category: Optional[str]) -> str:
    """
    Guaranteed non-empty description -- should rarely trigger now that the
    new site's JSON-LD has real per-page descriptions, but kept as a safety
    net (Meta/Hunch reject a product outright if g:description is empty).
    """
    display = entity_display(entity_type) if entity_type else ""
    if display and category:
        return f"{title} er et {display.lower()}-studium innen {category.lower()} hos NKI Nettstudier."
    if category:
        return f"{title} hos NKI Nettstudier, innen {category.lower()}."
    return f"{title} hos NKI Nettstudier."


def classify_entity_type(level: Optional[str], subject: Optional[str], path: str) -> str:
    if level == "vgo":
        if subject and subject in VGO_SUBJECT_TO_ENTITY_TYPE:
            return VGO_SUBJECT_TO_ENTITY_TYPE[subject]
        log.warning("level=vgo with unrecognized subject %r for %s, defaulting to vgo_teori", subject, path)
        return "vgo_teori"

    if level in LEVEL_TO_ENTITY_TYPE:
        return LEVEL_TO_ENTITY_TYPE[level]

    if level:
        log.warning("Unrecognized breadcrumb level %r for %s, falling back to URL", level, path)
    else:
        log.warning("No breadcrumb level found for %s, falling back to URL", path)

    for prefix, entity_type in URL_FALLBACK_ENTITY_TYPE:
        if path.startswith(prefix):
            return entity_type
    log.warning("Could not classify entity_type for %s, defaulting to 'kurs'", path)
    return "kurs"


def parse_product_page(url: str, html: str) -> Optional[dict]:
    """Returns a raw field dict, or None if this isn't a product page."""
    soup = BeautifulSoup(html, "lxml")
    blocks = _extract_ld_json_blocks(soup)

    product = _find_first_ld_json(blocks, ("Course", "Product"))
    if product is None:
        return None

    text = soup.get_text("\n")
    if _is_discontinued(text):
        log.info("%s has a discontinued-course marker in the page body, skipping", url)
        return None

    breadcrumb = _find_first_ld_json(blocks, ("BreadcrumbList",))
    level, subject, category = _parse_breadcrumb(breadcrumb)

    title = (product.get("name") or "").strip()

    description = (product.get("description") or "").strip()

    sku = product.get("sku")
    path = url.replace(BASE_URL, "").strip("/")
    item_id = sku or path  # URL path used as id for Course-type pages (no sku) -- confirmed with Robin

    image_link = product.get("image")
    if not image_link:
        og_image = soup.find("meta", attrs={"property": "og:image"})
        image_link = og_image["content"].strip() if og_image and og_image.get("content") else None

    offers = product.get("offers") or {}
    dl_price = offers.get("price")

    # Scoped to text AFTER <h1> only -- see _text_after_h1 docstring and the
    # 2026-09-14 fix note at the top of this module. Using the whole-page
    # text here was the bug: it matched the site header's unrelated
    # "campaign" widget instead of this product's own price.
    price_text = _text_after_h1(soup)
    price_info = _extract_price_info(price_text)
    notes = []
    # offers.price already reflects the live/current price correctly on the
    # new site (confirmed: shows the discounted price during an active
    # sale) -- use it directly. price_info["regular_dom"] is only used to
    # seed an accurate baseline for products we're seeing for the first time
    # while already on sale (see resolve_price's regular_hint).
    price = float(dl_price) if dl_price is not None else price_info["current"]
    regular_hint = price_info["regular_dom"]
    if regular_hint is not None:
        notes.append(f"Active DOM sale detected: før {regular_hint} nå {price_info['current']} (offers.price={dl_price})")

    if not description:
        notes.append("No description in structured data, used generated fallback")

    duration_months, duration_text = _parse_iso8601_duration(
        (product.get("hasCourseInstance") or {}).get("courseWorkload")
    )

    return {
        "id": item_id,
        "title": title,
        "description": description,
        "image_link": image_link,
        "category": category or "",
        "price": price,
        "regular_hint": regular_hint,
        "level": level,
        "subject": subject,
        "duration_text": duration_text,
        "duration_months": duration_months,
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# Tiering
# --------------------------------------------------------------------------- #


def price_tier(price: float) -> str:
    if price < 5000:
        return "under_5000"
    if price <= 15000:
        return "5000_15000"
    return "over_15000"


def duration_tier(months: Optional[int]) -> str:
    if months is None:
        return "kort"
    if months < 6:
        return "kort"
    if months <= 12:
        return "medium"
    return "lang"


# --------------------------------------------------------------------------- #
# Price history / sale price detection
# --------------------------------------------------------------------------- #


def load_price_history() -> dict:
    if PRICE_HISTORY_PATH.exists():
        return json.loads(PRICE_HISTORY_PATH.read_text(encoding="utf-8"))
    return {}


def save_price_history(history: dict) -> None:
    PRICE_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    PRICE_HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def resolve_price(
    product_id: str,
    current_price: float,
    history: dict,
    today: date,
    regular_hint: Optional[float] = None,
) -> tuple[float, Optional[float], Optional[str]]:
    """
    Returns (price, sale_price, sale_price_effective_date).
    Mutates `history` in place.

    regular_hint: the DOM-scraped "Foer" (regular) price, when the page shows
    an active sale (see _extract_price_info). Used ONLY the first time we see
    a product_id with no existing history entry -- without it, a product that
    is *already* on sale the very first time we scrape it would silently get
    its discounted price baked in as the "normal" baseline, and the ongoing
    sale would never be detected (exactly what happened during the 2026-09
    site-rewrite ID migration, where every Course-type product effectively
    became "new" overnight). With the hint, we seed baseline=regular_hint
    instead and correctly emit sale_price on the very first run.
    """
    entry = history.get(product_id)

    if entry is None:
        if regular_hint is not None and regular_hint > current_price:
            history[product_id] = {"baseline": regular_hint, "drop_since": today.isoformat()}
            end = today + timedelta(days=SALE_WINDOW_DAYS)
            effective_date = f"{today.isoformat()}T00:00+0100/{end.isoformat()}T23:59+0100"
            return regular_hint, current_price, effective_date
        history[product_id] = {"baseline": current_price, "drop_since": None}
        return current_price, None, None

    baseline = entry["baseline"]

    if current_price < baseline:
        if not entry.get("drop_since"):
            entry["drop_since"] = today.isoformat()
        start = date.fromisoformat(entry["drop_since"])
        end = today + timedelta(days=SALE_WINDOW_DAYS)
        effective_date = f"{start.isoformat()}T00:00+0100/{end.isoformat()}T23:59+0100"
        return baseline, current_price, effective_date

    if current_price > baseline:
        entry["baseline"] = current_price
        entry["drop_since"] = None
        return current_price, None, None

    entry["drop_since"] = None
    return baseline, None, None


# --------------------------------------------------------------------------- #
# Crawl
# --------------------------------------------------------------------------- #


def crawl() -> list[Product]:
    history = load_price_history()
    today = date.today()
    products: list[Product] = []
    skipped_non_product = 0
    out_of_stock: list[str] = []

    for url in get_candidate_urls():
        time.sleep(REQUEST_DELAY_SECONDS)
        resp = fetch(url)
        if resp is None:
            log.error("No response at all for %s, skipping", url)
            continue

        in_stock = resp.status_code == 200
        if not in_stock:
            out_of_stock.append(f"{url} (HTTP {resp.status_code})")

        raw = parse_product_page(url, resp.text)
        if raw is None:
            skipped_non_product += 1
            log.info("No product data found on %s, treating as a listing/404 page (skipped)", url)
            continue

        if not raw["id"] or raw["price"] is None:
            log.warning("Incomplete product data on %s (id=%r price=%r), skipping", url, raw["id"], raw["price"])
            continue

        path = url.replace(BASE_URL, "")
        entity_type = classify_entity_type(raw["level"], raw["subject"], path)
        price, sale_price, effective_date = resolve_price(
            raw["id"], raw["price"], history, today, regular_hint=raw.get("regular_hint")
        )

        description = raw["description"] or _fallback_description(raw["title"], entity_type, raw["category"])

        products.append(
            Product(
                id=raw["id"],
                title=raw["title"],
                description=description,
                link=url,
                image_link=raw["image_link"] or DEFAULT_IMAGE,
                in_stock=in_stock,
                price=price,
                sale_price=sale_price,
                sale_price_effective_date=effective_date,
                category=raw["category"],
                entity_type=entity_type,
                duration_months=raw["duration_months"],
                duration_text=raw["duration_text"],
                notes=raw["notes"],
            )
        )

    save_price_history(history)

    log.info(
        "Crawl done: %d products, %d skipped (non-product pages), %d out of stock",
        len(products),
        skipped_non_product,
        len(out_of_stock),
    )
    if out_of_stock:
        log.warning("Out-of-stock items:\n%s", "\n".join(out_of_stock))

    return products


# --------------------------------------------------------------------------- #
# XML output
# --------------------------------------------------------------------------- #

NS_G = "http://base.google.com/ns/1.0"
NS_NKI = "http://nki.no/ns/ads/1.0"
ET.register_namespace("g", NS_G)
ET.register_namespace("nki", NS_NKI)


def _g(tag: str) -> str:
    return f"{{{NS_G}}}{tag}"


def _nki(tag: str) -> str:
    return f"{{{NS_NKI}}}{tag}"


def _sub(parent: ET.Element, tag: str, text: Optional[str]) -> None:
    if text is None or text == "":
        return
    el = ET.SubElement(parent, tag)
    el.text = str(text)


def entity_display(entity_type: str) -> str:
    return ENTITY_TYPE_DISPLAY.get(entity_type, entity_type.replace("_", " ").title())


def smart_title(title: str, entity_type: str, category: str) -> str:
    if not category:
        return title
    return f"{title} − {entity_display(entity_type).lower()} i {category.lower()}"


def google_product_category_path(entity_type: str, category: str) -> str:
    parts = ["utdanning", entity_display(entity_type).lower()]
    if category:
        parts.append(category.lower())
    return " > ".join(parts)


def build_feed_xml(products: list[Product]) -> ET.ElementTree:
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    _sub(channel, "title", FEED_TITLE)
    _sub(channel, "link", FEED_LINK)
    _sub(channel, "description", FEED_DESCRIPTION)

    for p in products:
        item = ET.SubElement(channel, "item")
        display_type = entity_display(p.entity_type)

        _sub(item, "custom_label_0", display_type)
        _sub(item, "custom_label_1", p.category)
        _sub(item, "custom_label_2", duration_tier(p.duration_months))
        _sub(item, "fb_product_category", FB_PRODUCT_CATEGORY)
        _sub(item, "feed_name", p.title)
        _sub(item, "internal_label", p.title)

        _sub(item, _g("id"), p.id)
        _sub(item, _g("title"), smart_title(p.title, p.entity_type, p.category))
        _sub(item, _g("description"), p.description)
        _sub(item, _g("link"), p.link)
        _sub(item, _g("image_link"), p.image_link)
        _sub(item, _g("item_group_id"), p.id)
        _sub(item, _g("availability"), "in stock" if p.in_stock else "out of stock")
        _sub(item, _g("condition"), "new")
        _sub(item, _g("brand"), BRAND)
        _sub(item, _g("price"), f"{p.price:.2f} NOK")
        if p.sale_price is not None:
            _sub(item, _g("sale_price"), f"{p.sale_price:.2f} NOK")
            _sub(item, _g("sale_price_effective_date"), p.sale_price_effective_date)
        _sub(item, _g("google_product_category"), google_product_category_path(p.entity_type, p.category))
        _sub(item, _g("product_type"), p.category)

        ads_params = {
            "utm_source": "google",
            "utm_medium": "cpc",
            "utm_campaign": p.entity_type,
            "utm_content": p.id,
        }
        _sub(item, _g("ads_redirect"), f"{p.link}?{urlencode(ads_params)}")

        _sub(item, _nki("entity_type"), p.entity_type)
        _sub(item, _nki("category"), p.category)
        _sub(item, _nki("duration"), p.duration_text)
        _sub(item, _nki("price_numeric"), str(int(p.price)))
        if p.sale_price is not None:
            _sub(item, _nki("sale_price_numeric"), str(int(p.sale_price)))

    return ET.ElementTree(rss)


def write_feed(tree: ET.ElementTree, path: Path = OUTPUT_FEED_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)
    log.info("Wrote feed to %s", path)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    products = crawl()
    if not products:
        log.error("No products scraped -- refusing to overwrite existing feed.xml")
        sys.exit(1)
    tree = build_feed_xml(products)
    write_feed(tree)


if __name__ == "__main__":
    main()
