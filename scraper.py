#!/usr/bin/env python3
"""
NKI Nettstudier product feed generator.

Crawls nki.no via sitemap.xml, extracts product data from each course/program
page (GTM dataLayer + on-page facts box), classifies it, tracks price history
to detect real sale prices, checks availability, and writes an RSS 2.0 feed
to docs/feed.xml.

Field schema note (2026-07-08): the feed is deliberately shaped to match a
Hunch/Meta-oriented reference feed Robin uses for ad templates -- bare
(un-namespaced) custom_label_0/1/2, fb_product_category, feed_name,
internal_label, plus g:-namespaced item_group_id, a text-based
google_product_category, and a single-value product_type. This means
custom_label_0/1/2 are NOT in Google's g: namespace here, so Google Merchant
Center will not recognize them as shopping custom labels (it'll just ignore
the bare tags) -- that's a known, accepted tradeoff for matching Hunch
without remapping. The nki:* namespace fields, g:ads_redirect and
g:sale_price_effective_date are kept as bonus/compliance fields even though
they weren't in the reference example, since extra fields don't break
template matching, only missing ones would.

Other design notes / known simplifications (see conversation with Robin):
- Source of truth for id/price/category is the inline GTM dataLayer
  'productDetailView' push on each page (regex + json.loads, no headless
  browser needed -- nki.no is server-rendered by Enonic CMS).
- custom_label_0 / nki:entity_type comes straight from the page's
  "Utdanningsniva:" facts field, normalized to: kurs, enkeltemner, yrkesfag,
  vgo_teori, fagskole. There is no separate "vgo" rollup value -- Robin
  filters yrkesfag + vgo_teori together in Meta Ads when she wants the full
  VGO picture.
- Category (custom_label_1 / fb_product_category / nki:category /
  g:product_type) is the dataLayer's own category string. Some pages tag a
  course with multiple comma-separated categories -- we take the first as
  primary and log the rest (see _primary_category).
- sale_price is only ever emitted when the scraped price is LOWER than the
  persisted baseline in data/price_history.json. First run establishes
  baselines with no sale_price anywhere (nothing to compare against yet).
- Lanekassen eligibility (custom_label_4 in the original spec) is
  intentionally NOT scraped/emitted, per Robin's call, even though
  "Finansiering: Lanekassegodkjent" is a scrapeable field (LANEKASSEN_TEXT
  below) -- trivial to wire up later.
- Availability is derived from the HTTP status of the same GET used to scrape
  the page (200 -> in stock, anything else -> out of stock), rather than a
  second HEAD request -- same result, half the requests.
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
from urllib.parse import urlencode

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
BRAND = "NKI"  # matches the Hunch-oriented reference feed (was "NKI Nettstudier")

# Display label per entity_type -- used for custom_label_0, the
# google_product_category text path, and the smart-title suffix. Keep in sync
# with ENTITY_TYPE_MAP's target values below.
ENTITY_TYPE_DISPLAY: dict[str, str] = {
    "kurs": "Kurs",
    "enkeltemner": "Enkeltemner",
    "yrkesfag": "Yrkesfag",
    "vgo_teori": "Videregående",
    "fagskole": "Fagskole",
}

# Only crawl sitemap URLs under these prefixes -- everything else (blogg,
# om-nki, kampanjer, ...) is not a sellable course/program.
ALLOWED_PREFIXES = (
    "/kurs/",
    "/enkeltemner/",
    "/fagskole/",
    "/videregaende/",
)
# Index/listing pages that live under an allowed prefix but aren't products.
# Belt-and-suspenders on top of the "must have a dataLayer product" check.
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

# "Utdanningsniva:" facts-box value -> custom_label_0 / nki:entity_type
ENTITY_TYPE_MAP = {
    "kurs": "kurs",
    "enkeltemner": "enkeltemner",
    "enkeltemne": "enkeltemner",
    "yrkesfag": "yrkesfag",
    "praksiskandidat": "yrkesfag",
    "enkeltfag": "vgo_teori",
    "studiekompetanse": "vgo_teori",
    "realfag": "vgo_teori",
    "fagskole": "fagskole",
}
# Fallback if the facts-box value is missing or unrecognized: guess from URL.
URL_FALLBACK_ENTITY_TYPE = (
    ("/fagskole/", "fagskole"),
    ("/videregaende/yrkesfag/", "yrkesfag"),
    ("/videregaende/enkeltfag/", "vgo_teori"),
    ("/videregaende/studiekompetanse/", "vgo_teori"),
    ("/videregaende/realfag/", "vgo_teori"),
    ("/kurs/", "kurs"),
    ("/enkeltemner/", "enkeltemner"),
)

LANEKASSEN_TEXT = "Lånekassegodkjent"  # present in DOM; not wired up yet (see module docstring)

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
    price: float  # regular/baseline NOK, always populated
    sale_price: Optional[float]
    sale_price_effective_date: Optional[str]
    category: str  # raw site category (primary, after splitting multi-category values)
    entity_type: str  # kurs / enkeltemner / yrkesfag / vgo_teori / fagskole
    duration_months: Optional[int]
    duration_text: Optional[str]
    notes: list[str] = field(default_factory=list)  # scrape warnings, for the run summary


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

_PRODUCTS_RE = re.compile(r"'products'\s*:\s*(\[.*?\])", re.DOTALL)
_FACT_RE_TEMPLATE = r"{label}:\s*\n?\s*([^\n]+)"


def _extract_datalayer_product(html: str) -> Optional[dict]:
    match = _PRODUCTS_RE.search(html)
    if not match:
        return None
    try:
        products = json.loads(match.group(1))
    except json.JSONDecodeError:
        log.warning("Found 'products' block but could not parse JSON: %s", match.group(1)[:200])
        return None
    if not products:
        return None
    return products[0]


def _extract_fact(text: str, label: str) -> Optional[str]:
    pattern = _FACT_RE_TEMPLATE.format(label=re.escape(label))
    match = re.search(pattern, text)
    return match.group(1).strip() if match else None


def _parse_price_nok(text: str) -> Optional[float]:
    """'kr 7 900,-' -> 7900.0"""
    digits = re.sub(r"[^\d]", "", text)
    return float(digits) if digits else None


def _parse_duration_months(text: str) -> Optional[int]:
    """'3 maneder' -> 3"""
    match = re.search(r"(\d+)", text)
    return int(match.group(1)) if match else None


def _primary_category(raw_category: str) -> str:
    """
    Some dataLayer entries tag a course with multiple categories, comma
    separated, e.g. 'HR og ledelse, Jus og administrasjon'. We only have room
    for one category value downstream, so take the first as primary and log
    the rest so Robin can see what's being dropped.
    """
    if not raw_category:
        return raw_category
    parts = [p.strip() for p in raw_category.split(",") if p.strip()]
    if len(parts) > 1:
        log.info("Multi-category value %r, using primary %r (dropped: %s)", raw_category, parts[0], parts[1:])
    return parts[0] if parts else raw_category


def classify_entity_type(utdanningsniva: Optional[str], path: str) -> str:
    if utdanningsniva:
        key = utdanningsniva.strip().lower()
        if key in ENTITY_TYPE_MAP:
            return ENTITY_TYPE_MAP[key]
        log.warning("Unrecognized Utdanningsniva value %r for %s, falling back to URL", utdanningsniva, path)
    for prefix, entity_type in URL_FALLBACK_ENTITY_TYPE:
        if path.startswith(prefix):
            return entity_type
    log.warning("Could not classify entity_type for %s, defaulting to 'kurs'", path)
    return "kurs"


def parse_product_page(url: str, html: str) -> Optional[dict]:
    """Returns a raw field dict, or None if this isn't a product page."""
    product = _extract_datalayer_product(html)
    if product is None:
        return None

    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")

    title_tag = soup.find("h1")
    title = title_tag.get_text(strip=True) if title_tag else product.get("name", "")

    meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    description = meta_desc["content"].strip() if meta_desc and meta_desc.get("content") else ""

    og_image = soup.find("meta", attrs={"property": "og:image"})
    image_link = og_image["content"].strip() if og_image and og_image.get("content") else ""

    utdanningsniva = _extract_fact(text, "Utdanningsnivå")
    studietilgang = _extract_fact(text, "Studietilgang")
    pris_text = _extract_fact(text, "Pris")

    dom_price = _parse_price_nok(pris_text) if pris_text else None
    dl_price = product.get("price")
    price = float(dl_price) if dl_price is not None else dom_price

    notes = []
    if dom_price is not None and dl_price is not None and abs(dom_price - float(dl_price)) > 0.5:
        notes.append(f"Price mismatch: dataLayer={dl_price} DOM={dom_price}, used dataLayer")

    return {
        "id": product.get("id"),
        "title": title,
        "description": description,
        "image_link": image_link,
        "category": _primary_category(product.get("category", "")),
        "price": price,
        "utdanningsniva": utdanningsniva,
        "duration_text": studietilgang,
        "duration_months": _parse_duration_months(studietilgang) if studietilgang else None,
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


def resolve_price(product_id: str, current_price: float, history: dict, today: date) -> tuple[float, Optional[float], Optional[str]]:
    """
    Returns (price, sale_price, sale_price_effective_date).
    Mutates `history` in place.
    """
    entry = history.get(product_id)

    if entry is None:
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
        # New regular price -- not a sale, just a price change.
        entry["baseline"] = current_price
        entry["drop_since"] = None
        return current_price, None, None

    # current_price == baseline
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

        # Even for a non-200, NKI may serve a body (soft 404); try to parse
        # anyway so we don't drop items that are just temporarily flaky.
        raw = parse_product_page(url, resp.text)
        if raw is None:
            skipped_non_product += 1
            log.info("No product data found on %s, treating as a listing page (skipped)", url)
            continue

        if not raw["id"] or raw["price"] is None:
            log.warning("Incomplete product data on %s (id=%r price=%r), skipping", url, raw["id"], raw["price"])
            continue

        path = url.replace(BASE_URL, "")
        entity_type = classify_entity_type(raw["utdanningsniva"], path)
        price, sale_price, effective_date = resolve_price(raw["id"], raw["price"], history, today)

        products.append(
            Product(
                id=raw["id"],
                title=raw["title"],
                description=raw["description"],
                link=url,
                image_link=raw["image_link"] or f"{BASE_URL}/assets/images/og-default.jpg",
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
    """'Advokatsekretaer' -> 'Advokatsekretaer − fagskole i jus og administrasjon'"""
    if not category:
        return title
    return f"{title} − {entity_display(entity_type).lower()} i {category.lower()}"


def google_product_category_path(entity_type: str, category: str) -> str:
    """'fagskole', 'Jus og administrasjon' -> 'utdanning > fagskole > jus og administrasjon'"""
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

        # --- bare (un-namespaced) fields, matching the Hunch reference feed ---
        _sub(item, "custom_label_0", display_type)
        _sub(item, "custom_label_1", p.category)
        _sub(item, "custom_label_2", duration_tier(p.duration_months))  # bonus, extends the pattern
        _sub(item, "fb_product_category", p.category)
        _sub(item, "feed_name", p.title)
        _sub(item, "internal_label", p.title)

        # --- g:-namespaced standard fields ---
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

        # --- nki:-namespaced bonus fields ---
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
