import hashlib
import json
import random
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
import httpx
from bs4 import BeautifulSoup


def _normalize_city(text: str) -> str:
    """Normalise le nom d'une commune (sans accents, ligatures ni ponctuation) pour comparaison fiable."""
    if not text:
        return ""
    text = str(text).lower().strip()
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[-'’_./]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _city_matches_targets(ad_city: str, target_cities: List[str]) -> bool:
    """Vérifie si la ville d'une annonce correspond à l'une des villes recherchées."""
    if not target_cities:
        return True
    norm_ad = _normalize_city(ad_city)
    if not norm_ad:
        return True
    for target in target_cities:
        norm_t = _normalize_city(target)
        if norm_t and (norm_t == norm_ad or norm_t in norm_ad or norm_ad in norm_t):
            return True
    return False


def _canonical_city_name(ad_city: str, target_cities: Optional[List[str]] = None) -> str:
    """Unifie la graphie du nom de ville (ex: 'Marcq-en-Baroeul' -> 'Marcq-en-Barœul') pour éviter les doublons de filtre."""
    norm_ad = _normalize_city(ad_city)
    if not norm_ad:
        return ad_city.strip()
    for known_city in CITY_METADATA:
        if _normalize_city(known_city) == norm_ad:
            return known_city
    for target in (target_cities or []):
        if _normalize_city(target) == norm_ad:
            return target.strip()
    return ad_city.strip()


# Métadonnées géographiques et zoneIds Bien'ici / Slugs Agences pour la métropole lilloise
CITY_METADATA: Dict[str, Dict[str, str]] = {
    "Croix": {
        "postal_code": "59170",
        "insee": "59163",
        "slug": "croix-59170",
        "orpi_slug": "croix",
        "bienici_zone_id": "-58677",
        "seloger_place_id": "AD08FR23435",
        "seloger_slug": "immo-croix-59",
    },
    "Wasquehal": {
        "postal_code": "59290",
        "insee": "59646",
        "slug": "wasquehal-59290",
        "orpi_slug": "wasquehal",
        "bienici_zone_id": "-59041",
        "seloger_place_id": "AD08FR23906",
        "seloger_slug": "immo-wasquehal-59",
    },
    "Roubaix": {
        "postal_code": "59100",
        "insee": "59512",
        "slug": "roubaix-59100",
        "orpi_slug": "roubaix",
        "bienici_zone_id": "-59043",
        "seloger_place_id": "AD08FR23773",
        "seloger_slug": "immo-roubaix-59",
    },
    "Villeneuve-d'Ascq": {
        "postal_code": "59650",
        "insee": "59009",
        "slug": "villeneuve-d-ascq-59650",
        "orpi_slug": "villeneuve-d-ascq",
        "bienici_zone_id": "-58213",
        "seloger_place_id": "AD08FR23285",
        "seloger_slug": "immo-villeneuve-d-ascq-59",
    },
    "Marcq-en-Barœul": {
        "postal_code": "59700",
        "insee": "59378",
        "slug": "marcq-en-baroeul-59700",
        "orpi_slug": "marcq-en-baroeul",
        "bienici_zone_id": "-58686",
        "seloger_place_id": "AD08FR23643",
        "seloger_slug": "immo-marcq-en-baroeul-59",
    },
    "Mouvaux": {
        "postal_code": "59420",
        "insee": "59421",
        "slug": "mouvaux-59420",
        "orpi_slug": "mouvaux",
        "bienici_zone_id": "-58685",
        "seloger_place_id": "AD08FR23684",
        "seloger_slug": "immo-mouvaux-59",
    },
    "Bondues": {
        "postal_code": "59910",
        "insee": "59090",
        "slug": "bondues-59910",
        "orpi_slug": "bondues",
        "bienici_zone_id": "-58682",
        "seloger_place_id": "AD08FR23363",
        "seloger_slug": "immo-bondues-59",
    },
    "Hem": {
        "postal_code": "59510",
        "insee": "59299",
        "slug": "hem-59510",
        "orpi_slug": "hem",
        "bienici_zone_id": "-58678",
        "seloger_place_id": "AD08FR23561",
        "seloger_slug": "immo-hem-59",
    },
    "Lambersart": {
        "postal_code": "59130",
        "insee": "59328",
        "slug": "lambersart-59130",
        "orpi_slug": "lambersart",
        "bienici_zone_id": "-58692",
        "seloger_place_id": "AD08FR23590",
        "seloger_slug": "immo-lambersart-59",
    },
    "La Madeleine": {
        "postal_code": "59110",
        "insee": "59368",
        "slug": "la-madeleine-59110",
        "orpi_slug": "la-madeleine",
        "bienici_zone_id": "-58693",
        "seloger_place_id": "AD08FR23633",
        "seloger_slug": "immo-la-madeleine-59",
    },
}

ROTATING_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]


def get_browser_headers(accept_json: bool = False) -> Dict[str, str]:
    """
    Génère des en-têtes HTTP cohérents sans forcer Brotli ('br') afin que httpx
    décompresse nativement les réponses gzip/deflate de SeLoger, Orpi et Bien'ici.
    """
    ua = random.choice(ROTATING_USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept-Language": "fr-FR,fr;q=0.9",
    }
    if accept_json:
        headers["Accept"] = "application/json, text/plain, */*"
    else:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    return headers


DEFAULT_HEADERS = get_browser_headers(accept_json=False)

PARKING_KEYWORDS = [
    "garage",
    "box",
    "parking",
    "stationnement",
    "carport",
    "place de parc",
    "stationner",
]

NO_PARKING_KEYWORDS = [
    "sans garage",
    "pas de garage",
    "sans parking",
    "pas de parking",
    "stationnement dans la rue",
    "parking facile dans la rue",
]


def _detect_parking_in_text(text: str, explicit_flag: Optional[bool] = None) -> bool:
    """Détecte la présence d'un stationnement privatif (garage/box/parking) sans faux positif."""
    text_lower = (text or "").lower()
    if any(neg in text_lower for neg in NO_PARKING_KEYWORDS):
        return False
    if explicit_flag is True:
        return True
    return any(kw in text_lower for kw in PARKING_KEYWORDS)


def _get_city_meta(city_name: str) -> Dict[str, str]:
    c_norm = _normalize_city(city_name)
    for known_city, meta in CITY_METADATA.items():
        if _normalize_city(known_city) == c_norm:
            return meta
    slug = c_norm.replace(" ", "-")
    return {
        "postal_code": "59000",
        "slug": f"{slug}-59000",
        "orpi_slug": slug,
    }


def robust_http_get(
    client: httpx.Client,
    url: str,
    max_retries: int = 2,
    backoff: float = 1.0,
) -> Optional[httpx.Response]:
    """Exécute une requête GET avec retry et backoff exponentiel pour absorber les micro-coupures et rate limits."""
    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url)
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < max_retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
            return resp
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt < max_retries:
                time.sleep(backoff * (attempt + 1))
                continue
            return None
        except Exception:
            return None
    return None


def fetch_page_content_playwright(url: str, timeout: float = 16.0) -> Optional[str]:
    """
    Fallback anti-WAF / anti-bot : utilise un vrai navigateur Chromium headless via Playwright
    pour contourner les challenges JS / Captcha DataDome / Cloudflare si la requête HTTP simple est bloquée.
    """
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError):
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                locale="fr-FR",
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            page.wait_for_timeout(1200)
            html = page.content()
            browser.close()
            return html
    except Exception as exc:
        print(f"[Playwright Fallback] Échec navigation pour {url}: {exc}")
        return None


def _clean_html_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _format_agency_source(ad_id: str, account_name: str = "") -> str:
    """Identifie le réseau d'agence ou mandataire d'origine à partir de l'annonce agrégée."""
    if account_name:
        return f"Bien'ici / {account_name}"
    prefix = ad_id.split("-")[0].lower() if "-" in ad_id else ""
    mapping = {
        "iad": "IAD France (via Bien'ici)",
        "safti": "Safti (via Bien'ici)",
        "citya": "Citya Immobilier (via Bien'ici)",
        "era": "ERA Immobilier (via Bien'ici)",
        "guy": "Guy Hoquet (via Bien'ici)",
        "square": "Square Habitat (via Bien'ici)",
        "megagence": "MegAgence (via Bien'ici)",
        "hektor": "Agence Locale Hektor (via Bien'ici)",
        "netty": "Agence Locale Netty (via Bien'ici)",
        "apimo": "Agence Locale Apimo (via Bien'ici)",
        "immo": "Immo de France / Sergic (via Bien'ici)",
        "fnaim": "Agence FNAIM (via Bien'ici)",
    }
    return mapping.get(prefix, "Bien'ici (Agrégateur Agences)")


def resolve_bienici_zone_id(client: httpx.Client, city_name: str) -> List[str]:
    """Résout dynamiquement le zoneId Bien'ici pour n'importe quelle ville saisie par l'utilisateur."""
    c_norm = _normalize_city(city_name)
    for known_city, meta in CITY_METADATA.items():
        if _normalize_city(known_city) == c_norm and meta.get("bienici_zone_id"):
            return [meta["bienici_zone_id"]]

    try:
        url = f"https://www.bienici.com/place.json?q={urllib.parse.quote(city_name)}"
        resp = robust_http_get(client, url, max_retries=1)
        if resp and resp.status_code == 200:
            data = resp.json()
            zids = data.get("zoneIds", [])
            if zids:
                return [zids[0]]
    except Exception:
        pass
    return []


def scrape_bienici_live(criteria: Dict[str, Any], limit_per_city: int = 15, timeout: float = 12.0) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel l'agrégateur Bien'ici (qui regroupe IAD, Safti, Citya, Square Habitat,
    Guy Hoquet, ERA, Sergic et agences indépendantes de la métropole lilloise) ville par ville.
    """
    results: List[Dict[str, Any]] = []
    seen_ids = set()
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = float(criteria.get("min_surface", 80.0))
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    prop_types_map = []
    selected_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]
    if "maison" in selected_types:
        prop_types_map.append("house")
    if "appartement" in selected_types:
        prop_types_map.append("flat")
    if not prop_types_map:
        prop_types_map = ["house", "flat"]

    headers = get_browser_headers(accept_json=True)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, verify=False) as client:
        for city in cities:
            zone_ids = resolve_bienici_zone_id(client, city)
            if not zone_ids:
                continue

            filters = {
                "size": limit_per_city,
                "from": 0,
                "showAllModels": False,
                "filterType": "buy",
                "propertyType": prop_types_map,
                "minPrice": 70000,
                "maxPrice": max_budget,
                "minArea": min_surface,
                "sortBy": "publicationDate",
                "sortOrder": "desc",
                "zoneIdsByTypes": {"zoneIds": zone_ids},
            }
            url = "https://www.bienici.com/realEstateAds.json?filters=" + urllib.parse.quote(json.dumps(filters))
            try:
                resp = robust_http_get(client, url, max_retries=2)
                data = None
                if resp and resp.status_code == 200:
                    data = resp.json()
                elif not resp or resp.status_code in (403, 429):
                    pw_html = fetch_page_content_playwright(url, timeout=timeout)
                    if pw_html:
                        soup = BeautifulSoup(pw_html, "html.parser")
                        pre = soup.find("pre")
                        if pre and pre.text:
                            data = json.loads(pre.text)

                if not data:
                    continue

                for ad in data.get("realEstateAds", []):
                    ad_id = str(ad.get("id", "")).strip()
                    if not ad_id or ad_id in seen_ids:
                        continue

                    prop_type = "Maison" if ad.get("propertyType") == "house" else "Appartement"
                    if prop_type.lower() not in selected_types:
                        continue

                    ad_city = _canonical_city_name(str(ad.get("city") or city).strip(), cities)
                    if not _city_matches_targets(ad_city, cities):
                        continue

                    seen_ids.add(ad_id)
                    city_meta = _get_city_meta(ad_city)
                    postal_code = str(ad.get("postalCode") or city_meta.get("postal_code", "59000"))
                    rooms = int(ad.get("roomsQuantity") or 0)
                    surface = float(ad.get("surfaceArea") or 0.0)
                    price = int(ad.get("price") or 0)
                    if price < 70000 or price > max_budget or surface < min_surface:
                        continue

                    raw_desc = _clean_html_text(ad.get("description") or "")

                    extra_meta = []
                    if ad.get("bedroomsQuantity"):
                        extra_meta.append(f"{ad.get('bedroomsQuantity')} chambres")
                    if ad.get("hasTerrace"):
                        extra_meta.append("Terrasse")
                    if ad.get("hasGarden"):
                        extra_meta.append("Jardin")
                    if ad.get("hasCellar"):
                        extra_meta.append("Cave")
                    if ad.get("isCondominiumInProcedure") is True:
                        extra_meta.append("Procédure de copropriété en cours")
                    elif ad.get("isCondominiumInProcedure") is False:
                        extra_meta.append("Aucune procédure en cours")
                    if ad.get("workToDo"):
                        extra_meta.append("Travaux à prévoir")

                    full_desc = raw_desc
                    if extra_meta:
                        full_desc += f" [Infos techniques : {', '.join(extra_meta)}]"

                    account_name = str(ad.get("accountDisplayName") or "").strip()
                    title = (ad.get("title") or "").strip()
                    if not title or len(title) < 10:
                        raw_dist = ad.get("displayDistrictName")
                        district = f" ({raw_dist.strip()})" if isinstance(raw_dist, str) and raw_dist.strip() else ""
                        title = f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city}{district}"

                    has_parking_json = bool(
                        ad.get("hasParking") or ad.get("hasGarage") or (ad.get("parkingPlacesQuantity") or 0) > 0
                    )
                    has_parking = _detect_parking_in_text(f"{title} {full_desc}", explicit_flag=has_parking_json)

                    dpe = str(ad.get("energyClassification") or "Non renseigné").strip().upper()
                    if dpe not in ("A", "B", "C", "D", "E", "F", "G"):
                        m_dpe = re.search(r"\bdpe\s*[:\-]?\s*([a-g])\b", f"{title} {full_desc}", re.IGNORECASE)
                        dpe = m_dpe.group(1).upper() if m_dpe else "Non renseigné"

                    results.append(
                        {
                            "external_id": f"bienici-{ad_id}",
                            "source": _format_agency_source(ad_id, account_name),
                            "url": f"https://www.bienici.com/annonce/{ad_id}",
                            "title": title,
                            "property_type": prop_type,
                            "price": price,
                            "surface": surface,
                            "rooms": rooms,
                            "city": ad_city,
                            "postal_code": postal_code,
                            "description": full_desc or title,
                            "dpe": dpe,
                            "has_parking": has_parking,
                        }
                    )
            except Exception:
                continue

    return results


def _fetch_orpi_detail_enrichment(
    client: httpx.Client, ad_url: str
) -> Tuple[str, str, Optional[str], bool]:
    """
    Récupère sur la fiche détail Orpi :
    - la description intégrale non tronquée (`div.s-cms`)
    - la lettre DPE réelle (`window.dataLayer` `dpe: "1".."7"` ou `li.c-dpe__index--active`)
    - le code postal exact (`codePostal` dans `window.dataLayer`)
    - l'indicateur de parking/garage issu du `dataLayer` (`nbGarage`, `nbParking`, `stationnement`)
    """
    full_desc = ""
    dpe = "Non renseigné"
    postal_code: Optional[str] = None
    has_parking_dl = False

    dpe_num_map = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E", "6": "F", "7": "G"}

    try:
        resp = robust_http_get(client, ad_url, max_retries=1)
        if not resp or resp.status_code != 200:
            return full_desc, dpe, postal_code, has_parking_dl

        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        cms_div = soup.find("div", class_=re.compile(r"\bs-cms\b"))
        if cms_div:
            full_desc = _clean_html_text(str(cms_div))

        active_dpe = soup.find("li", class_=re.compile(r"c-dpe__index--active"))
        if active_dpe:
            txt = active_dpe.get_text(" ", strip=True)
            if txt and txt[0].upper() in ("A", "B", "C", "D", "E", "F", "G"):
                dpe = txt[0].upper()

        m_cp = re.search(r'codePostal\s*:\s*["\'](\d{5})["\']', html)
        if m_cp:
            postal_code = m_cp.group(1)

        if dpe == "Non renseigné":
            m_dpe = re.search(r'\bdpe\s*:\s*["\']([1-7A-Ga-g])["\']', html)
            if m_dpe:
                val = m_dpe.group(1).upper()
                dpe = dpe_num_map.get(val, val if val in "ABCDEFG" else "Non renseigné")

        m_garage = re.search(r'nbGarage\s*:\s*["\'](\d+)["\']', html)
        m_parking = re.search(r'nbParking\s*:\s*["\'](\d+)["\']', html)
        m_stat = re.search(r'stationnement\s*:\s*["\']([^"\']+)["\']', html)
        if (m_garage and int(m_garage.group(1)) > 0) or (m_parking and int(m_parking.group(1)) > 0):
            has_parking_dl = True
        elif m_stat and m_stat.group(1).strip() not in ("", "0", "non", "false"):
            has_parking_dl = True
    except Exception:
        pass

    return full_desc, dpe, postal_code, has_parking_dl


def scrape_orpi_live(criteria: Dict[str, Any], timeout: float = 12.0) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel les annonces des agences Orpi de la métropole lilloise
    via l'attribut JSON `data-result` des pages de recherche Orpi et enrichit chaque annonce
    éligible avec sa fiche détail (description complète, DPE officiel, parking, code postal).
    """
    results: List[Dict[str, Any]] = []
    seen_refs = set()
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = float(criteria.get("min_surface", 80.0))
    cities = criteria.get("cities", list(CITY_METADATA.keys()))
    selected_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]

    type_params = []
    if "maison" in selected_types:
        type_params.append("realEstateTypes[0]=maison")
    if "appartement" in selected_types:
        type_params.append(f"realEstateTypes[{len(type_params)}]=appartement")
    type_query = ("&" + "&".join(type_params)) if type_params else ""

    headers = get_browser_headers(accept_json=False)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, verify=False) as client:
        for city in cities:
            meta = _get_city_meta(city)
            orpi_slug = meta.get("orpi_slug") or _normalize_city(city).replace(" ", "-")
            url = (
                f"https://www.orpi.com/recherche/buy?locations[0][value]={urllib.parse.quote(orpi_slug)}"
                f"{type_query}&maxPrice={max_budget}&minSurface={int(min_surface)}"
            )
            try:
                resp = robust_http_get(client, url, max_retries=1)
                html_text = ""
                if resp and resp.status_code == 200:
                    html_text = resp.text
                elif not resp or resp.status_code in (403, 429):
                    html_text = fetch_page_content_playwright(url, timeout=timeout) or ""

                if not html_text:
                    continue

                soup = BeautifulSoup(html_text, "html.parser")
                div = soup.find("div", attrs={"data-result": True})
                if not div:
                    continue
                payload = json.loads(div["data-result"])

                candidates = list(payload.get("items") or []) + list(payload.get("similars") or [])
                for item in candidates:
                    slug = item.get("slug")
                    ref = str(item.get("reference") or item.get("id") or "").strip()
                    if not slug or not ref or ref in seen_refs:
                        continue

                    raw_type = str(item.get("type") or "").lower()
                    prop_type = "Maison" if "maison" in raw_type else "Appartement"
                    if selected_types and prop_type.lower() not in selected_types:
                        continue

                    price = int(item.get("price") or 0)
                    surface = float(item.get("surface") or 0.0)
                    if price < 70000 or price > max_budget or surface < min_surface:
                        continue

                    raw_city = item.get("city")
                    if isinstance(raw_city, dict):
                        ad_city = str(raw_city.get("name") or city).strip()
                    else:
                        ad_city = str(raw_city or city).strip()
                    ad_city = _canonical_city_name(ad_city, cities)

                    if not _city_matches_targets(ad_city, cities):
                        continue

                    seen_refs.add(ref)

                    raw_district = item.get("district")
                    if isinstance(raw_district, dict):
                        district_name = str(raw_district.get("name") or "").strip()
                    else:
                        district_name = str(raw_district or "").strip()

                    rooms = int(item.get("nbRooms") or 0)
                    short_ad = _clean_html_text(str(item.get("longAd") or ""))
                    tags = [str(t) for t in (item.get("tags") or [])]
                    if item.get("isLifeAnnuity"):
                        tags.append("Viager")
                    if item.get("isRentalInvestment"):
                        tags.append("Investissement locatif / Vendu loué")

                    ad_url = f"https://www.orpi.com/annonce-vente-{slug}/"
                    detail_desc, detail_dpe, detail_cp, has_parking_dl = _fetch_orpi_detail_enrichment(client, ad_url)

                    full_desc = detail_desc if len(detail_desc) > len(short_ad) else short_ad
                    if tags:
                        full_desc += f" [Tags Orpi : {', '.join(tags)}]"

                    raw_item_dpe = str(item.get("dpe") or "").strip().upper()
                    if raw_item_dpe in ("A", "B", "C", "D", "E", "F", "G"):
                        dpe = raw_item_dpe
                    elif detail_dpe in ("A", "B", "C", "D", "E", "F", "G"):
                        dpe = detail_dpe
                    else:
                        m_dpe = re.search(r"\bdpe\s*[:\-]?\s*([a-g])\b", full_desc, re.IGNORECASE)
                        dpe = m_dpe.group(1).upper() if m_dpe else "Non renseigné"

                    district_suffix = f" ({district_name})" if district_name else ""
                    title = f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city}{district_suffix}"

                    has_parking_tags = any(
                        any(w in t.lower() for w in PARKING_KEYWORDS) for t in tags
                    )
                    has_parking = _detect_parking_in_text(
                        f"{title} {full_desc}",
                        explicit_flag=(has_parking_dl or has_parking_tags),
                    )

                    agency_info = item.get("agency") or {}
                    agency_name = agency_info.get("name") if isinstance(agency_info, dict) else "Orpi"
                    ad_city_meta = _get_city_meta(ad_city)
                    postal_code = detail_cp or ad_city_meta.get("postal_code", "59000")

                    results.append(
                        {
                            "external_id": f"orpi-{ref}",
                            "source": f"Orpi ({agency_name})" if agency_name else "Orpi",
                            "url": ad_url,
                            "title": title,
                            "property_type": prop_type,
                            "price": price,
                            "surface": surface,
                            "rooms": rooms,
                            "city": ad_city,
                            "postal_code": postal_code,
                            "description": full_desc or f"Annonce Orpi {slug}",
                            "dpe": dpe,
                            "has_parking": has_parking,
                        }
                    )
            except Exception:
                continue

    return results


def resolve_seloger_place_id(client: httpx.Client, city_name: str) -> str:
    """Résout le placeId SeLoger (ex: AD08FR23435) pour une commune donnée."""
    c_norm = _normalize_city(city_name)
    for known_city, meta in CITY_METADATA.items():
        if _normalize_city(known_city) == c_norm and meta.get("seloger_place_id"):
            return meta["seloger_place_id"]

    slug = c_norm.replace(" ", "-")
    try:
        url = f"https://www.seloger.com/immobilier/achat/immo-{slug}-59/"
        resp = robust_http_get(client, url, max_retries=1)
        if resp and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for s in soup.find_all("script"):
                txt = s.string or ""
                if "__UFRN_FETCHER__" in txt:
                    start = txt.find("JSON.parse(") + len("JSON.parse(")
                    end = txt.rfind(")")
                    data = json.loads(json.loads(txt[start:end]))
                    locs = (
                        data.get("data", {})
                        .get("classified-serp-data", {})
                        .get("searchModel", {})
                        .get("locations", [])
                    )
                    if locs and locs[0].get("placeId"):
                        return str(locs[0]["placeId"])
    except Exception:
        pass
    return ""


def scrape_seloger_live(criteria: Dict[str, Any], timeout: float = 12.0) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel les annonces SeLoger.com (via le flux SSR __UFRN_FETCHER__
    de /classified-search) pour les villes, budget et surface sélectionnés,
    avec extraction du DPE officiel (`energyClass`), filtrage des suggestions hors-secteur
    et bascule automatique Playwright en cas de défi anti-bot / DataDome.
    """
    results: List[Dict[str, Any]] = []
    seen_ids = set()
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = float(criteria.get("min_surface", 80.0))
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    estate_types = []
    selected_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]
    if "maison" in selected_types:
        estate_types.append("House")
    if "appartement" in selected_types:
        estate_types.append("Apartment")
    if not estate_types:
        estate_types = ["House", "Apartment"]

    seloger_headers = get_browser_headers(accept_json=False)

    with httpx.Client(headers=seloger_headers, timeout=timeout, follow_redirects=True, verify=False) as client:
        for city in cities:
            place_id = resolve_seloger_place_id(client, city)
            if not place_id:
                continue

            url = (
                f"https://www.seloger.com/classified-search?"
                f"distributionTypes=Buy&estateTypes={','.join(estate_types)}"
                f"&locations={place_id}&priceMin=70000&priceMax={max_budget}&spaceMin={int(min_surface)}"
            )
            try:
                resp = robust_http_get(client, url, max_retries=1)
                html_text = ""
                used_playwright = False
                if resp and resp.status_code == 200:
                    html_text = resp.text
                elif not resp or resp.status_code in (403, 429):
                    html_text = fetch_page_content_playwright(url, timeout=timeout) or ""
                    used_playwright = True

                if not html_text:
                    continue

                soup = BeautifulSoup(html_text, "html.parser")

                for s in soup.find_all("script"):
                    txt = s.string or ""
                    if "__UFRN_FETCHER__" not in txt:
                        continue
                    try:
                        start = txt.find("JSON.parse(") + len("JSON.parse(")
                        end = txt.rfind(")")
                        data = json.loads(json.loads(txt[start:end]))
                        serp = data.get("data", {}).get("classified-serp-data", {})
                        cdata = serp.get("classifiedsData", {})
                    except Exception:
                        continue

                    if not cdata:
                        continue

                    enriched_meta = serp.get("enrichedClassifiedsData", {})
                    suggested_ids = set(str(x) for x in (serp.get("suggestedClassifieds") or []))

                    for ad_id, item in cdata.items():
                        ad_id_str = str(ad_id).strip()
                        if not ad_id_str or ad_id_str in seen_ids:
                            continue

                        is_suggestion = (
                            ad_id_str in suggested_ids
                            or bool((enriched_meta.get(ad_id_str) or {}).get("isSuggestion"))
                        )

                        raw = item.get("rawData") or {}
                        price = int(raw.get("price") or 0)
                        surf_obj = raw.get("surface") or {}
                        surface = float(surf_obj.get("main") or 0.0)
                        if price < 70000 or price > max_budget or surface < min_surface:
                            continue

                        prop_raw = str(raw.get("propertyType") or "").upper()
                        prop_type = "Maison" if "HOUSE" in prop_raw else "Appartement"
                        if selected_types and prop_type.lower() not in selected_types:
                            continue

                        rooms = int(raw.get("nbroom") or 0)
                        bedrooms = int(raw.get("nbbedroom") or 0)

                        loc_addr = (item.get("location") or {}).get("address") or {}
                        ad_city = _canonical_city_name(str(loc_addr.get("city") or city).strip(), cities)
                        if is_suggestion and not _city_matches_targets(ad_city, cities):
                            continue
                        if not _city_matches_targets(ad_city, cities):
                            continue

                        seen_ids.add(ad_id_str)
                        ad_city_meta = _get_city_meta(ad_city)
                        zip_code = str(loc_addr.get("zipCode") or ad_city_meta.get("postal_code", "59000"))
                        district = str(loc_addr.get("district") or "").strip()

                        main_desc = item.get("mainDescription") or {}
                        headline = _clean_html_text(str(main_desc.get("headline") or ""))
                        desc_body = _clean_html_text(str(main_desc.get("description") or ""))

                        hard_facts = item.get("hardFacts") or {}
                        keyfacts = [str(kf) for kf in (hard_facts.get("keyfacts") or [])]
                        if bedrooms and not any("chambre" in kf.lower() for kf in keyfacts):
                            keyfacts.append(f"{bedrooms} chambres")

                        provider = item.get("provider") or {}
                        agency_card = provider.get("intermediaryCard") or {}
                        agency_title = str(agency_card.get("title") or "Agence SeLoger").strip()

                        full_title = (
                            headline
                            if len(headline) > 12
                            else f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city}"
                        )
                        if district and district.lower() not in full_title.lower():
                            full_title += f" ({district})"

                        # Extraction du DPE depuis le champ JSON officiel `energyClass` de SeLoger
                        raw_energy = str(item.get("energyClass") or "").strip().upper()
                        if raw_energy in ("A", "B", "C", "D", "E", "F", "G"):
                            dpe = raw_energy
                        else:
                            m_dpe = re.search(
                                r"\bdpe\s*[:\-]?\s*([a-g])\b",
                                f"{full_title} {desc_body}",
                                re.IGNORECASE,
                            )
                            dpe = m_dpe.group(1).upper() if m_dpe else "Non renseigné"

                        full_desc = desc_body
                        if keyfacts:
                            full_desc += f" [Caractéristiques SeLoger : {', '.join(keyfacts)}]"
                        if surf_obj.get("plot"):
                            full_desc += f" [Terrain : {surf_obj.get('plot')} m²]"
                        if dpe != "Non renseigné":
                            full_desc += f" [DPE : {dpe}]"

                        has_parking = _detect_parking_in_text(f"{full_title} {full_desc}")

                        ad_url = str(item.get("url") or f"https://www.seloger.com/annonce/achat/{ad_id_str}")
                        if ad_url.startswith("/"):
                            ad_url = "https://www.seloger.com" + ad_url

                        src_tag = f"SeLoger ({agency_title})"
                        if used_playwright:
                            src_tag += " [Playwright]"

                        results.append(
                            {
                                "external_id": f"seloger-{ad_id_str}",
                                "source": src_tag,
                                "url": ad_url,
                                "title": full_title,
                                "property_type": prop_type,
                                "price": price,
                                "surface": surface,
                                "rooms": rooms,
                                "city": ad_city,
                                "postal_code": zip_code,
                                "description": full_desc or full_title,
                                "dpe": dpe,
                                "has_parking": has_parking,
                            }
                        )
            except Exception:
                continue

    return results


def scrape_all_live_sources(criteria: Dict[str, Any]) -> Dict[str, Any]:
    """
    Exécute le scraping live multi-sources (SeLoger + Bien'ici + Réseaux d'Agences & Orpi)
    avec résilience réseau et bypass WAF automatique.
    """
    seloger_ads = scrape_seloger_live(criteria)
    bienici_ads = scrape_bienici_live(criteria, limit_per_city=15)
    orpi_ads = scrape_orpi_live(criteria)
    combined = seloger_ads + bienici_ads + orpi_ads
    return {
        "ads": combined,
        "stats": {
            "seloger": len(seloger_ads),
            "bienici_agences": len(bienici_ads),
            "orpi_direct": len(orpi_ads),
            "total": len(combined),
        },
    }


def build_portal_search_links(criteria: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """
    Génère les liens directs vers les recherches pré-filtrées (Budget, Surface, Ville)
    sur Bien'ici, Orpi, LeBonCoin et SeLoger pour chaque ville sélectionnée.
    """
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = int(criteria.get("min_surface", 80))
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    links_by_city: Dict[str, List[Dict[str, str]]] = {}
    for city in cities:
        meta = _get_city_meta(city)
        cp = meta["postal_code"]
        slug = meta["slug"]
        orpi_slug = meta.get("orpi_slug", slug)

        bienici_url = (
            f"https://www.bienici.com/recherche/achat/{slug}"
            f"?prix-max={max_budget}&surface-min={min_surface}&parking=true"
        )
        orpi_url = (
            f"https://www.orpi.com/recherche/buy?locations[0][value]={orpi_slug}"
            f"&maxPrice={max_budget}&minSurface={min_surface}"
        )
        leboncoin_query = urllib.parse.quote(f"{city} {cp}")
        leboncoin_url = (
            f"https://www.leboncoin.fr/recherche?category=9&locations={leboncoin_query}"
            f"&price=min-{max_budget}&square={min_surface}-max&real_estate_type=1,2"
        )
        seloger_place = meta.get("seloger_place_id", "")
        if seloger_place:
            seloger_url = (
                f"https://www.seloger.com/classified-search?distributionTypes=Buy"
                f"&estateTypes=House,Apartment&locations={seloger_place}"
                f"&priceMax={max_budget}&spaceMin={min_surface}"
            )
        else:
            seloger_url = f"https://www.seloger.com/immobilier/achat/immo-{slug}/"

        links_by_city[city] = [
            {"portail": "Bien'ici (Agrégateur)", "url": bienici_url},
            {"portail": "Orpi Agences", "url": orpi_url},
            {"portail": "LeBonCoin", "url": leboncoin_url},
            {"portail": "SeLoger", "url": seloger_url},
        ]
    return links_by_city
