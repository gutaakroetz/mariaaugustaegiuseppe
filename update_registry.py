#!/usr/bin/env python3
"""
update_registry.py — Wedding gift registry page generator
==========================================================
Reads the Rinascente Lista Nozze HTML (download it from the registry site),
parses every gift item (name, price, quantity, department), downloads one
product image per item via Bing image search (with DuckDuckGo fallback),
then writes a complete standalone lista.html gift-catalog page.

Usage
-----
  python update_registry.py \
      --registry  "Rinascente Lista Nozze.html" \
      --output    lista.html \
      --assets    assets/gifts

  # Re-generate HTML only, skip image downloads:
  python update_registry.py \
      --registry  "Rinascente Lista Nozze.html" \
      --output    lista.html \
      --assets    assets/gifts \
      --no-download

Flags
-----
  --registry    Path to the downloaded Rinascente HTML file  [required]
  --output      Path for the generated lista.html            [default: lista.html]
  --assets      Directory where gift images are stored       [default: matrimonio/assets/gifts]
  --no-download Skip image fetching; use whatever is on disk
"""

import argparse, html, json, os, re, sys, time, unicodedata, urllib.parse, urllib.request
from pathlib import Path

REGISTRY_URL = "https://www.rinascentelistanozze.it/ListaNozze/it/AreaDonatori/5697/Regali"

DEPT_LABELS = {
    "gift-cards":  ("Gift Card",  "Gift Card",  "Gift Cards"),
    "kitchenware": ("Cucina",     "Cozinha",    "Kitchen"),
    "tableware":   ("Tavola",     "Mesa",       "Tableware"),
    "design":      ("Design",     "Design",     "Design"),
    "travel":      ("Valigie",    "Malas",      "Luggage"),
}


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:60]


def dept_slug(raw: str) -> str:
    raw = raw.strip().upper()
    if raw in ("", "GIFT CARDS", "CARTA VOGLIO TE"):
        return "gift-cards"
    if raw == "KITCHENWARE":  return "kitchenware"
    if raw == "TABLEWARE":    return "tableware"
    if raw == "DESIGN":       return "design"
    if raw == "TRAVEL":       return "travel"
    return slugify(raw)


def fmt_price(raw: str) -> str:
    return re.sub(r"€\s*", "€", raw.strip())


def fmt_qty(val: str) -> str:
    try:
        n = int(val.strip())
        return f"×{n}" if n > 1 else ""
    except ValueError:
        return ""


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_registry(html_path: str) -> list:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        sys.exit("BeautifulSoup4 not found — run: pip install beautifulsoup4 --break-system-packages")

    with open(html_path, encoding="utf-8", errors="replace") as f:
        soup = BeautifulSoup(f, "html.parser")

    items = []
    current_dept = "gift-cards"
    seen_slugs = {}

    for tag in soup.find_all(["span", "tr"]):
        if tag.name == "span" and "nome_reparto" in tag.get("class", []):
            raw = tag.get_text(strip=True)
            current_dept = dept_slug(raw)
            continue

        if tag.name == "tr" and "tr_view" in tag.get("class", []):
            name_tag = tag.find("span", id=re.compile(r"lbDescrizione_\d+$"))
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)

            brand_tag = tag.find("span", id=re.compile(r"lbMarca_\d+$"))
            brand_raw = brand_tag.get_text(strip=True) if brand_tag else ""
            # Extract human brand name: "11I ELECTRO - 117 ZWILLING" → "Zwilling"
            m = re.search(r'-\s*\d+\w*\s+(.+)$', brand_raw)
            brand = m.group(1).strip().title() if m else brand_raw.strip().title()

            code_tag = tag.find("span", id=re.compile(r"lbCodice_\d+$"))
            code = code_tag.get_text(strip=True) if code_tag else ""

            price_tag = tag.find("span", id=re.compile(r"lbPrezzo_\d+$"))
            price_raw = fmt_price(price_tag.get_text(strip=True)) if price_tag else ""

            qty_input = tag.find("input", class_="inp_quantita")
            qty_val = int(qty_input.get("value", "1")) if qty_input else 1
            qty = fmt_qty(str(qty_val))

            # Detect sold-out: remaining quantity is 0, or row has sold-out class
            sold_out = (qty_val == 0) or bool(
                tag.find(class_=re.compile(r"acquist|esaur|sold|unavail", re.I))
            )

            # Calculate total price (unit × quantity)
            total_price = price_raw
            if qty_val > 1 and price_raw:
                try:
                    unit = float(re.sub(r"[^\d,]", "", price_raw).replace(",", "."))
                    total = unit * qty_val
                    total_price = f"€{total:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
                except Exception:
                    pass

            base_slug = slugify(name) or "item"
            n = seen_slugs.get(base_slug, 0)
            seen_slugs[base_slug] = n + 1
            slug = base_slug if n == 0 else f"{base_slug}-{n}"

            labels = DEPT_LABELS.get(current_dept, (current_dept,) * 3)

            items.append({
                "slug":          slug,
                "name":          name,
                "brand":         brand,
                "code":          code,
                "price":         total_price,
                "unit_price":    price_raw,
                "qty":           qty,
                "qty_val":       qty_val,
                "sold_out":      sold_out,
                "dept":          current_dept,
                "dept_label_it": labels[0],
                "dept_label_pt": labels[1],
                "dept_label_en": labels[2],
                "img_path":      None,
            })

    return items


# ── Search query builder ──────────────────────────────────────────────────────

# Maps Rinascente slug keywords → better search query
# Key: substring that appears in the product slug (lowercased, hyphenated)
# Value: search query to use instead
QUERY_OVERRIDES = {
    "frigg":                    "Ariete Frigg air fryer 4L black",
    "frullatore-da-tavolo":     "Ariete blender 0.8L",
    "griglia-a-contatto":       "Ariete contact grill 2-in-1",
    "lineo-frullatore":         "Ariete LINEO immersion blender set 4-in-1",
    "forno-e-friggitrice":      "Ninja Combi oven air fryer",
    "tazza-espresso-london":    "Le Creuset espresso cup London orange 100ml",
    "cocotte-tonda-20":         "Le Creuset round cocotte 20cm orange",
    "cocotte-tonda-24":         "Le Creuset round cocotte 24cm orange",
    "set-pentole-5pz-passion":  "Lagostina Passion cookware set 5 pieces",
    "set-combipentola-24":      "Lagostina combipentola 24cm",
    "ceppo-gourmet-new-7":      "Wüsthof knife block set 7 pieces",
    "piatto-piano-27-bianco":   "Villeroy Boch white dinner plate 27cm",
    "piatto-fondo-20-bianco":   "Villeroy Boch white soup plate 20cm",
    "piatto-frutta-20-bianco":  "Villeroy Boch white side plate 20cm",
    "insalatiera-21-bianco":    "Villeroy Boch white salad bowl 21cm",
    "zuccheriera-bianco":       "Villeroy Boch sugar bowl white green",
    "tazza-caffe-c-p-bianco":   "Villeroy Boch coffee cup saucer white",
    "ciotola-media-13-bianco":  "Villeroy Boch white bowl 13cm",
    "mug-bianco":               "Villeroy Boch mug white green",
    "servizio-30-pz-silk":      "Richard Ginori Silk dinner service 30 pieces",
    "deco-clear-liquore":       "Arnolfo di Cambio DECO CLEAR liqueur glasses set",
    "deco-clear-acqua":         "Arnolfo di Cambio DECO CLEAR water glasses set",
    "decant-fatto-a-mano":      "Alessi hand-blown glass decanter cornetto",
    "riedel-veloce-cabernet":   "Riedel Veloce Cabernet wine glasses value pack",
    "riedel-veloce-pinot":      "Riedel Veloce Pinot Noir wine glasses value pack",
    "riedel-veloce-champagne":  "Riedel Veloce Champagne flutes value pack",
    "iconic-la-boule-memphis":  "Alessi La Boule Memphis centerpiece",
    "catene-cachemere-segnaposto": "Richard Ginori Catene Cachemere place card holder",
    "impero-catene-cachemere-piatto-piano-cm-22":  "Richard Ginori Impero Catene Cachemere plate 22cm",
    "impero-catene-cachemere-piatto-piano-cm-28":  "Richard Ginori Impero Catene Cachemere plate 28cm",
    "impero-catene-cachemere-piatto-fondo":        "Richard Ginori Impero Catene Cachemere soup plate",
    "impero-catene-cachemere-piatto-caffe":        "Richard Ginori Impero Catene Cachemere coffee plate",
    "impero-catene-cachemere-piatto-te":           "Richard Ginori Impero Catene Cachemere tea plate",
    "impero-catene-cachemere-tazza-caffe":         "Richard Ginori Impero Catene Cachemere espresso cup",
    "impero-catene-cachemere-tazza-te":            "Richard Ginori Impero Catene Cachemere tea cup",
    "impero-catene-cachemere-vassoio":             "Richard Ginori Impero Catene Cachemere oval tray",
    "octeve-cestino-pane":      "Christofle Octeve bread basket",
    "sarria-cestino":           "Sambonet Sarria round steel basket",
    "port-cestino":             "Sambonet Port round steel basket",
    "resonance-fruttirera":     "Sambonet Resonance fruit bowl",
    "harmonic-cestino":         "Sambonet Harmonic basket",
    "centrotavola-acciaio":     "Alessi steel centerpiece bowl",
    "manteglace-portabottiglia": "Alessi Manteglace wine bottle holder",
    "the-tending-box-set":      "W&P The Tending Box cocktail shaker strainer set",
    "plisse-caraffa-termois":   "Alessi Plissé thermal carafe white",
    "il-cono-bollitore":        "Alessi Il Cono kettle",
    "la-conica-caff":           "Alessi La Conica moka 3 cups",
    "cavatappi-red":            "Alessi corkscrew red",
    "eugenia-rinfrescatore":    "Alessi Eugenia wine cooler",
    "forma-grattugia":          "Alessi Forma grater",
    "edo-bollilatte":           "Alessi Edo milk frother",
    "plisse-bollitore-elet":    "Alessi Plissé electric kettle 1L white",
    "plisse-tostapane":         "Alessi Plissé toaster white",
    "augh-sottop-estensibile":  "Alessi Augh extensible trivet",
    "its-yunokiware":           "ITS Yunokiware set 12 pieces decorated cups",
    "plate-ovale-meo":          "Meo oval plate 37x33 white ceramic",
    "mill-grinder-small":       "Peugeot Paris pepper mill small",
    "mill-grinder-tall":        "Peugeot Paris pepper mill tall",
    "fazz-sabb":                "Richard Ginori vase sand glass",
    "tr-cabina-oliva":          "Samsonite trolley cabin olive green",
    "tr-grande-oliva":          "Samsonite trolley large olive green",
    "trolley-medio-olive":      "Samsonite trolley medium olive green",
}


def build_search_query(item: dict) -> str:
    """Return the best search query for a registry item."""
    slug = item["slug"]
    # Check for an exact override first
    if slug in QUERY_OVERRIDES:
        return QUERY_OVERRIDES[slug]
    # Check if any override key is a prefix of this slug
    for key, query in QUERY_OVERRIDES.items():
        if slug.startswith(key):
            return query
    # Fallback: title-case the name and append "product"
    name = item["name"]
    if name == name.upper() and len(name) > 6:
        name = name.title()
    return f"{name} product"


# ── Image search engines ──────────────────────────────────────────────────────

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/124.0.0.0 Safari/537.36")


def _fetch(url: str, headers: dict = None, timeout: int = 14) -> bytes:
    req = urllib.request.Request(url, headers={**{"User-Agent": _UA}, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def bing_image_url(query: str):
    """Scrape Bing Images for the first product image URL."""
    import html as _html
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://www.bing.com/images/search?q={encoded}&first=1&count=10&form=HDRSC2"
        page = _fetch(url, headers={"Accept-Language": "en-US,en;q=0.9"}, timeout=10).decode("utf-8", errors="replace")
        # Bing HTML-encodes the JSON inside data attributes — decode first
        page = _html.unescape(page)
        urls = re.findall(r'"murl"\s*:\s*"(https?://[^"]+)"', page)
        if not urls:
            urls = [urllib.parse.unquote(u) for u in re.findall(r'imgurl=([^&"]+)', page)]
        if not urls:
            print(f"    Bing: no URLs found", end=" ")
            return None
        for u in urls[:10]:
            low = u.lower().split("?")[0]
            if any(low.endswith(e) for e in (".jpg", ".jpeg", ".png", ".webp")):
                return u
        return urls[0] if urls else None
    except Exception as exc:
        print(f"    Bing error: {exc}", end=" ")
        return None


def ddg_image_url(query: str):
    """DuckDuckGo image search — requires fetching a vqd token first."""
    try:
        encoded = urllib.parse.quote_plus(query)
        page = _fetch(
            f"https://duckduckgo.com/?q={encoded}&iax=images&ia=images",
            timeout=8,
        ).decode("utf-8", errors="replace")
        m = (re.search(r'vqd=(["\'])([^"\']+)\1', page) or
             re.search(r'"vqd"\s*:\s*"([^"]+)"', page) or
             re.search(r'vqd=([0-9-]+)', page))
        if not m:
            return None
        vqd = m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
        api = (f"https://duckduckgo.com/i.js?"
               f"q={encoded}&vqd={urllib.parse.quote(vqd)}&f=,,,,&p=1&v7exp=a")
        data = json.loads(_fetch(api, headers={"Referer": "https://duckduckgo.com/"}, timeout=8).decode())
        results = data.get("results", [])
        for res in results[:8]:
            u = res.get("image", "")
            if u and any(u.lower().split("?")[0].endswith(e)
                         for e in (".jpg", ".jpeg", ".png", ".webp")):
                return u
        return results[0].get("image") if results else None
    except Exception as exc:
        print(f"    DDG error: {exc}", end=" ")
        return None


def google_cse_image_url(query: str, api_key: str, cx: str):
    """Google Custom Search — only used if GOOGLE_CSE_KEY and GOOGLE_CSE_CX are set."""
    try:
        encoded = urllib.parse.quote_plus(query)
        url = (f"https://www.googleapis.com/customsearch/v1"
               f"?q={encoded}&searchType=image&num=3&key={api_key}&cx={cx}")
        data = json.loads(_fetch(url, timeout=10).decode())
        items = data.get("items", [])
        for item in items:
            u = item.get("link", "")
            if u and any(u.lower().split("?")[0].endswith(e)
                         for e in (".jpg", ".jpeg", ".png", ".webp")):
                return u
        return None
    except Exception as exc:
        print(f"    Google CSE error: {exc}", end=" ")
        return None


def find_image_url(query: str):
    """Try Bing → DDG → Google CSE (if keys set in env)."""
    url = bing_image_url(query)
    if url:
        return url, "Bing"
    url = ddg_image_url(query)
    if url:
        return url, "DDG"
    # Optional: set GOOGLE_CSE_KEY and GOOGLE_CSE_CX in your environment
    gkey = os.environ.get("GOOGLE_CSE_KEY", "")
    gcx  = os.environ.get("GOOGLE_CSE_CX", "")
    if gkey and gcx:
        url = google_cse_image_url(query, gkey, gcx)
        if url:
            return url, "Google"
    return None, None


def guess_ext(url: str) -> str:
    path = url.split("?")[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def dl(url: str, dest: str) -> int:
    data = _fetch(url, timeout=15)
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)


def ensure_images(items: list, assets_dir: str) -> None:
    os.makedirs(assets_dir, exist_ok=True)
    total = len(items)
    for i, item in enumerate(items):
        slug = item["slug"]
        prefix = f"[{i+1:3d}/{total}]"

        existing = [f for f in os.listdir(assets_dir) if f.startswith(slug + ".")]
        if existing:
            item["img_path"] = f"assets/gifts/{existing[0]}"
            print(f"{prefix} SKIP (exists): {existing[0]}")
            continue

        if item["dept"] == "gift-cards":
            print(f"{prefix} SKIP (gift card): {slug}")
            continue

        query = build_search_query(item)
        print(f"{prefix} Searching: {query[:58]!r} … ", end="", flush=True)

        img_url, source = find_image_url(query)
        if not img_url:
            print("✗ no result")
            continue

        ext = guess_ext(img_url)
        dest = os.path.join(assets_dir, slug + ext)
        try:
            size = dl(img_url, dest)
            item["img_path"] = f"assets/gifts/{slug}{ext}"
            print(f"✓ {size // 1024}KB [{source}]")
        except Exception as exc:
            print(f"✗ download failed: {exc}")

        time.sleep(0.6)


# ── HTML generation ───────────────────────────────────────────────────────────

def _eur_numeric(price_str: str) -> str:
    """Extract numeric EUR value from '€1.234,56' → '1234.56' for data-eur attribute."""
    raw = re.sub(r"[^\d,]", "", price_str)  # keep digits and comma
    # Italian format: dots as thousands sep, comma as decimal
    # Remove any dots first (thousand separators), replace comma with dot
    raw = raw.replace(".", "").replace(",", ".")
    try:
        return f"{float(raw):.2f}"
    except ValueError:
        return ""


def _card(item: dict) -> str:
    name_esc  = html.escape(item["name"].title())
    brand_esc = html.escape(item.get("brand", ""))
    code_esc  = html.escape(item.get("code", ""))
    price_esc = html.escape(item["price"])
    qty_val   = item.get("qty_val", 1)
    sold_out  = item.get("sold_out", False)
    dept      = item["dept"]
    li_it     = html.escape(item["dept_label_it"])
    li_pt     = html.escape(item["dept_label_pt"])
    li_en     = html.escape(item["dept_label_en"])
    img_path  = item["img_path"]

    # data-eur attribute for currency toggle
    eur_val = _eur_numeric(item["price"])
    eur_attr = f' data-eur="{eur_val}"' if eur_val else ""

    # Gift cards use a hardcoded image
    if dept == "gift-cards" and not img_path:
        img_path = "assets/gift-card.jpg"

    if img_path:
        web_path = img_path.replace("\\", "/")
        img_tag = (f'<img src="{html.escape(web_path)}" alt="{name_esc}" '
                   f'loading="lazy" onerror="this.parentNode.classList.add(\'no-img\');this.remove()">')
        img_cls = "gift-card-img"
    else:
        img_tag = ""
        img_cls = "gift-card-img no-img"

    sold_badge = (
        '\n        <div class="sold-out-badge">'
        '<span data-lang="it">Esaurito</span>'
        '<span data-lang="pt">Esgotado</span>'
        '<span data-lang="en">Taken</span>'
        '</div>'
    ) if sold_out else ""

    card_cls = "gift-card sold-out" if sold_out else "gift-card"
    qty_html  = f'<span class="card-qty">×{qty_val}</span>' if qty_val > 1 else ""
    code_html = f'<span class="card-code">cod. {code_esc}</span>' if code_esc else ""

    cta_html = (
        '<span data-lang="it">Esaurito</span>'
        '<span data-lang="pt">Esgotado</span>'
        '<span data-lang="en">Taken</span>'
    ) if sold_out else (
        '<span data-lang="it">Regala ↗</span>'
        '<span data-lang="pt">Presentear ↗</span>'
        '<span data-lang="en">Gift ↗</span>'
    )

    return (
        f'\n      <a class="{card_cls}" data-dept="{dept}" '
        f'href="{REGISTRY_URL}" target="_blank" rel="noopener">'
        f'\n        <div class="{img_cls}">{img_tag}</div>'
        f'{sold_badge}'
        f'\n        <div class="gift-card-body">'
        f'\n          <p class="gift-card-dept">'
        f'<span data-lang="it">{li_it}</span>'
        f'<span data-lang="pt">{li_pt}</span>'
        f'<span data-lang="en">{li_en}</span></p>'
        f'\n          <p class="gift-card-name">{name_esc}{qty_html}</p>'
        f'\n          <p class="gift-card-brand">{brand_esc}</p>'
        f'\n          {code_html}'
        f'\n          <p class="gift-card-price"{eur_attr}>{price_esc}</p>'
        f'\n          <p class="gift-card-cta">{cta_html}</p>'
        f'\n        </div>'
        f'\n      </a>'
    )


def generate_page(items: list, assets_dir: str) -> str:
    # Gift cards always last
    items_sorted = [i for i in items if i["dept"] != "gift-cards"] + \
                   [i for i in items if i["dept"] == "gift-cards"]
    cards_html = "".join(_card(item) for item in items_sorted)

    seen_depts = []
    for item in items_sorted:
        if item["dept"] not in seen_depts:
            seen_depts.append(item["dept"])

    filter_btns = ['      <button class="filter-btn active" data-dept="all">'
                   '<span data-lang="it">Tutti</span>'
                   '<span data-lang="pt">Todos</span>'
                   '<span data-lang="en">All</span></button>']
    for d in seen_depts:
        lb = DEPT_LABELS.get(d, (d, d, d))
        filter_btns.append(
            f'      <button class="filter-btn" data-dept="{d}">'
            f'<span data-lang="it">{html.escape(lb[0])}</span>'
            f'<span data-lang="pt">{html.escape(lb[1])}</span>'
            f'<span data-lang="en">{html.escape(lb[2])}</span></button>'
        )
    filters_html = "\n".join(filter_btns)

    FAVICON = ("data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%27"
               "%20viewBox%3D%270%200%2032%2032%27%3E%3Crect%20width%3D%2732%27%20height%3D%2732%27"
               "%20fill%3D%27%23F4F1E8%27%2F%3E%3Ctext%20x%3D%2716%27%20y%3D%2723%27"
               "%20text-anchor%3D%27middle%27%20font-size%3D%2720%27%20font-family%3D%27Georgia%2Cserif%27"
               "%20fill%3D%27%234A5740%27%3E%E2%99%A1%3C%2Ftext%3E%3C%2Fsvg%3E")

    return f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lista Regali · Maria Augusta &amp; Giuseppe</title>
<meta name="robots" content="noindex, nofollow">
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Jost:ital,wght@0,200;0,300;0,400;0,500;1,300;1,400&display=swap">
<style>
:root {{
  --cream:      #F4F1E8;
  --cream-deep: #EAE6D8;
  --sage:       #6B7A5E;
  --sage-dark:  #4A5740;
  --sage-light: #8A9A7A;
  --ink:        #2C2C28;
  --ink-soft:   #5A5A52;
  --line:       rgba(107,122,94,0.25);
  --font: 'Jost', 'Futura', 'Century Gothic', sans-serif;
  color-scheme: light;
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ scroll-behavior: smooth; }}
body {{
  background: var(--cream);
  color: var(--ink);
  font-family: var(--font);
  font-size: 16px;
  line-height: 1.65;
  min-height: 100dvh;
}}
img {{ max-width: 100%; display: block; }}
a {{ text-decoration: none; }}

/* TOP BAR */
.top-bar {{
  position: sticky; top: 0; z-index: 100;
  background: var(--sage-dark);
  display: flex; align-items: center; justify-content: space-between;
  padding: 0 28px; height: 52px; gap: 16px;
}}
.top-bar-back {{
  font-family: var(--font); font-size: 9px; letter-spacing: 0.28em;
  text-transform: uppercase; color: var(--cream); opacity: 0.75;
  transition: opacity 0.15s;
}}
.top-bar-back:hover {{ opacity: 1; }}
.top-bar-title {{
  font-family: var(--font); font-size: 11px; letter-spacing: 0.32em;
  text-transform: uppercase; font-weight: 300; color: var(--cream);
  opacity: 0.9; flex: 1; text-align: center;
}}
.lang-switcher {{ display: flex; gap: 10px; }}
.lang-btn {{
  font-family: var(--font); font-size: 9px; letter-spacing: 0.24em;
  text-transform: uppercase; background: none; border: none;
  color: var(--cream); opacity: 0.5; cursor: pointer; padding: 4px 0;
  transition: opacity 0.15s;
}}
.lang-btn:hover {{ opacity: 0.85; }}
.lang-btn.active {{ opacity: 1; font-weight: 500; }}
/* CURRENCY SLIDER TOGGLE */
.currency-toggle {{
  margin-left: auto; display: flex; align-items: center;
  background: var(--cream-deep); border: 1px solid var(--line);
  border-radius: 20px; padding: 2px; gap: 0; cursor: pointer;
  flex-shrink: 0;
}}
.currency-toggle span {{
  font-family: var(--font); font-size: 9px; letter-spacing: 0.22em;
  text-transform: uppercase; font-weight: 400; color: var(--ink-soft);
  padding: 5px 13px; border-radius: 16px; transition: background 0.18s, color 0.18s;
  user-select: none; white-space: nowrap;
}}
.currency-toggle span.cur-active {{
  background: var(--sage-dark); color: var(--cream);
}}

/* PAGE HEADER */
.page-header {{
  padding: 56px 28px 40px; max-width: 960px; margin: 0 auto;
  border-bottom: 1px solid var(--line);
}}
.page-eyebrow {{
  font-family: var(--font); font-size: 9px; letter-spacing: 0.36em;
  text-transform: uppercase; font-weight: 400; color: var(--sage); margin-bottom: 14px;
}}
.page-title {{
  font-family: var(--font); font-size: clamp(1.6rem, 4vw, 2.6rem);
  font-weight: 200; letter-spacing: 0.04em; color: var(--ink);
  line-height: 1.2; margin-bottom: 16px;
}}
.page-subtitle {{
  font-family: var(--font); font-size: 0.85rem; font-weight: 300;
  color: var(--ink-soft); max-width: 560px; line-height: 1.7;
  margin-bottom: 20px;
}}
.page-subtitle a {{ color: var(--sage-dark); text-decoration: underline; text-underline-offset: 3px; }}

/* LOGIN INLINE */
.login-inline {{
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  max-width: 560px;
}}
.login-inline-label {{
  font-family: var(--font); font-size: 8px; letter-spacing: 0.28em;
  text-transform: uppercase; color: var(--ink-soft); opacity: 0.65;
}}
.login-inline-value {{
  font-family: var(--font); font-size: 0.95rem; font-weight: 400;
  letter-spacing: 0.06em; color: var(--ink);
}}
.login-inline-sep {{ color: var(--line); font-weight: 300; margin: 0 4px; }}

/* FILTERS */
.filters-wrap {{
  position: sticky; top: 52px; z-index: 90;
  background: var(--cream);
  border-bottom: 1px solid var(--line);
}}
.filters-inner {{
  max-width: 960px; margin: 0 auto;
  padding: 14px 28px;
  display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
}}
.filter-btn {{
  font-family: var(--font); font-size: 9px; letter-spacing: 0.22em;
  text-transform: uppercase; font-weight: 400; background: transparent;
  border: 1px solid var(--line); color: var(--ink-soft);
  padding: 6px 16px; border-radius: 20px; cursor: pointer;
  transition: background 0.18s, border-color 0.18s, color 0.18s;
}}
.filter-btn:hover, .filter-btn.active {{
  background: var(--sage-dark); border-color: var(--sage-dark); color: var(--cream);
}}

/* GRID */
.catalog-grid {{
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 1px; background: var(--line);
  max-width: 960px; margin: 28px auto 0;
  border: 1px solid var(--line);
}}
.gift-card {{
  background: var(--cream); display: flex; flex-direction: column;
  text-decoration: none; transition: background 0.18s; overflow: hidden;
}}
.gift-card:hover {{ background: var(--cream-deep); }}
.gift-card[hidden] {{ display: none !important; }}

/* card image */
.gift-card-img {{
  width: 100%; aspect-ratio: 1 / 1; overflow: hidden;
  background: var(--cream-deep);
  display: flex; align-items: center; justify-content: center;
  font-size: 2rem; color: var(--sage-light);
}}
.gift-card-img.no-img::after {{ content: "♡"; }}
.gift-card-img img {{
  width: 100%; height: 100%; object-fit: cover; display: block;
  transition: transform 0.3s;
}}
.gift-card:hover .gift-card-img img {{ transform: scale(1.04); }}

/* card body */
.gift-card-body {{
  padding: 14px 16px 18px; display: flex; flex-direction: column;
  flex: 1; gap: 5px;
}}
.gift-card-dept {{
  font-family: var(--font); font-size: 8px; letter-spacing: 0.30em;
  text-transform: uppercase; color: var(--sage);
}}
.gift-card-brand {{
  font-family: var(--font); font-size: 0.65rem; font-weight: 400;
  color: var(--sage); text-transform: uppercase; letter-spacing: 0.10em;
}}
.gift-card-name {{
  font-family: var(--font); font-size: 0.78rem; font-weight: 300;
  color: var(--ink); line-height: 1.4; flex: 1;
}}
.card-qty {{
  display: inline-block; margin-left: 5px;
  font-size: 0.7rem; color: var(--sage); font-weight: 400;
}}
.card-code {{
  display: block; font-size: 0.65rem; color: var(--ink-soft);
  opacity: 0.6; letter-spacing: 0.03em; margin-top: -2px;
}}
.gift-card-price {{
  font-family: var(--font); font-size: 0.88rem; font-weight: 400;
  color: var(--sage-dark); margin-top: 4px;
}}
.gift-card-cta {{
  font-family: var(--font); font-size: 8px; letter-spacing: 0.22em;
  text-transform: uppercase; color: var(--sage);
  margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--line);
}}
.gift-card:hover .gift-card-cta {{ color: var(--sage-dark); }}

/* SOLD OUT */
.gift-card.sold-out {{ pointer-events: none; }}
.gift-card.sold-out .gift-card-img {{ filter: grayscale(60%); opacity: 0.7; }}
.gift-card.sold-out .gift-card-price {{ text-decoration: line-through; opacity: 0.5; }}
.gift-card.sold-out .gift-card-cta {{ color: var(--sage-light); letter-spacing: 0.12em; }}
.sold-out-badge {{
  position: absolute; top: 10px; left: 10px;
  background: var(--sage-dark); color: var(--cream);
  font-family: var(--font); font-size: 8px; letter-spacing: 0.22em;
  text-transform: uppercase; padding: 4px 10px; border-radius: 2px;
  pointer-events: none;
}}
.gift-card {{ position: relative; }}

/* FOOTER */
.page-footer {{
  text-align: center; padding: 40px 24px;
  border-top: 1px solid var(--line);
  font-family: var(--font); font-size: 9px; letter-spacing: 0.28em;
  text-transform: uppercase; color: var(--ink-soft); opacity: 0.6;
  margin-top: 64px;
}}

/* LANGUAGE — driven by html[lang] attribute */
[data-lang] {{ display: none; }}
html[lang="it"] [data-lang="it"],
html[lang="pt"] [data-lang="pt"],
html[lang="en"] [data-lang="en"] {{ display: inline; }}

/* RESPONSIVE */
@media (max-width: 860px) {{
  .catalog-grid {{ grid-template-columns: repeat(3, 1fr); }}
}}
@media (max-width: 580px) {{
  .catalog-grid {{ grid-template-columns: repeat(2, 1fr); }}
  .top-bar {{ padding: 0 16px; }}
  .page-header, .filters-inner {{ padding-inline: 16px; }}
}}
</style>
</head>
<body>

<div class="top-bar">
  <a class="top-bar-back" href="index.html">
    <span data-lang="it">← Indietro</span>
    <span data-lang="pt">← Voltar</span>
    <span data-lang="en">← Back</span>
  </a>
  <span class="top-bar-title">
    <span data-lang="it">Lista Regali · La Rinascente</span>
    <span data-lang="pt">Lista de Presentes · La Rinascente</span>
    <span data-lang="en">Gift Registry · La Rinascente</span>
  </span>
  <div class="lang-switcher" role="group" aria-label="Language">
    <button class="lang-btn" data-set-lang="it">IT</button>
    <button class="lang-btn" data-set-lang="pt">PT</button>
    <button class="lang-btn" data-set-lang="en">EN</button>
  </div>
</div>

<header class="page-header">
  <p class="page-eyebrow">Maria Augusta &amp; Giuseppe · 20.03.2027</p>
  <h1 class="page-title">
    <span data-lang="it">Lista Regali</span>
    <span data-lang="pt">Lista de Presentes</span>
    <span data-lang="en">Gift Registry</span>
  </h1>
  <p class="page-subtitle">
    <span data-lang="it">
      Una selezione dalla nostra lista <a href="{REGISTRY_URL}" target="_blank" rel="noopener">La Rinascente ↗</a>.
      Clicca su un regalo per acquistarlo direttamente dalla lista ufficiale.
    </span>
    <span data-lang="pt">
      Uma seleção da nossa lista <a href="{REGISTRY_URL}" target="_blank" rel="noopener">La Rinascente ↗</a>.
      Clique em um presente para comprá-lo diretamente da lista oficial.
    </span>
    <span data-lang="en">
      A selection from our <a href="{REGISTRY_URL}" target="_blank" rel="noopener">La Rinascente ↗</a> registry.
      Click any gift to purchase it directly from the official list.
    </span>
  </p>
  <div class="login-inline">
    <span class="login-inline-label">
      <span data-lang="it">Utente</span>
      <span data-lang="pt">Usuário</span>
      <span data-lang="en">Username</span>
    </span>
    <span class="login-inline-value">451019980</span>
    <span class="login-inline-sep">·</span>
    <span class="login-inline-label">Password</span>
    <span class="login-inline-value">Maria&amp;Giuseppe27</span>
  </div>
</header>

<div class="filters-wrap">
  <div class="filters-inner">
{filters_html}
      <div class="currency-toggle" id="currency-toggle" role="group" aria-label="Currency">
        <span id="cur-eur" class="cur-active">&euro; EUR</span>
        <span id="cur-brl">R$ BRL</span>
      </div>
  </div>
</div>

<div class="catalog-grid">
{cards_html}
</div>

<footer class="page-footer">
  <p>Maria Augusta &amp; Giuseppe · 20 · 03 · 2027 · Curitiba</p>
</footer>

<script>
(function () {{
  var root = document.documentElement;
  var stored;
  try {{ stored = localStorage.getItem('ma-g-lang'); }} catch (e) {{}}
  var lang = stored || (navigator.language || 'it').slice(0, 2);
  if (!['it', 'pt', 'en'].includes(lang)) lang = 'it';

  /* --- Currency --- */
  var brlRate = null;
  var showBRL = false;
  var brlManual = false;
  var curEur = document.getElementById('cur-eur');
  var curBrl = document.getElementById('cur-brl');

  function fetchRate() {{
    fetch('https://api.frankfurter.app/latest?from=EUR&to=BRL')
      .then(function(r) {{ return r.json(); }})
      .then(function(data) {{
        brlRate = data.rates && data.rates.BRL ? data.rates.BRL : null;
        renderPrices();
      }})
      .catch(function() {{ brlRate = null; renderPrices(); }});
  }}

  function formatBRL(eur) {{
    if (!brlRate) return null;
    var brl = eur * brlRate;
    var parts = brl.toFixed(2).split('.');
    var intPart = parts[0].replace(/\B(?=(\d{{3}})+(?!\d))/g, '.');
    return 'R$\u00a0' + intPart + ',' + parts[1];
  }}

  function renderPrices() {{
    document.querySelectorAll('[data-eur]').forEach(function(el) {{
      var eur = parseFloat(el.dataset.eur);
      if (showBRL && brlRate) {{
        el.textContent = formatBRL(eur) || ('\u20ac' + el.dataset.eur.replace('.', ','));
      }} else {{
        var parts = eur.toFixed(2).split('.');
        var intPart = parts[0].replace(/\B(?=(\d{{3}})+(?!\d))/g, '.');
        el.textContent = '\u20ac' + intPart + ',' + parts[1];
      }}
    }});
    if (curEur) curEur.classList.toggle('cur-active', !showBRL);
    if (curBrl) curBrl.classList.toggle('cur-active', showBRL);
  }}

  function activateCurrency(brl) {{
    brlManual = true;
    showBRL = brl;
    if (showBRL && !brlRate) {{
      fetchRate();
    }} else {{
      renderPrices();
    }}
  }}

  if (curEur) curEur.addEventListener('click', function() {{ activateCurrency(false); }});
  if (curBrl) curBrl.addEventListener('click', function() {{ activateCurrency(true); }});

  // Lang switcher
  document.querySelectorAll('[data-set-lang]').forEach(function (btn) {{
    btn.addEventListener('click', function () {{ setLang(btn.dataset.setLang); }});
  }});

  function setLang(l) {{
    root.lang = l;
    document.querySelectorAll('[data-set-lang]').forEach(function (b) {{
      b.classList.toggle('active', b.dataset.setLang === l);
    }});
    try {{ localStorage.setItem('ma-g-lang', l); }} catch (e) {{}}
    if (!brlManual) {{
      showBRL = (l === 'pt');
      renderPrices();
    }}
  }}

  // Kick off: fetch rate, then set language (setLang calls renderPrices)
  fetchRate();
  setLang(lang);

  /* Department filter */
  var filterBtns = document.querySelectorAll('.filter-btn');
  var cards = document.querySelectorAll('.gift-card');
  filterBtns.forEach(function (btn) {{
    btn.addEventListener('click', function () {{
      var dept = btn.dataset.dept;
      filterBtns.forEach(function (b) {{ b.classList.remove('active'); }});
      btn.classList.add('active');
      cards.forEach(function (card) {{
        if (dept === 'all' || card.dataset.dept === dept) {{
          card.removeAttribute('hidden');
        }} else {{
          card.setAttribute('hidden', '');
        }}
      }});
    }});
  }});
}})();
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Generate wedding gift registry page")
    ap.add_argument("--registry",    required=True,          help="Path to Rinascente HTML")
    ap.add_argument("--output",      default="matrimonio/lista.html",   help="Output HTML path [matrimonio/lista.html]")
    ap.add_argument("--assets",      default="matrimonio/assets/gifts", help="Image directory [matrimonio/assets/gifts]")
    ap.add_argument("--no-download", action="store_true",    help="Skip image downloads")
    args = ap.parse_args()

    print(f"\nParsing registry: {args.registry}")
    items = parse_registry(args.registry)
    print(f"Found {len(items)} items\n")

    if not args.no_download:
        print(f"Downloading images → {args.assets}/\n")
        ensure_images(items, args.assets)
    else:
        os.makedirs(args.assets, exist_ok=True)
        for item in items:
            existing = [f for f in os.listdir(args.assets) if f.startswith(item["slug"] + ".")]
            if existing:
                item["img_path"] = f"assets/gifts/{existing[0]}"

    print(f"\nWriting: {args.output}")
    page = generate_page(items, args.assets)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(page)

    resolved = sum(1 for i in items if i["img_path"])
    print(f"Done. {resolved}/{len(items)} images resolved → {args.output}\n")


if __name__ == "__main__":
    main()
