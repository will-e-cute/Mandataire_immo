import hashlib
import json
import random
import re
import time
import unicodedata
import urllib.parse
from typing import Any, Dict, List, Optional
import httpx
from bs4 import BeautifulSoup

def _normalize_city(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower().strip()
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[-'’_./]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

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
}

ROTATING_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]


def get_browser_headers(accept_json: bool = False) -> Dict[str, str]:
    ua = random.choice(ROTATING_USER_AGENTS)
    headers = {
        "User-Agent": ua,
        "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "empty" if accept_json else "document",
        "Sec-Fetch-Mode": "cors" if accept_json else "navigate",
        "Sec-Fetch-Site": "same-origin",
    }
    if accept_json:
        headers["Accept"] = "application/json, text/plain, */*"
    else:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
    return headers


DEFAULT_HEADERS = get_browser_headers(accept_json=False)


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
        if _normalize_city(known_city) == c_norm:
            return [meta["bienici_zone_id"]]

    try:
        url = f"https://www.bienici.com/place.json?q={urllib.parse.quote(city_name)}"
        resp = robust_http_get(client, url, max_retries=1)
        if resp and resp.status_code == 200:
            data = resp.json()
            zids = data.get("zoneIds", [])
            if zids:
                return zids
    except Exception:
        pass
    return []


def scrape_bienici_live(criteria: Dict[str, Any], limit_per_city: int = 12, timeout: float = 12.0) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel l'agrégateur Bien'ici (qui regroupe IAD, Safti, Citya, Square Habitat,
    Guy Hoquet, ERA, Sergic et agences indépendantes de la métropole lilloise) ville par ville.
    """
    results: List[Dict[str, Any]] = []
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
                    # Fallback Playwright si WAF activé sur Bien'ici
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
                    if not ad_id:
                        continue

                    prop_type = "Maison" if ad.get("propertyType") == "house" else "Appartement"
                    ad_city = ad.get("city") or city
                    postal_code = str(ad.get("postalCode") or CITY_METADATA.get(city, {}).get("postal_code", ""))
                    rooms = int(ad.get("roomsQuantity") or 0)
                    surface = float(ad.get("surfaceArea") or 0.0)
                    price = int(ad.get("price") or 0)
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

                    has_parking_json = ad.get("hasParking") or ad.get("hasGarage") or (ad.get("parkingPlacesQuantity") or 0) > 0
                    desc_lower = full_desc.lower()
                    has_parking_text = any(
                        w in desc_lower
                        for w in ["garage", "box", "parking", "stationnement priv", "carport", "place de parc"]
                    )
                    has_parking = True if (has_parking_json or has_parking_text) else False

                    dpe = str(ad.get("energyClassification") or "Non renseigné").strip().upper()
                    if dpe not in ("A", "B", "C", "D", "E", "F", "G"):
                        dpe = "Non renseigné"

                    account_name = str(ad.get("accountDisplayName") or "").strip()
                    title = (ad.get("title") or "").strip()
                    if not title or len(title) < 10:
                        district = f" ({ad.get('displayDistrictName')})" if ad.get("displayDistrictName") else ""
                        title = f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city}{district}"

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


def scrape_orpi_live(criteria: Dict[str, Any], timeout: float = 12.0) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel les annonces des agences Orpi de la métropole lilloise
    via l'attribut JSON `data-result` des pages de recherche Orpi avec résilience HTTP & Playwright.
    """
    results: List[Dict[str, Any]] = []
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = int(criteria.get("min_surface", 80))
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    headers = get_browser_headers(accept_json=False)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, verify=False) as client:
        for city in cities:
            meta = CITY_METADATA.get(city, {})
            orpi_slug = meta.get("orpi_slug") or city.lower().replace("'", "-").replace(" ", "-").replace("œ", "oe")
            url = (
                f"https://www.orpi.com/recherche/buy?locations[0][value]={urllib.parse.quote(orpi_slug)}"
                f"&maxPrice={max_budget}&minSurface={min_surface}"
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
                for item in payload.get("items", []):
                    slug = item.get("slug")
                    ref = str(item.get("reference") or item.get("id") or "")
                    if not slug or not ref:
                        continue

                    raw_type = str(item.get("type") or "").lower()
                    prop_type = "Maison" if "maison" in raw_type else "Appartement"
                    price = int(item.get("price") or 0)
                    surface = float(item.get("surface") or 0.0)
                    if price <= 0 or surface <= 0:
                        continue

                    rooms = int(item.get("nbRooms") or 0)
                    ad_city = str(item.get("city") or city)
                    long_ad = _clean_html_text(str(item.get("longAd") or ""))
                    tags = [str(t) for t in (item.get("tags") or [])]
                    if item.get("isLifeAnnuity"):
                        tags.append("Viager")
                    if item.get("isRentalInvestment"):
                        tags.append("Investissement locatif / Vendu loué")

                    full_desc = long_ad
                    if tags:
                        full_desc += f" [Tags Orpi : {', '.join(tags)}]"

                    desc_lower = full_desc.lower()
                    has_parking = any(
                        w in desc_lower for w in ["garage", "box", "parking", "stationnement", "carport"]
                    )
                    agency_info = item.get("agency") or {}
                    agency_name = agency_info.get("name") if isinstance(agency_info, dict) else "Orpi"

                    results.append(
                        {
                            "external_id": f"orpi-{ref}",
                            "source": f"Orpi ({agency_name})" if agency_name else "Orpi",
                            "url": f"https://www.orpi.com/annonce-vente-{slug}/",
                            "title": f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city} ({item.get('district') or 'Orpi'})",
                            "property_type": prop_type,
                            "price": price,
                            "surface": surface,
                            "rooms": rooms,
                            "city": ad_city,
                            "postal_code": meta.get("postal_code", "59000"),
                            "description": full_desc or f"Annonce Orpi {slug}",
                            "dpe": str(item.get("dpe") or "Non renseigné").upper()
                            if str(item.get("dpe") or "").upper() in ("A", "B", "C", "D", "E", "F", "G")
                            else "Non renseigné",
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
        if _normalize_city(known_city) == c_norm:
            return meta["seloger_place_id"]

    slug = city_name.lower().strip().replace("'", "-").replace(" ", "-").replace("œ", "oe")
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
    avec bascule automatique Playwright en cas de défi anti-bot / DataDome.
    """
    results: List[Dict[str, Any]] = []
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = int(criteria.get("min_surface", 80))
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
                f"&locations={place_id}&priceMin=70000&priceMax={max_budget}&spaceMin={min_surface}"
            )
            try:
                resp = robust_http_get(client, url, max_retries=1)
                html_text = ""
                used_playwright = False
                if resp and resp.status_code == 200:
                    html_text = resp.text
                elif not resp or resp.status_code in (403, 429):
                    # WAF DataDome détecté -> bascule automatique sur Chromium Playwright
                    html_text = fetch_page_content_playwright(url, timeout=timeout) or ""
                    used_playwright = True

                if not html_text:
                    continue

                soup = BeautifulSoup(html_text, "html.parser")
                classifieds_found = False

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

                    classifieds_found = True
                    for ad_id, item in cdata.items():
                        raw = item.get("rawData") or {}
                        price = int(raw.get("price") or 0)
                        surf_obj = raw.get("surface") or {}
                        surface = float(surf_obj.get("main") or 0.0)
                        if price < 70000 or price > max_budget or surface < min_surface:
                            continue

                        prop_raw = str(raw.get("propertyType") or "").upper()
                        prop_type = "Maison" if "HOUSE" in prop_raw else "Appartement"
                        rooms = int(raw.get("nbroom") or 0)

                        loc_addr = (item.get("location") or {}).get("address") or {}
                        ad_city = str(loc_addr.get("city") or city)
                        zip_code = str(loc_addr.get("zipCode") or CITY_METADATA.get(city, {}).get("postal_code", "59000"))
                        district = str(loc_addr.get("district") or "").strip()

                        main_desc = item.get("mainDescription") or {}
                        headline = _clean_html_text(str(main_desc.get("headline") or ""))
                        desc_body = _clean_html_text(str(main_desc.get("description") or ""))

                        hard_facts = item.get("hardFacts") or {}
                        keyfacts = [str(kf) for kf in (hard_facts.get("keyfacts") or [])]

                        provider = item.get("provider") or {}
                        agency_card = provider.get("intermediaryCard") or {}
                        agency_title = str(agency_card.get("title") or "Agence SeLoger").strip()

                        full_title = headline if len(headline) > 12 else f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city}"
                        if district and district.lower() not in full_title.lower():
                            full_title += f" ({district})"

                        full_desc = desc_body
                        if keyfacts:
                            full_desc += f" [Caractéristiques SeLoger : {', '.join(keyfacts)}]"
                        if surf_obj.get("plot"):
                            full_desc += f" [Terrain : {surf_obj.get('plot')} m²]"

                        desc_lower = f"{full_title} {full_desc}".lower()
                        has_parking = any(
                            w in desc_lower
                            for w in ["garage", "box", "parking", "stationnement", "carport", "place de parc"]
                        )

                        dpe = "Non renseigné"
                        m_dpe = re.search(r"\bdpe\s*[:\-]?\s*([a-g])\b", desc_lower, re.IGNORECASE)
                        if m_dpe:
                            dpe = m_dpe.group(1).upper()

                        ad_url = str(item.get("url") or f"https://www.seloger.com/annonce/achat/{ad_id}")
                        if ad_url.startswith("/"):
                            ad_url = "https://www.seloger.com" + ad_url

                        src_tag = f"SeLoger ({agency_title})"
                        if used_playwright:
                            src_tag += " [Playwright]"

                        results.append(
                            {
                                "external_id": f"seloger-{ad_id}",
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
    bienici_ads = scrape_bienici_live(criteria, limit_per_city=12)
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
    sur Bien'ici, Orpi, Laforêt, LeBonCoin et SeLoger pour chaque ville sélectionnée.
    """
    max_budget = int(criteria.get("max_budget", 280000))
    min_surface = int(criteria.get("min_surface", 80))
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    links_by_city: Dict[str, List[Dict[str, str]]] = {}
    for city in cities:
        meta = CITY_METADATA.get(
            city,
            {
                "postal_code": "59000",
                "slug": city.lower().replace(" ", "-"),
                "orpi_slug": city.lower().replace(" ", "-"),
            },
        )
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
