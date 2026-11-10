#!/usr/bin/env python3
"""
NKI Nettstudier product feed generator.

Crawls nki.no via sitemap.xml, extracts product data from each course/program
page, classifies it, detects real sale prices, checks availability, and
writes an RSS 2.0 feed to docs/feed.xml.

=============================================================================
2026-09-12 REWRITE: NKI relaunched nki.no on a new stack (Next.js + Sanity
CMS). Data now comes from schema.org JSON-LD (<script type="application/ld+json">):
  - @type "Course"  -- plain kurs/enkeltfag/yrkesfag pages. name, description,
                       offers.price, hasCourseInstance.courseWorkload (ISO 8601
                       duration). NO sku, NO image.
  - @type "Product" -- fagskole/"pakke" pages. Same PLUS sku (old PG-xxxx ID)
                       and a direct image URL.
  - BreadcrumbList  -- 2nd item URL query (?level=X&subject=Y) is the
                       classification signal (replaces old Utdanningsniva text).
  - og:image is reliable as an image fallback on the new site.
ID strategy: Product-type pages keep sku (PG-xxxx); Course-type pages use the
URL path as id (e.g. "kurs/trening-som-medisin").

2026-09-14 FIX -- header widget: every page's main nav renders a "campaign"
widget with Foer/Naa prices for 2-3 UNRELATED products BEFORE the <h1>. The
Foer/Naa search is therefore scoped to text AFTER the <h1> (_text_after_h1).

2026-11-10 FIX -- permanent price cuts were treated as sales: the old logic
flagged ANY price below the stored baseline as a sale. NKI also lowers prices
permanently (e.g. Norsk paabygging 6500 -> 5950, no campaign shown on the
page), which must NOT produce sale_price. New rule (resolve_price): a sale
exists ONLY while the product page itself shows a Foer/Naa pair
(regular_hint > current price). Otherwise the current price IS the regular
price. price_history.json now only remembers when an active sale started
(drop_since) so the effective-date window start stays stable.
=============================================================================

Field schema note: feed shaped to match a Hunch/Meta reference feed -- bare
custom_label_0/1/2, fb_product_category, feed_name, internal_label, plus
g:-namespaced item_group_id, text google_product_category, single-value
product_type.

fb_product_category: Meta taxonomy value (separate from Google's). Default text
"Interests > Education > Distance education"; Robin also has a numeric
override rule on the Hunch side.

Other design notes:
- Lanekassen eligibility intentionally NOT scraped/emitted (Robin's call).
- Availability derived from HTTP status of the page GET.
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

SALE_WINDOW_DAYS = 30  # rolling window while a sale is active

FEED_TITLE = "NKI Nettstudier — Kurs og utdanning"
FEED_LINK = "https://www.nki.no"
FEED_DESCRIPTION = "Produktfeed for NKI Nettstudier. Nettstudier med fleksibel oppstart."
BRAND = "NKI"
DEFAULT_IMAGE = f"{BASE_URL}/assets/images/og-default.jpg"

FB_PRODUCT_CATEGORY = "Interests > Education > Distance education"

# Belt-and-suspenders only -- new site 404s discontinued courses.
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

LEVEL_TO_ENTITY_TYPE = {
    "kurs": "kurs",
    "enkeltemner": "enkeltemner",
    "fagskole": "fagskole",
}
VGO_SUBJECT_TO_ENTITY_TYPE = {
    "yrkesfag": "yrkesfag",
    "praksiskandidat": "yrkesfag",
    "enkeltfag": "vgo_teori",
    "studiekompetanse": "vgo_teori",
    "realfag": "vgo_teori",
}
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
# Page parsing
# --------------------------------------------------------------------------- #

_FACT_RE_TEMPLATE = r"{label}:\s*\n?\s*([^\n]+)"
# "Før" and "Nå" appear in either order, format "5 490 kr". Matched
# independently so order doesn't matter.
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
    """Returns (level, subject, category_label) from the 2nd breadcrumb item."""
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
    deliberately EXCLUDES the site header/nav, which renders before the <h1>
    and contains a "campaign" widget with unrelated products' Foer/Naa prices.
    Falls back to full-page text if no <h1> is found.
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
    Detects an active 'Foer/Naa' sale: crossed-out "Før X kr" plus current
    "Nå Y kr", in either order. offers.price in JSON-LD already reflects the
    discounted price. Caller must pass text scoped to the current product
    (see _text_after_h1).
    """
    foer_match = _PRICE_FOER_RE.search(text)
    naa_match = _PRICE_NAA_RE.search(text)
    if foer_match and naa_match:
        before = _parse_price_nok(foer_match.group(1))
        now = _parse_price_nok(naa_match.group(1))
        return {"current": now, "regular_dom": before}
    return {"current": None, "regular_dom": None}


def _parse_iso8601_duration(value: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """'PT250H' -> (months≈1, "250 timer")."""
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
    """Guaranteed non-empty description (Meta/Hunch reject empty g:description)."""
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
    item_id = sku or path  # URL path used as id for Course-type pages (no sku)

    image_link = product.get("image")
    if not image_link:
        og_image = soup.find("meta", attrs={"property": "og:image"})
        image_link = og_image["content"].strip() if og_image and og_image.get("content") else None

    offers = product.get("offers") or {}
    dl_price = offers.get("price")

    # Scoped to text AFTER <h1> only -- see _text_after_h1 / 2026-09-14 note.
    price_info = _extract_price_info(_text_after_h1(soup))
    notes = []
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
    Returns (price, sale_price, sale_price_effective_date). Mutates `history`.

    A sale exists ONLY while the product page itself shows a Foer/Naa pair
    (regular_hint > current_price). NKI also lowers prices permanently with no
    campaign shown -- those must NOT produce sale_price; the current price
    simply becomes the regular price. history stores only when an active sale
    started (drop_since), so the effective-date window start stays stable
    across runs; baseline mirrors the regular price for reference.
    """
    entry = history.get(product_id)

    if regular_hint is not None and regular_hint > current_price:
        start = today.isoformat()
        if entry and entry.get("drop_since"):
            start = entry["drop_since"]
        history[product_id] = {"baseline": regular_hint, "drop_since": start}
        end = today + timedelta(days=SALE_WINDOW_DAYS)
        effective_date = f"{start}T00:00+0100/{end.isoformat()}T23:59+0100"
        return regular_hint, current_price, effective_date

    history[product_id] = {"baseline": current_price, "drop_since": None}
    return current_price, None, None


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
