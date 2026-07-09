"""Quick sanity checks against real snippets captured from nki.no (2026-07-08).
Not a full test suite -- just enough to catch encoding/regex mistakes before
running the scraper against the live site from GitHub Actions.
"""

import scraper

SAMPLE_HTML = """
<html>
<head>
<meta property="og:description" content="Praktisk nettkurs i ledelse som gir deg kunnskapen du trenger for a bli en god leder.">
<meta property="og:image" content="https://www.nki.no/kurs/innforing-i-ledelse/_/image/abc/width-800/pic.jpg">
</head>
<body>
<script>
  dataLayer.push({
      'event': 'productDetailView',
      'ecommerce': {
          'detail': {
            'products':[{"name":"Innføring i ledelse","id":"PG-0000172","price":7900,"brand":"NKI","category":"HR og ledelse"}]
          }
      }
  });
</script>
<h1>Innføring i ledelse</h1>
<div>
Utdanningsnivå:
<span>Kurs</span>
Oppstart:
<span>Start når du vil</span>
Studietilgang:
<span>3 måneder</span>
Pris:
<span>kr 7 900,-</span>
</div>
</body>
</html>
"""

FAGSKOLE_HTML = """
<html>
<head>
<meta property="og:description" content="Bli prosjektleder med fagskoleutdanning.">
<meta property="og:image" content="https://www.nki.no/fagskole/prosjektleder/_/image/abc/width-800/pic.jpg">
</head>
<body>
<script>
  dataLayer.push({
      'event': 'productDetailView',
      'ecommerce': {
          'detail': {
            'products':[{"name":"Prosjektleder","id":"PG-0001642","price":49500,"brand":"NKI","category":"HR og ledelse, Jus og administrasjon"}]
          }
      }
  });
</script>
<h1>Prosjektleder</h1>
<div>
Utdanningsnivå:
<span>Fagskole</span>
Finansiering:
<span>Lånekassegodkjent</span>
Oppstart:
<span>Start når du vil</span>
Studietilgang:
<span>12 måneder</span>
Pris:
<span>kr 49 500,-</span>
</div>
</body>
</html>
"""

# No meta description / og:description at all -- exercises the guaranteed
# fallback description (the real-world bug: Hunch rejected products where
# this was empty with "Field value is not provided").
NO_META_HTML = """
<html>
<head>
</head>
<body>
<script>
  dataLayer.push({
      'event': 'productDetailView',
      'ecommerce': {
          'detail': {
            'products':[{"name":"Saksbehandler","id":"PG-0000259","price":39500,"brand":"NKI","category":"Jus og administrasjon"}]
          }
      }
  });
</script>
<h1>Saksbehandler</h1>
<img src="https://www.nki.no/fagskole/saksbehandler/_/image/real/block-780-780/Solfrid%20Fagskole%202026.jpg" alt="hero">
<div>
Utdanningsnivå:
<span>Fagskole</span>
Oppstart:
<span>Start når du vil</span>
Studietilgang:
<span>12 måneder</span>
Pris:
<span>kr 39 500,-</span>
</div>
</body>
</html>
"""


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        raise SystemExit(1)


raw = scraper.parse_product_page("https://www.nki.no/kurs/innforing-i-ledelse", SAMPLE_HTML)
check("dataLayer id extracted", raw["id"] == "PG-0000172")
check("dataLayer price extracted", raw["price"] == 7900)
check("dataLayer category extracted", raw["category"] == "HR og ledelse")
check("Utdanningsniva extracted", raw["utdanningsniva"] == "Kurs")
check("Studietilgang extracted", raw["duration_text"] == "3 måneder")
check("duration_months parsed", raw["duration_months"] == 3)
check("title from h1", raw["title"] == "Innføring i ledelse")
check("og:image used as fallback when no body <img>", raw["image_link"].endswith("pic.jpg"))
check("og:description captured", "ledelse" in raw["description"])

# --- hero image: must prefer the real <img> after <h1> over og:image ---
HERO_IMG_HTML = SAMPLE_HTML.replace(
    "<h1>Innføring i ledelse</h1>",
    '<h1>Innføring i ledelse</h1>\n<img src="/icons/heart.svg" alt="">'
    '\n<img src="https://www.nki.no/kurs/innforing-i-ledelse/_/image/real/block-780-780/pexels-fauxels.jpg" alt="hero">',
)
raw_hero = scraper.parse_product_page("https://www.nki.no/kurs/innforing-i-ledelse", HERO_IMG_HTML)
check(
    "hero <img> after h1 wins over og:image",
    raw_hero["image_link"] == "https://www.nki.no/kurs/innforing-i-ledelse/_/image/real/block-780-780/pexels-fauxels.jpg",
)
check("svg icon between h1 and hero photo is skipped", not raw_hero["image_link"].endswith(".svg"))

entity = scraper.classify_entity_type(raw["utdanningsniva"], "/kurs/innforing-i-ledelse")
check("entity_type = kurs", entity == "kurs")
check("price_tier 7900 = 5000_15000", scraper.price_tier(7900) == "5000_15000")
check("duration_tier 3mnd = kort", scraper.duration_tier(3) == "kort")
check("entity_display kurs = Kurs", scraper.entity_display("kurs") == "Kurs")
check(
    "smart_title suffix",
    scraper.smart_title("Innføring i ledelse", "kurs", "HR og ledelse") == "Innføring i ledelse − kurs i hr og ledelse",
)
check(
    "google_product_category path",
    scraper.google_product_category_path("kurs", "HR og ledelse") == "utdanning > kurs > hr og ledelse",
)

raw2 = scraper.parse_product_page("https://www.nki.no/fagskole/prosjektleder", FAGSKOLE_HTML)
check("fagskole id", raw2["id"] == "PG-0001642")
check("multi-category split to primary", raw2["category"] == "HR og ledelse")
entity2 = scraper.classify_entity_type(raw2["utdanningsniva"], "/fagskole/prosjektleder")
check("entity_type = fagskole", entity2 == "fagskole")
check("duration_tier 12mnd = medium", scraper.duration_tier(raw2["duration_months"]) == "medium")
check("entity_display fagskole = Fagskole", scraper.entity_display("fagskole") == "Fagskole")
check(
    "google_product_category matches Robin's reference example",
    scraper.google_product_category_path("fagskole", "Jus og administrasjon") == "utdanning > fagskole > jus og administrasjon",
)

check("Realfag maps to vgo_teori", scraper.classify_entity_type("Realfag", "/videregaende/realfag/x") == "vgo_teori")

# --- category-priority classification (real mismatches found against Robin's reference feed) ---
check(
    "Enkeltfag + 'Yrkesfag paa videregaende' category -> yrkesfag (not vgo_teori)",
    scraper.classify_entity_type("Enkeltfag", "/videregaende/enkeltfag/ambulansemedisin", "Yrkesfag på videregående") == "yrkesfag",
)
check(
    "Enkeltfag + 'Spesiell studiekompetanse' category -> vgo_teori",
    scraper.classify_entity_type("Enkeltfag", "/videregaende/enkeltfag/biologi-1", "Spesiell studiekompetanse") == "vgo_teori",
)
check(
    "Kurs-URL item with VGO category still classified as vgo_teori",
    scraper.classify_entity_type("Kurs", "/kurs/forkurs-ingenior-realfagskurs", "Spesiell studiekompetanse") == "vgo_teori",
)
check(
    "No category override: Utdanningsniva still used normally",
    scraper.classify_entity_type("Kurs", "/kurs/innforing-i-ledelse", "HR og ledelse") == "kurs",
)

# --- guaranteed non-empty description (the real Hunch bug: "Field value is not provided") ---
raw_no_meta = scraper.parse_product_page("https://www.nki.no/fagskole/saksbehandler", NO_META_HTML)
check("no meta description -> raw description is empty (filled in later in crawl())", raw_no_meta["description"] == "")
check("note logged about missing description", any("fallback" in n for n in raw_no_meta["notes"]))

fallback_desc = scraper._fallback_description("Saksbehandler", "fagskole", "Jus og administrasjon")
check("fallback description is non-empty", bool(fallback_desc))
check("fallback description mentions title", fallback_desc.startswith("Saksbehandler"))
check("fallback description mentions NKI", "NKI" in fallback_desc)

check("fallback with no category still non-empty", bool(scraper._fallback_description("X", "kurs", "")))
check("fallback with no entity_type/category still non-empty", bool(scraper._fallback_description("X", None, None)))

# --- price history / sale price logic ---
from datetime import date, timedelta

history = {}
today = date(2026, 7, 8)

price, sale, eff = scraper.resolve_price("PG-0000172", 7900, history, today)
check("first run: no sale_price", sale is None and price == 7900)
check("baseline stored", history["PG-0000172"]["baseline"] == 7900)

price, sale, eff = scraper.resolve_price("PG-0000172", 5925, history, today)
check("price drop detected", sale == 5925 and price == 7900)
check("effective_date starts today", eff.startswith("2026-07-08"))
check("drop_since recorded", history["PG-0000172"]["drop_since"] == "2026-07-08")

later = today + timedelta(days=5)
price, sale, eff = scraper.resolve_price("PG-0000172", 5925, history, later)
check("drop_since NOT reset on continued sale", history["PG-0000172"]["drop_since"] == "2026-07-08")
check("effective_date end rolls forward", eff.endswith("2026-08-12T23:59+0100"))

price, sale, eff = scraper.resolve_price("PG-0000172", 7900, history, later)
check("price back to baseline: no sale", sale is None and price == 7900)

price, sale, eff = scraper.resolve_price("PG-0000172", 8900, history, later)
check("price increase becomes new baseline", sale is None and price == 8900)
check("baseline updated", history["PG-0000172"]["baseline"] == 8900)

# --- full XML build, spot-check the new field structure ---
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
check("bare fb_product_category", text_of("fb_product_category") == "Jus og administrasjon")
check("bare feed_name (plain title)", text_of("feed_name") == "Advokatsekretær")
check("bare internal_label", text_of("internal_label") == "Advokatsekretær")
check("g:id", text_of("g:id", NS) == "PG-0001534")
check("g:item_group_id matches id", text_of("g:item_group_id", NS) == "PG-0001534")
check("g:brand is NKI", text_of("g:brand", NS) == "NKI")
check(
    "g:title has smart suffix",
    text_of("g:title", NS) == "Advokatsekretær − fagskole i jus og administrasjon",
)
check(
    "g:google_product_category text path",
    text_of("g:google_product_category", NS) == "utdanning > fagskole > jus og administrasjon",
)
check("g:product_type is bare category", text_of("g:product_type", NS) == "Jus og administrasjon")
check("g:price", text_of("g:price", NS) == "81500.00 NOK")
check("g:sale_price", text_of("g:sale_price", NS) == "61125.00 NOK")

print("\nAll checks passed.")
