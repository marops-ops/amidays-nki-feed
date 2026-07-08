# NKI produktfeed — oppsett

## Filplassering i repoet

```
scraper.py
requirements.txt
.github/workflows/feed.yml   <- flytt feed.yml hit
docs/feed.xml                <- genereres automatisk av scraperen, ikke rediger manuelt
data/price_history.json      <- genereres automatisk, commit den (den ER minnet for sale_price-logikken)
```

## Oppsett (én gang)

1. Opprett et repo på GitHub (privat eller offentlig — offentlig er nødvendig for at GitHub Pages skal servere feeden gratis).
2. Push disse filene til `main`-branchen, med `feed.yml` lagt i `.github/workflows/feed.yml`.
3. Settings → Pages → Source: "Deploy from a branch" → Branch: `main`, mappe: `/docs`.
4. Feeden blir da tilgjengelig på `https://<brukernavn>.github.io/<repo>/feed.xml`.
5. Kjør workflowen manuelt én gang (Actions-fanen → "Update NKI product feed" → "Run workflow") for å generere første `docs/feed.xml` og `data/price_history.json`.

## Viktig om første kjøring

- `data/price_history.json` starter tom. Første kjøring setter *baseline*-pris for hvert kurs, men emitter ingen `sale_price` — det er ingenting å sammenligne mot ennå. Sale price dukker først opp fra andre kjøring, når prisen faktisk har falt under den lagrede baseline.
- Loggen (i Actions-kjøringen) lister opp: kurs uten treff i `Utdanningsnivå`-feltet (klassifisert med fallback), kategorier som mangler i `CATEGORY_MAP`, og alle out-of-stock-lenker. Verdt å sjekke etter første kjøring.

## Kjente forenklinger vs. den gamle feeden (verdt å vite)

- **custom_label_0** bruker nå den direkte 5-delte oppdelingen (`kurs`, `enkeltemner`, `yrkesfag`, `vgo_teori`, `fagskole`) hentet fra sidens eget "Utdanningsnivå"-felt — ikke de gamle verdiene `emne_kurs`/`privat_vgs`. UTM-kampanjer i `ads_redirect` bruker denne nye verdien, så historisk GA4/Google Ads-rapportering på `utm_campaign` får et brudd i kontinuiteten fra denne datoen.
- **custom_label_2** (kategori) er nki.no sin egen, noe grovere kategorisering (f.eks. "HR og ledelse" som én bøtte, ikke splittet i "HR" og "Ledelse" slik den gamle feeden gjorde). `CATEGORY_MAP` i scraper.py kan finpusses etter behov når du ser reelle output-verdier.
- **product_type** er nå 2 nivåer (`Kurs > HR og ledelse`) i stedet for 3 (`Kurs > Ledelse > Teamledelse`) — vi har ikke en pålitelig kilde til det tredje, mer spesifikke nivået uten å gjette.
- **Lånekassen (custom_label_4)** er ikke med i denne versjonen (etter avtale). Feltet `Finansiering: Lånekassegodkjent` finnes faktisk på siden og er triviell å legge til senere.
- **g:id** hentes live fra sidens GTM dataLayer hver kjøring — det er IKKE de samme CRS-/PG-numrene som i den gamle feeden for alle kurs (stikkprøve viste at "Innføring i ledelse" har byttet fra `CRS-00586` til `PG-0000172` på siden). Dette er nki.no sin egen kilde, så det er riktig verdi, men vær obs på at gamle rapporter i Google Ads som er bygget på gamle IDer ikke nødvendigvis matcher nye kjøringer 1:1.
- **Ødelagte lenker fikset**: den gamle feeden hadde doble URL-segmenter (f.eks. `/enkeltemner//kurs/...`) som gir 404. Nye feeden bruker kun den reelle sitemap-URL-en.
