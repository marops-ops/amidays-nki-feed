"""
Sanity checks against real page structure captured from nki.no (rewritten
2026-09-12 for the new Next.js/Sanity/JSON-LD site, extended 2026-09-14 for
the header-widget sale-price fix). Not a full test suite -- just enough to
catch parsing/regex mistakes before running the scraper against the live
site from GitHub Actions.
"""

import scraper


def _script(payload_dict) -> str:
    import json
    return f'<script type="application/ld+json">{json.dumps(payload_dict)}</script>'


def _page(ld_blocks, before_h1_extra="", after_h1_extra="", h1_text=None, meta_extra=""):
    """
    Builds minimal but structurally realistic HTML: <head> with meta tags,
    then BEFORE the <h1> some optional extra markup (used to simulate the
    real site's header/nav "campaign widget" that sits before the product
    content), then the <h1>, then optional extra markup AFTER it (used to
    simulate the product's own real price block).
    """
    name = h1_text
    if name is None:
        for block in ld_blocks:
            if block.get("@type") in ("Course", "Product"):
                name = block.get("name", "Product")
                break
    scripts = "\n".join(_script(b) for b in ld_blocks)
    return f"""
<html>
<head>
{meta_extra}
{scripts}
</head>
<body>
<header>
{before_h1_extra}
</header>
<main>
<h1>{name}</h1>
{after_h1_extra}
</main>
</body>
</html>
"""


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(1)


# --------------------------------------------------------------------------- #
# Fixtures: JSON-LD blocks matching the two real shapes on nki.no
# --------------------------------------------------------------------------- #

COURSE_LD = {
    "@type": "Course",
    "name": "Trening som medisin",
    "description": "Kurset Trening som medisin gir deg treningsfysiologien bak anbefalingene.",
    "url": "https://www.nki.no/kurs/trening-som-medisin",
    "provider": {"@type": "Organization", "name": "NKI Nettstudier"},
    "offers": {"@type": "Offer", "price": 9900, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"},
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
    "description": "Bli medisinsk sekretær med denne fagskolepakken.",
    "url": "https://www.nki.no/fagskole/medisinsk-sekretaer",
    "sku": "PG-0001745",
    "image": "https://cdn.sanity.io/images/nki/medisinsk-sekretaer.jpg",
    "offers": {"@type": "Offer", "price": 54900, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"},
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
    "description": "Enkeltfag Biologi 2 på videregående nivå.",
    "url": "https://www.nki.no/videregaende/enkeltfag/biologi-2",
    "offers": {"@type": "Offer", "price": 5490, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"},
}
ENKELTFAG_BREADCRUMB = {
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://www.nki.no/"},
        {
            "@type": "ListItem",
            "position": 2,
            "name": "Spesiell studiekompetanse",
            "item": "https://www.nki.no/videregaende?level=vgo&subject=enkeltfag",
        },
    ],
}

YRKESFAG_LD = {
    "@type": "Course",
    "name": "Kommunikasjon og samhandling for ambulansefag",
    "description": "Yrkesfaglig enkeltfag for ambulansefag.",
    "url": "https://www.nki.no/videregaende/enkeltfag/ambulansemedisin",
    "offers": {"@type": "Offer", "price": 4900, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"},
}
YRKESFAG_BREADCRUMB = {
    "@type": "BreadcrumbList",
    "itemListElement": [
        {"@type": "ListItem", "position": 1, "name": "Hjem", "item": "https://www.nki.no/"},
        {
            "@type": "ListItem",
            "position": 2,
            "name": "Yrkesfag på videregående",
            "item": "https://www.nki.no/videregaende?level=vgo&subject=yrkesfag",
        },
    ],
}

NO_DESC_LD = {
    "@type": "Product",
    "name": "Saksbehandler",
    "description": "",
    "url": "https://www.nki.no/fagskole/saksbehandler",
    "sku": "PG-0000259",
    "offers": {"@type": "Offer", "price": 39500, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"},
}

DISCONTINUED_HTML = """
<html>
<head></head>
<body>
<h1>Tannlegeassistent</h1>
<div>Dette studiet er ikke lengre aktivt. Finn ditt studium under Studievelger.</div>
</body>
</html>
"""

# --------------------------------------------------------------------------- #
# Basic Course parsing
# --------------------------------------------------------------------------- #

COURSE_HTML = _page([COURSE_LD, COURSE_BREADCRUMB])
raw = scraper.parse_product_page("https://www.nki.no/kurs/trening-som-medisin", COURSE_HTML)
check("Course: id falls back to URL path (no sku)", raw["id"] == "kurs/trening-som-medisin")
check("Course: title from JSON-LD name", raw["title"] == "Trening som medisin")
check("Course: description from JSON-LD", "treningsfysiologien" in raw["description"])
check("Course: price from offers.price", raw["price"] == 9900)
check("Course: no regular_hint when no sale markup present", raw["regular_hint"] is None)
check("Course: duration parsed from ISO8601 (PT250H)", raw["duration_text"] == "250 timer")
check("Course: duration_months >= 1", raw["duration_months"] == 1)
check("Course: level/subject from breadcrumb", raw["level"] == "kurs")
check("Course: category from breadcrumb 2nd item", raw["category"] == "Helse og livsstil")

# --------------------------------------------------------------------------- #
# Basic Product (fagskole/pakke) parsing
# --------------------------------------------------------------------------- #

PRODUCT_HTML = _page([PRODUCT_LD, PRODUCT_BREADCRUMB])
raw2 = scraper.parse_product_page("https://www.nki.no/fagskole/medisinsk-sekretaer", PRODUCT_HTML)
check("Product: id uses sku (PG-xxxx), not URL", raw2["id"] == "PG-0001745")
check("Product: image from JSON-LD image field", raw2["image_link"] == "https://cdn.sanity.io/images/nki/medisinsk-sekretaer.jpg")
check("Product: price from offers.price", raw2["price"] == 54900)

entity2 = scraper.classify_entity_type(raw2["level"], raw2["subject"], "fagskole/medisinsk-sekretaer")
check("Product: entity_type = fagskole", entity2 == "fagskole")

# --------------------------------------------------------------------------- #
# Breadcrumb-based VGO disambiguation (yrkesfag vs vgo_teori)
# --------------------------------------------------------------------------- #

ENKELTFAG_HTML = _page([ENKELTFAG_LD, ENKELTFAG_BREADCRUMB])
raw3 = scraper.parse_product_page("https://www.nki.no/videregaende/enkeltfag/biologi-2", ENKELTFAG_HTML)
entity3 = scraper.classify_entity_type(raw3["level"], raw3["subject"], "videregaende/enkeltfag/biologi-2")
check("Academic enkeltfag (subject=enkeltfag) -> vgo_teori", entity3 == "vgo_teori")

YRKESFAG_HTML = _page([YRKESFAG_LD, YRKESFAG_BREADCRUMB])
raw4 = scraper.parse_product_page("https://www.nki.no/videregaende/enkeltfag/ambulansemedisin", YRKESFAG_HTML)
entity4 = scraper.classify_entity_type(raw4["level"], raw4["subject"], "videregaende/enkeltfag/ambulansemedisin")
check("Vocational enkeltfag (subject=yrkesfag), same URL prefix -> yrkesfag, not vgo_teori", entity4 == "yrkesfag")

# --------------------------------------------------------------------------- #
# Discontinued course detection
# --------------------------------------------------------------------------- #

check(
    "Discontinued marker text -> page skipped entirely",
    scraper.parse_product_page("https://www.nki.no/kurs/tannlegeassistent", DISCONTINUED_HTML) is None,
)

# --------------------------------------------------------------------------- #
# Guaranteed non-empty description fallback
# --------------------------------------------------------------------------- #

NO_DESC_HTML = _page([NO_DESC_LD])
raw_no_desc = scraper.parse_product_page("https://www.nki.no/fagskole/saksbehandler", NO_DESC_HTML)
check("Empty JSON-LD description -> raw description empty (filled in later by crawl())", raw_no_desc["description"] == "")
check("Note logged about missing description", any("fallback" in n for n in raw_no_desc["notes"]))

fallback_desc = scraper._fallback_description("Saksbehandler", "fagskole", "Jus og administrasjon")
check("Fallback description non-empty", bool(fallback_desc))
check("Fallback description mentions title", fallback_desc.startswith("Saksbehandler"))
check("Fallback description mentions NKI", "NKI" in fallback_desc)
check("Fallback with no category still non-empty", bool(scraper._fallback_description("X", "kurs", "")))
check("Fallback with no entity_type/category still non-empty", bool(scraper._fallback_description("X", None, None)))

# --------------------------------------------------------------------------- #
# 2026-09-14 FIX: header/nav "campaign widget" must NOT be mistaken for the
# current product's own sale. This reproduces the real bug found live: every
# nki.no page renders a nav widget listing 2-3 unrelated discounted packages
# (with their own "Foer"/"Naa" DOM text) BEFORE the page's own <h1>.
# --------------------------------------------------------------------------- #

HEADER_WIDGET_HTML = """
<div class="nav-campaign-widget">
  <a href="/videregaende/studiekompetanse/generell-studiekompetanse-23-5-regelen">
    <span class="type-weight-medium"><del><span class="sr-only">Før</span>35 900 kr</del>
    <span><span class="sr-only">Nå</span>30 515 kr</span></span>
  </a>
  <a href="/videregaende/yrkesfag/ambulansefag-vg2">
    <span class="type-weight-medium"><del><span class="sr-only">Før</span>39 900 kr</del>
    <span><span class="sr-only">Nå</span>33 915 kr</span></span>
  </a>
</div>
"""

# Case A: product has NO real sale of its own (offers.price is the plain
# price, no Foer/Naa after the h1) -- reproduces "AI i arbeidslivet" live.
NO_SALE_LD = dict(COURSE_LD)
NO_SALE_LD["name"] = "AI i arbeidslivet"
NO_SALE_LD["offers"] = {"@type": "Offer", "price": 3900, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"}
NO_SALE_HTML = _page([NO_SALE_LD, COURSE_BREADCRUMB], before_h1_extra=HEADER_WIDGET_HTML)
raw_no_sale = scraper.parse_product_page("https://www.nki.no/kurs/ai-i-arbeidslivet", NO_SALE_HTML)
check(
    "Header widget's unrelated Foer/Naa (before h1) is NOT picked up as this product's sale",
    raw_no_sale["regular_hint"] is None,
)
check("Price stays the real offers.price, not the header widget's 35900", raw_no_sale["price"] == 3900)

# Case B: product DOES have a real, active sale of its own, rendered AFTER
# the h1 -- reproduces "Biologi 2" live (Nå 4 667 kr / Før 5 490 kr / -14%),
# on a page that ALSO has the unrelated header widget before the h1.
SALE_LD = dict(ENKELTFAG_LD)
SALE_LD["offers"] = {"@type": "Offer", "price": 4667, "priceCurrency": "NOK", "availability": "https://schema.org/InStock"}
SALE_HTML = _page(
    [SALE_LD, ENKELTFAG_BREADCRUMB],
    before_h1_extra=HEADER_WIDGET_HTML,
    after_h1_extra="<div>Nå\n4 667 kr\nFør\n5 490 kr\n-14%</div>",
)
raw_sale = scraper.parse_product_page("https://www.nki.no/videregaende/enkeltfag/biologi-2", SALE_HTML)
check("offers.price (already discounted) used directly as the live price", raw_sale["price"] == 4667)
check(
    "regular_hint recovered from the product's OWN Foer/Naa (after h1), not the header widget's 35900",
    raw_sale["regular_hint"] == 5490,
)
check("Note logged about the active DOM sale", any("Active DOM sale" in n for n in raw_sale["notes"]))

fresh_history = {}
price, sale, eff = scraper.resolve_price(
    "videregaende/enkeltfag/biologi-2", raw_sale["price"], fresh_history, __import__("datetime").date(2026, 9, 10),
    regular_hint=raw_sale["regular_hint"],
)
check("First-ever sight of an active sale: baseline seeded to the real regular price (5490, not 35900)", price == 5490)
check("First-ever sight of an active sale: sale_price emitted immediately", sale == 4667)
check("Baseline stored correctly", fresh_history["videregaende/enkeltfag/biologi-2"]["baseline"] == 5490)

# --------------------------------------------------------------------------- #
# Price history / sale price resolution logic
# --------------------------------------------------------------------------- #

from datetime import date, timedelta

history = {}
today = date(2026, 9, 10)

price, sale, eff = scraper.resolve_price("kurs/trening-som-medisin", 9900, history, today, regular_hint=None)
check("First run with no sale: unaffected by the regular_hint fix", sale is None and price == 9900)
check("Baseline stored", history["kurs/trening-som-medisin"]["baseline"] == 9900)

price, sale, eff = scraper.resolve_price("kurs/trening-som-medisin", 7425, history, today)
check("Price drop detected", sale == 7425 and price == 9900)
check("Effective_date starts today", eff.startswith("2026-09-10"))
check("drop_since recorded", history["kurs/trening-som-medisin"]["drop_since"] == "2026-09-10")

later = today + timedelta(days=5)
price, sale, eff = scraper.resolve_price("kurs/trening-som-medisin", 7425, history, later)
check("drop_since NOT reset on continued sale", history["kurs/trening-som-medisin"]["drop_since"] == "2026-09-10")
check("Effective_date end rolls forward", eff.endswith("2026-10-15T23:59+0100"))

price, sale, eff = scraper.resolve_price("kurs/trening-som-medisin", 9900, history, later)
check("Price back to baseline: no sale", sale is None and price == 9900)

price, sale, eff = scraper.resolve_price("kurs/trening-som-medisin", 10900, history, later)
check("Price increase becomes new baseline", sale is None and price == 10900)
check("Baseline updated", history["kurs/trening-som-medisin"]["baseline"] == 10900)

# --------------------------------------------------------------------------- #
# Tiering / display helpers
# --------------------------------------------------------------------------- #

check("price_tier 7900 = 5000_15000", scraper.price_tier(7900) == "5000_15000")
check("price_tier 3900 = under_5000", scraper.price_tier(3900) == "under_5000")
check("duration_tier 3mnd = kort", scraper.duration_tier(3) == "kort")
check("duration_tier 12mnd = medium", scraper.duration_tier(12) == "medium")
check("duration_tier 24mnd = lang", scraper.duration_tier(24) == "lang")
check("entity_display kurs = Kurs", scraper.entity_display("kurs") == "Kurs")
check(
    "smart_title suffix",
    scraper.smart_title("Trening som medisin", "kurs", "Helse og livsstil") == "Trening som medisin − kurs i helse og livsstil",
)
check(
    "google_product_category path",
    scraper.google_product_category_path("fagskole", "Jus og administrasjon") == "utdanning > fagskole > jus og administrasjon",
)

# --------------------------------------------------------------------------- #
# Full XML build, spot-check field structure
# --------------------------------------------------------------------------- #

p1 = scraper.Product(
    id="PG-0001534", title="Advokatsekretær", description="Bli advokatsekretær.",
    link="https://www.nki.no/fagskole/advokatsekretaer", image_link="https://www.nki.no/img.jpg",
    in_stock=True, price=81500.0, sale_price=61125.0,
    sale_price_effective_date="2026-07-08T00:00+0100/2026-08-07T23:59+0100",
    category="Jus og administrasjon", entity_type="fagskole", duration_months=24, duration_text="24 måneder",
)
tree = scraper.build_feed_xml([p1])
item = tree.getroot().find("channel/item")
NS = {"g": scraper.NS_G, "nki": scraper.NS_NKI}


def text_of(tag, ns=None):
    el = item.find(tag, ns) if ns else item.find(tag)
    return el.text if el is not None else None


check("bare custom_label_0", text_of("custom_label_0") == "Fagskole")
check("bare custom_label_1", text_of("custom_label_1") == "Jus og administrasjon")
check("fb_product_category is the fixed taxonomy value", text_of("fb_product_category") == "Interests > Education > Distance education")
check("bare feed_name (plain title)", text_of("feed_name") == "Advokatsekretær")
check("bare internal_label", text_of("internal_label") == "Advokatsekretær")
check("g:id", text_of("g:id", NS) == "PG-0001534")
check("g:item_group_id matches id", text_of("g:item_group_id", NS) == "PG-0001534")
check("g:brand is NKI", text_of("g:brand", NS) == "NKI")
check("g:title has smart suffix", text_of("g:title", NS) == "Advokatsekretær − fagskole i jus og administrasjon")
check(
    "g:google_product_category text path",
    text_of("g:google_product_category", NS) == "utdanning > fagskole > jus og administrasjon",
)
check("g:product_type is bare category", text_of("g:product_type", NS) == "Jus og administrasjon")
check("g:price", text_of("g:price", NS) == "81500.00 NOK")
check("g:sale_price", text_of("g:sale_price", NS) == "61125.00 NOK")

print("\nAll checks passed.")
