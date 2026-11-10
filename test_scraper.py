"""
Sanity checks against real page structure captured from nki.no (new site,
2026-09). Extended 2026-09-14 (header-widget fix) and 2026-11-10 (permanent
price cuts must not become sale_price).
"""

import json
from datetime import date, timedelta

import scraper


def _script(payload_dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(payload_dict)}</script>'


def _page(ld_blocks, before_h1_extra="", after_h1_extra="", h1_text=None):
    name = h1_text
    if name is None:
        for block in ld_blocks:
            if block.get("@type") in ("Course", "Product"):
                name = block.get("name", "Product")
                break
    scripts = "\n".join(_script(b) for b in ld_blocks)
    return f"""
<html><head>{scripts}</head>
<body>
<header>{before_h1_extra}</header>
<main><h1>{name}</h1>{after_h1_extra}</main>
</body></html>
"""


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


COURSE_LD = {
    "@type": "Course",
    "name": "Trening som medisin",
    "description": "Kurset Trening som medisin gir deg treningsfysiologien bak anbefalingene.",
    "offers": {"@type": "Offer", "price": 9900, "priceCurrency": "NOK"},
    "hasCourseInstance": {"@type": "CourseInstance", "courseWorkload": "PT250H"},
}
COURSE_BREADCRUMB = {
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://www.nki.no/"},
        {"@type": "ListItem", "position": 2, "name": "Helse og livsstil", "item": "https://www.nki.no/kurs?level=kurs"},
    ],
}
PRODUCT_LD = {
    "@type": "Product",
    "name": "Medisinsk sekretær",
    "description": "Bli medisinsk sekretær.",
    "sku": "PG-0001745",
    "image": "https://cdn.sanity.io/images/nki/medisinsk-sekretaer.jpg",
    "offers": {"@type": "Offer", "price": 54900, "priceCurrency": "NOK"},
}
PRODUCT_BREADCRUMB = {
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://www.nki.no/"},
        {"@type": "ListItem", "position": 2, "name": "Fagskole", "item": "https://www.nki.no/fagskole?level=fagskole"},
    ],
}
ENKELTFAG_LD = {
    "@type": "Course",
    "name": "Biologi 2 (REA3036/3037)",
    "description": "Enkeltfag Biologi 2.",
    "offers": {"@type": "Offer", "price": 5490, "priceCurrency": "NOK"},
}
ENKELTFAG_BREADCRUMB = {
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://www.nki.no/"},
        {"@type": "ListItem", "position": 2, "name": "Spesiell studiekompetanse",
         "item": "https://www.nki.no/videregaende?level=vgo&subject=enkeltfag"},
    ],
}
YRKESFAG_LD = {
    "@type": "Course",
    "name": "Kommunikasjon og samhandling for ambulansefag",
    "description": "Yrkesfaglig enkeltfag.",
    "offers": {"@type": "Offer", "price": 4900, "priceCurrency": "NOK"},
}
YRKESFAG_BREADCRUMB = {
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://www.nki.no/"},
        {"@type": "ListItem", "position": 2, "name": "Yrkesfag på videregående",
         "item": "https://www.nki.no/videregaende?level=vgo&subject=yrkesfag"},
    ],
}
NO_DESC_LD = {
    "@type": "Product", "name": "Saksbehandler", "description": "", "sku": "PG-0000259",
    "offers": {"@type": "Offer", "price": 39500, "priceCurrency": "NOK"},
}
DISCONTINUED_HTML = "<html><body><h1>Tannlegeassistent</h1><div>Dette studiet er ikke lengre aktivt.</div></body></html>"

# --- Course / Product parsing ---
raw = scraper.parse_product_page("https://www.nki.no/kurs/trening-som-medisin", _page([COURSE_LD, COURSE_BREADCRUMB]))
check("Course: id falls back to URL path", raw["id"] == "kurs/trening-som-medisin")
check("Course: price from offers.price", raw["price"] == 9900)
check("Course: no regular_hint without sale markup", raw["regular_hint"] is None)
check("Course: duration PT250H -> 250 timer", raw["duration_text"] == "250 timer")
check("Course: category from breadcrumb", raw["category"] == "Helse og livsstil")

raw2 = scraper.parse_product_page("https://www.nki.no/fagskole/medisinsk-sekretaer", _page([PRODUCT_LD, PRODUCT_BREADCRUMB]))
check("Product: id uses sku", raw2["id"] == "PG-0001745")
check("Product: image from JSON-LD", raw2["image_link"].endswith("medisinsk-sekretaer.jpg"))
check("Product: entity_type fagskole", scraper.classify_entity_type(raw2["level"], raw2["subject"], "x") == "fagskole")

raw3 = scraper.parse_product_page("https://www.nki.no/videregaende/enkeltfag/biologi-2", _page([ENKELTFAG_LD, ENKELTFAG_BREADCRUMB]))
check("Academic enkeltfag -> vgo_teori", scraper.classify_entity_type(raw3["level"], raw3["subject"], "x") == "vgo_teori")
raw4 = scraper.parse_product_page("https://www.nki.no/videregaende/enkeltfag/amb", _page([YRKESFAG_LD, YRKESFAG_BREADCRUMB]))
check("Vocational enkeltfag -> yrkesfag", scraper.classify_entity_type(raw4["level"], raw4["subject"], "x") == "yrkesfag")

check("Discontinued page skipped", scraper.parse_product_page("https://www.nki.no/kurs/t", DISCONTINUED_HTML) is None)

raw_nd = scraper.parse_product_page("https://www.nki.no/fagskole/saksbehandler", _page([NO_DESC_LD]))
check("Empty description -> empty raw + note", raw_nd["description"] == "" and any("fallback" in n for n in raw_nd["notes"]))
check("Fallback description non-empty and mentions NKI", "NKI" in scraper._fallback_description("X", "kurs", "Y"))

# --- Header widget must not be mistaken for the product's own sale ---
HEADER_WIDGET_HTML = """
<div><a><del><span class="sr-only">Før</span>35 900 kr</del>
<span><span class="sr-only">Nå</span>30 515 kr</span></a></div>
"""
NO_SALE_LD = dict(COURSE_LD, name="AI i arbeidslivet", offers={"@type": "Offer", "price": 3900, "priceCurrency": "NOK"})
raw_ns = scraper.parse_product_page("https://www.nki.no/kurs/ai", _page([NO_SALE_LD, COURSE_BREADCRUMB], before_h1_extra=HEADER_WIDGET_HTML))
check("Header widget Foer/Naa (before h1) ignored", raw_ns["regular_hint"] is None and raw_ns["price"] == 3900)

SALE_LD = dict(ENKELTFAG_LD, offers={"@type": "Offer", "price": 4667, "priceCurrency": "NOK"})
raw_sale = scraper.parse_product_page(
    "https://www.nki.no/videregaende/enkeltfag/biologi-2",
    _page([SALE_LD, ENKELTFAG_BREADCRUMB], before_h1_extra=HEADER_WIDGET_HTML,
          after_h1_extra="<div>Nå\n4 667 kr\nFør\n5 490 kr\n-14%</div>"),
)
check("Own Foer/Naa after h1 detected", raw_sale["price"] == 4667 and raw_sale["regular_hint"] == 5490)

# --- resolve_price: sale only when the page shows Foer/Naa ---
today = date(2026, 11, 10)
history = {}
p, s, e = scraper.resolve_price("a", 5490, history, today, regular_hint=None)
check("No sale markup: plain price, no sale_price", p == 5490 and s is None and e is None)

p, s, e = scraper.resolve_price("a", 4667, history, today, regular_hint=5490)
check("Active Foer/Naa: price=regular, sale_price=current", p == 5490 and s == 4667)
check("Effective date starts today", e.startswith("2026-11-10") and e.endswith("2026-12-10T23:59+0100"))
check("drop_since recorded", history["a"]["drop_since"] == "2026-11-10")

later = today + timedelta(days=5)
p, s, e = scraper.resolve_price("a", 4667, history, later, regular_hint=5490)
check("Continued sale keeps original start", e.startswith("2026-11-10") and e.endswith("2026-12-15T23:59+0100"))

p, s, e = scraper.resolve_price("a", 5490, history, later, regular_hint=None)
check("Sale over (markup gone): back to plain price", p == 5490 and s is None)
check("drop_since cleared", history["a"]["drop_since"] is None)

# THE BUG (2026-11-10): permanent price cut with no campaign markup
perm = {"naturfag": {"baseline": 6500.0, "drop_since": "2026-09-29"}}
p, s, e = scraper.resolve_price("naturfag", 5950, perm, today, regular_hint=None)
check("Permanent price cut (no Foer/Naa): NO sale_price", s is None and e is None)
check("Permanent price cut: new price is the regular price", p == 5950)
check("Stale drop_since cleared", perm["naturfag"]["drop_since"] is None and perm["naturfag"]["baseline"] == 5950)

# --- helpers ---
check("price_tier", scraper.price_tier(7900) == "5000_15000" and scraper.price_tier(3900) == "under_5000")
check("duration_tier", scraper.duration_tier(3) == "kort" and scraper.duration_tier(12) == "medium" and scraper.duration_tier(24) == "lang")
check("smart_title", scraper.smart_title("T", "kurs", "HR og ledelse") == "T − kurs i hr og ledelse")

# --- XML spot-check ---
p1 = scraper.Product(
    id="PG-0001534", title="Advokatsekretær", description="Bli advokatsekretær.",
    link="https://www.nki.no/fagskole/advokatsekretaer", image_link="https://www.nki.no/img.jpg",
    in_stock=True, price=81500.0, sale_price=61125.0,
    sale_price_effective_date="2026-11-10T00:00+0100/2026-12-10T23:59+0100",
    category="Jus og administrasjon", entity_type="fagskole", duration_months=24, duration_text="24 måneder",
)
item = scraper.build_feed_xml([p1]).getroot().find("channel/item")
NS = {"g": scraper.NS_G}
check("g:price", item.find("g:price", NS).text == "81500.00 NOK")
check("g:sale_price", item.find("g:sale_price", NS).text == "61125.00 NOK")
check("fb_product_category fixed value", item.find("fb_product_category").text == "Interests > Education > Distance education")

print("\nAll checks passed.")
