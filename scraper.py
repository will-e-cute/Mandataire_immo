import concurrent.futures
import difflib
import json
import random
import re
import threading
import time
import unicodedata
import urllib.parse
from typing import Any, Callable, Dict, List, Optional, Tuple
import httpx
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except (ImportError, ModuleNotFoundError):
    cffi_requests = None
    HAS_CURL_CFFI = False

_THREAD_LOCAL = threading.local()


def _get_thread_cffi_session() -> Optional[Any]:
    """Réutilise une session HTTP/2 TLS Keep-Alive curl_cffi par thread pour accélérer les requêtes successives."""
    if not HAS_CURL_CFFI or cffi_requests is None:
        return None
    sess = getattr(_THREAD_LOCAL, "cffi_session", None)
    if sess is None:
        sess = cffi_requests.Session(impersonate="chrome")
        _THREAD_LOCAL.cffi_session = sess
    return sess

CITY_TYPO_FIXES = {
    "bareoul": "baroeul",
    "marcq en bareoul": "marcq en baroeul",
    "villeneuve dascq": "villeneuve d ascq",
}


def _normalize_city(text: str) -> str:
    """Normalise le nom d'une commune (sans accents, ligatures ni ponctuation, avec correction des fautes de frappe courantes)."""
    if not text:
        return ""
    text = str(text).lower().strip()
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[-'’_./]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for wrong, right in CITY_TYPO_FIXES.items():
        if wrong in text:
            text = text.replace(wrong, right)
    return text


def _match_known_city(city_name: str) -> Optional[str]:
    """Trouve le nom canonique dans CITY_METADATA même en cas de légère faute de frappe (ex: 'marcq en bareoul')."""
    norm = _normalize_city(city_name)
    if not norm:
        return None
    norm_map = {_normalize_city(k): k for k in CITY_METADATA}
    if norm in norm_map:
        return norm_map[norm]
    close = difflib.get_close_matches(norm, list(norm_map.keys()), n=1, cutoff=0.82)
    if close:
        return norm_map[close[0]]
    return None


def _city_matches_targets(ad_city: str, target_cities: List[str]) -> bool:
    """Vérifie si la ville d'une annonce correspond à l'une des villes recherchées."""
    if not target_cities:
        return True
    norm_ad = _normalize_city(ad_city)
    if not norm_ad:
        return True
    canon_ad = _match_known_city(ad_city)
    for target in target_cities:
        norm_t = _normalize_city(target)
        if norm_t and (norm_t == norm_ad or norm_t in norm_ad or norm_ad in norm_t):
            return True
        if canon_ad and _match_known_city(target) == canon_ad:
            return True
    return False


def _canonical_city_name(ad_city: str, target_cities: Optional[List[str]] = None) -> str:
    """Unifie la graphie du nom de ville (ex: 'marcq en bareoul' / 'Marcq-en-Baroeul' -> 'Marcq-en-Barœul') pour éviter les doublons de filtre."""
    matched = _match_known_city(ad_city)
    if matched:
        return matched
    norm_ad = _normalize_city(ad_city)
    if not norm_ad:
        return ad_city.strip()
    for target in (target_cities or []):
        if _normalize_city(target) == norm_ad:
            matched_t = _match_known_city(target)
            return matched_t or target.strip()
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
    décompresse nativement les réponses gzip/deflate d'Orpi et Bien'ici.
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

STRONG_PARKING_KEYWORDS = [
    "garage",
    "box",
    "carport",
    "parking priv",
    "stationnement priv",
    "place de parking",
    "place de parc",
    "2 places de parking",
]

PARKING_KEYWORDS = [
    "garage",
    "box",
    "parking",
    "stationnement",
    "carport",
    "place de parc",
    "stationner",
]

STRICT_NO_PARKING_KEYWORDS = [
    "sans garage",
    "pas de garage",
    "aucun garage",
    "sans parking",
    "pas de parking",
    "aucun stationnement priv",
]

STREET_PARKING_KEYWORDS = [
    "stationnement dans la rue",
    "parking facile dans la rue",
    "stationnement facile dans la rue",
    "stationnement gratuit dans la rue",
]


def _detect_parking_in_text(text: str, explicit_flag: Optional[bool] = None) -> bool:
    """
    Détecte la présence d'un stationnement privatif (garage/box/parking) sans faux négatif
    lorsqu'une annonce mentionne à la fois un garage ET du stationnement facile dans la rue.
    """
    text_lower = (text or "").lower()
    if any(neg in text_lower for neg in STRICT_NO_PARKING_KEYWORDS):
        return False
    if explicit_flag is True:
        return True
    if any(strong in text_lower for strong in STRONG_PARKING_KEYWORDS):
        return True
    if any(street in text_lower for street in STREET_PARKING_KEYWORDS):
        return False
    return any(kw in text_lower for kw in PARKING_KEYWORDS)


def _get_city_meta(city_name: str) -> Dict[str, str]:
    matched = _match_known_city(city_name)
    if matched and matched in CITY_METADATA:
        return CITY_METADATA[matched]
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


def _infer_referer(url: str) -> str:
    if "bienici.com" in url:
        return "https://www.bienici.com/recherche/achat/france"
    if "orpi.com" in url:
        return "https://www.orpi.com/"
    return "https://www.google.fr/"


def robust_http_get(
    client: httpx.Client,
    url: str,
    max_retries: int = 2,
    backoff: float = 1.0,
) -> Optional[Any]:
    """
    Exécute une requête GET avec contournement anti-WAF (empreinte TLS/HTTP2 Chrome via curl_cffi
    pour Streamlit Cloud, et fallback httpx) avec retry et backoff exponentiel.
    """
    accept_json = ".json" in url or "realEstateAds" in url or "place.json" in url
    req_headers = get_browser_headers(accept_json=accept_json)
    req_headers["Referer"] = _infer_referer(url)

    cffi_sess = _get_thread_cffi_session()
    for attempt in range(max_retries + 1):
        # 1. Priorité à la session curl_cffi Keep-Alive (empreinte TLS JA3/JA4 Chrome native + HTTP/2 multiplexing)
        if cffi_sess is not None:
            try:
                cffi_resp = cffi_sess.get(
                    url,
                    headers=req_headers,
                    verify=False,
                    timeout=12,
                    allow_redirects=True,
                )
                if cffi_resp.status_code in (200, 404, 410):
                    return cffi_resp
                if cffi_resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                    time.sleep(backoff * (attempt + 1))
                    continue
            except Exception:
                pass

        # 2. Fallback httpx standard
        try:
            resp = client.get(url, headers=req_headers)
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
    (ou Microsoft Edge / Google Chrome système si le binaire Playwright n'est pas installé)
    si la requête HTTP simple échoue.
    """
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError):
        return None

    try:
        with sync_playwright() as p:
            browser = None
            launch_args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
            for chan in (None, "msedge", "chrome"):
                try:
                    if chan:
                        browser = p.chromium.launch(channel=chan, headless=True, args=launch_args)
                    else:
                        browser = p.chromium.launch(headless=True, args=launch_args)
                    break
                except Exception:
                    continue
            if not browser:
                return None

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


_UNAVAILABLE_EXACT_TAGS = {
    "vendu",
    "vendue",
    "sous compromis",
    "sous offre",
    "sous option",
    "réservé",
    "réservée",
    "reserve",
    "reservee",
    "indisponible",
    "expiré",
    "expirée",
    "expire",
    "expiree",
    "clôturé",
    "clôturée",
    "sold",
    "under_offer",
}

_UNAVAILABLE_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bsous\s+compromis\b", re.IGNORECASE), "Bien sous compromis"),
    (
        re.compile(r"\bcompromis\s+(?:de\s+vente\s+)?(?:sign[eé]|en\s+cours|accept[eé])\b", re.IGNORECASE),
        "Bien sous compromis",
    ),
    (re.compile(r"\bsous\s+offre\b", re.IGNORECASE), "Bien sous offre"),
    (
        re.compile(r"\boffre\s+(?:d'achat\s+)?(?:accept[eé]e|en\s+cours|valid[eé]e)\b", re.IGNORECASE),
        "Bien sous offre",
    ),
    (re.compile(r"\bsous\s+option\b", re.IGNORECASE), "Bien sous option"),
    (
        re.compile(r"\boption\s+d'achat\s+(?:en\s+cours|accept[eé]e)\b", re.IGNORECASE),
        "Bien sous option",
    ),
    (re.compile(r"\bd[eé]j[aà]\s+vendue?\b", re.IGNORECASE), "Bien déjà vendu"),
    (re.compile(r"\ba\s+trouv[eé]\s+(?:son\s+|un\s+)?preneur\b", re.IGNORECASE), "Bien déjà vendu"),
    (re.compile(r"\b(?:c'est\s+)?trop\s+tard\b", re.IGNORECASE), "Bien déjà vendu"),
    (
        re.compile(
            r"\bvendue?\s+(?:en\s+\d+\s*(?:visite|jour|semaine|heure)s?|par\s+(?:notre\s+agence|l'agence|nos\s+soins|orpi|iad|safti|citya|century|era|guy\s+hoquet|square\s+habitat|lafor[eê]t|st[eé]phane\s+plaza|foncia|nexity)|gr[aâ]ce\s+[aà])\b",
            re.IGNORECASE,
        ),
        "Bien déjà vendu",
    ),
    (re.compile(r"\bvendue?\s*!", re.IGNORECASE), "Bien déjà vendu"),
    (
        re.compile(
            r"\b(?:ce\s+bien|cette\s+maison|cet\s+appartement|l'appartement|la\s+maison)\s+(?:est|a\s+[eé]t[eé])\s+vendue?\b(?!\s+(?:lou[eé]e?|libre|vide|meubl[eé]e?|non\s+meubl[eé]e?|en\s+l'[eé]tat|en\s+viager|occup[eé]e?|avec))",
            re.IGNORECASE,
        ),
        "Bien déjà vendu",
    ),
    (
        re.compile(
            r"\bbien\s+vendue?\b(?!\s+(?:lou[eé]e?|libre|vide|meubl[eé]e?|non\s+meubl[eé]e?|en\s+l'[eé]tat|en\s+viager|occup[eé]e?|avec))",
            re.IGNORECASE,
        ),
        "Bien déjà vendu",
    ),
    (
        re.compile(
            r"(?:^|[\n\r])\s*[_\-\*\[\(★⭐🔥✅❌🔴🟢•|/]*\s*vendue?\b(?!\s+(?:lou[eé]e?|libre|vide|meubl[eé]e?|non\s+meubl[eé]e?|en\s+l'[eé]tat|en\s+viager|occup[eé]e?|avec\s+(?:un|une|des|son|sa|ses|garage|parking|box|jardin|locataire|terrain|cave|meuble|cuisine|travaux|permis|\d)))",
            re.IGNORECASE,
        ),
        "Bien déjà vendu",
    ),
    (
        re.compile(
            r"\b(?:n'est\s+plus\s+disponible|plus\s+disponible|bien\s+indisponible|annonce\s+(?:expir[eé]e|retir[eé]e|d[eé]sactiv[eé]e|cl[oô]tur[eé]e))\b",
            re.IGNORECASE,
        ),
        "Annonce plus disponible",
    ),
    (
        re.compile(
            r"\b(?:bien|maison|appartement|logement)\s+(?:d[eé]j[aà]\s+)?r[eé]serv[eé]e?\b|\bd[eé]j[aà]\s+r[eé]serv[eé]e?\b|(?:^|[\n\r])\s*[_\-\*\[\(]*\s*r[eé]serv[eé]e?\s*!",
            re.IGNORECASE,
        ),
        "Bien réservé",
    ),
]

_RENT_UNAVAILABLE_RE = re.compile(r"\bd[eé]j[aà]\s+lou[eé]e?\b", re.IGNORECASE)


def detect_unavailable_reason(
    title: str,
    description: str,
    tags: Optional[List[str]] = None,
    tx_type: str = "buy",
) -> Optional[str]:
    """
    Détecte de façon fiable si une annonce indique un bien vendu, sous compromis,
    sous offre/option, réservé ou retiré, sans faux positifs sur 'vendu loué',
    'vendu libre' ou 'sans compromis'.
    """
    for raw_tag in (tags or []):
        tag_norm = str(raw_tag or "").replace("’", "'").strip().lower()
        if tag_norm in _UNAVAILABLE_EXACT_TAGS:
            if "compromis" in tag_norm:
                return "Bien sous compromis"
            if "offre" in tag_norm or tag_norm == "under_offer":
                return "Bien sous offre"
            if "option" in tag_norm:
                return "Bien sous option"
            if "vendu" in tag_norm or tag_norm == "sold":
                return "Bien déjà vendu"
            if "serv" in tag_norm:
                return "Bien réservé"
            return "Annonce plus disponible"

    is_rent = str(tx_type or "buy").strip().lower() in ("rent", "location")
    fields = [title or "", description or ""]
    for raw_field in fields:
        field_norm = raw_field.replace("’", "'")
        if not field_norm.strip():
            continue
        for rx, reason in _UNAVAILABLE_RULES:
            if rx.search(field_norm):
                return reason
        if is_rent and _RENT_UNAVAILABLE_RE.search(field_norm):
            return "Bien déjà loué"
    return None


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
    matched = _match_known_city(city_name)
    if matched and CITY_METADATA[matched].get("bienici_zone_id"):
        return [CITY_METADATA[matched]["bienici_zone_id"]]

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


def _extract_bedrooms_from_text(text: str, rooms: int = 0) -> int:
    """Extrait le nombre de chambres mentionné dans la description ou l'estime à défaut."""
    if not text:
        return max(0, rooms - 1) if rooms >= 2 else 0
    m = re.search(r"\b(\d+)\s*chambres?\b", text, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if 0 < val <= 15:
            return val
    return 0


def scrape_bienici_live(criteria: Dict[str, Any], limit_per_city: Optional[int] = None, timeout: float = 12.0) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel l'agrégateur Bien'ici (qui regroupe IAD, Safti, Citya, Square Habitat,
    Guy Hoquet, ERA, Sergic et agences indépendantes de la métropole lilloise) ville par ville,
    avec pagination complète sans limite de 15 annonces.
    """
    results: List[Dict[str, Any]] = []
    seen_ids = set()
    tx_type = "rent" if str(criteria.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    min_price_floor = 250 if tx_type == "rent" else 70000
    max_budget = int(criteria.get("max_budget", 1500 if tx_type == "rent" else 280000))
    min_surface = float(criteria.get("min_surface", 80.0))
    min_rooms = int(criteria.get("min_rooms", 0) or 0)
    min_bedrooms = int(criteria.get("min_bedrooms", 0) or 0)
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    prop_types_map = []
    selected_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]
    if "maison" in selected_types:
        prop_types_map.append("house")
    if "appartement" in selected_types:
        prop_types_map.append("flat")
    if not prop_types_map:
        prop_types_map = ["house", "flat"]

    page_size = 100 if not limit_per_city else min(100, int(limit_per_city))
    max_pages = 15  # Jusqu'à 1500 annonces par ville

    headers = get_browser_headers(accept_json=True)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, verify=False) as client:
        for city in cities:
            zone_ids = resolve_bienici_zone_id(client, city)
            if not zone_ids:
                continue

            city_count = 0
            offset = 0
            for _ in range(max_pages):
                filters: Dict[str, Any] = {
                    "size": page_size,
                    "from": offset,
                    "showAllModels": False,
                    "filterType": tx_type,
                    "propertyType": prop_types_map,
                    "minPrice": min_price_floor,
                    "maxPrice": max_budget,
                    "minArea": min_surface,
                    "onTheMarket": [True],
                    "sortBy": "publicationDate",
                    "sortOrder": "desc",
                    "zoneIdsByTypes": {"zoneIds": zone_ids},
                }
                if min_rooms > 0:
                    filters["minRooms"] = min_rooms
                if min_bedrooms > 0:
                    filters["minBedrooms"] = min_bedrooms

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
                        break

                    ads_batch = data.get("realEstateAds") or []
                    if not ads_batch:
                        break

                    for ad in ads_batch:
                        ad_id = str(ad.get("id", "")).strip()
                        if not ad_id or ad_id in seen_ids:
                            continue

                        if ad.get("isSold") is True or ad.get("sold") is True:
                            continue
                        ad_status = ad.get("status")
                        if isinstance(ad_status, dict) and (
                            ad_status.get("onTheMarket") is False or ad_status.get("closedByUser") is True
                        ):
                            continue

                        prop_type = "Maison" if ad.get("propertyType") == "house" else "Appartement"
                        if prop_type.lower() not in selected_types:
                            continue

                        ad_city = _canonical_city_name(str(ad.get("city") or city).strip(), cities)
                        if not _city_matches_targets(ad_city, cities):
                            continue

                        city_meta = _get_city_meta(ad_city)
                        postal_code = str(ad.get("postalCode") or city_meta.get("postal_code", "59000"))
                        rooms = int(ad.get("roomsQuantity") or 0)
                        raw_desc = _clean_html_text(ad.get("description") or "")
                        bedrooms = int(ad.get("bedroomsQuantity") or 0)
                        if bedrooms == 0:
                            bedrooms = _extract_bedrooms_from_text(raw_desc, rooms)

                        surface = float(ad.get("surfaceArea") or 0.0)
                        price = int(ad.get("price") or 0)
                        if price < min_price_floor or price > max_budget or surface < min_surface:
                            continue
                        if min_rooms > 0 and rooms > 0 and rooms < min_rooms:
                            continue
                        if min_bedrooms > 0 and bedrooms > 0 and bedrooms < min_bedrooms:
                            continue

                        seen_ids.add(ad_id)

                        extra_meta = []
                        if bedrooms:
                            extra_meta.append(f"{bedrooms} chambres")
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

                        if detect_unavailable_reason(title, full_desc, tx_type=tx_type):
                            continue

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
                                "transaction_type": tx_type,
                                "property_type": prop_type,
                                "price": price,
                                "surface": surface,
                                "rooms": rooms,
                                "bedrooms": bedrooms,
                                "city": ad_city,
                                "postal_code": postal_code,
                                "description": full_desc or title,
                                "dpe": dpe,
                                "has_parking": has_parking,
                            }
                        )
                        city_count += 1
                        if limit_per_city and city_count >= limit_per_city:
                            break

                    if limit_per_city and city_count >= limit_per_city:
                        break
                    total_available = int(data.get("total") or 0)
                    offset += len(ads_batch)
                    if len(ads_batch) < page_size or (total_available > 0 and offset >= total_available):
                        break
                except Exception:
                    break

    return results


def _fetch_orpi_detail_enrichment(
    client: httpx.Client, ad_url: str
) -> Tuple[str, str, Optional[str], bool, bool]:
    """
    Récupère sur la fiche détail Orpi :
    - la description intégrale non tronquée (`div.s-cms`)
    - la lettre DPE réelle (`window.dataLayer` `dpe: "1".."7"` ou `li.c-dpe__index--active`)
    - le code postal exact (`codePostal` dans `window.dataLayer`)
    - l'indicateur de parking/garage issu du `dataLayer` (`nbGarage`, `nbParking`, `stationnement`)
    - l'indicateur d'indisponibilité (HTTP 404/410, redirection hors /annonce-, ou Sold=1)
    """
    full_desc = ""
    dpe = "Non renseigné"
    postal_code: Optional[str] = None
    has_parking_dl = False
    is_unavailable = False

    dpe_num_map = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E", "6": "F", "7": "G"}

    try:
        resp = robust_http_get(client, ad_url, max_retries=1)
        if not resp:
            return full_desc, dpe, postal_code, has_parking_dl, False
        if resp.status_code in (404, 410):
            return full_desc, dpe, postal_code, has_parking_dl, True
        if resp.status_code != 200:
            return full_desc, dpe, postal_code, has_parking_dl, False

        final_url = str(getattr(resp, "url", ad_url) or ad_url)
        if "/annonce-" not in final_url.lower():
            return full_desc, dpe, postal_code, has_parking_dl, True

        html = resp.text
        if re.search(r'\bSold\s*=\s*1\b|"sold"\s*:\s*true|\bisSold\s*:\s*true\b', html, re.IGNORECASE):
            is_unavailable = True

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

    return full_desc, dpe, postal_code, has_parking_dl, is_unavailable


def scrape_orpi_live(
    criteria: Dict[str, Any],
    timeout: float = 12.0,
    existing_orpi_cache: Optional[Dict[str, Tuple[str, str, str, Optional[int]]]] = None,
) -> List[Dict[str, Any]]:
    """
    Scrape en temps réel les annonces des agences Orpi de la métropole lilloise
    via l'attribut JSON `data-result` des pages de recherche Orpi et enrichit en parallèle
    chaque nouvelle annonce éligible avec sa fiche détail (en réutilisant le cache SQLite
    pour les annonces déjà connues).
    """
    results: List[Dict[str, Any]] = []
    seen_refs = set()
    orpi_cache = existing_orpi_cache or {}
    tx_type = "rent" if str(criteria.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    min_price_floor = 250 if tx_type == "rent" else 70000
    max_budget = int(criteria.get("max_budget", 1500 if tx_type == "rent" else 280000))
    min_surface = float(criteria.get("min_surface", 80.0))
    min_rooms = int(criteria.get("min_rooms", 0) or 0)
    min_bedrooms = int(criteria.get("min_bedrooms", 0) or 0)
    cities = criteria.get("cities", list(CITY_METADATA.keys()))
    selected_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]

    type_params = []
    if "maison" in selected_types:
        type_params.append("realEstateTypes[0]=maison")
    if "appartement" in selected_types:
        type_params.append(f"realEstateTypes[{len(type_params)}]=appartement")
    type_query = ("&" + "&".join(type_params)) if type_params else ""

    orpi_endpoint = "rent" if tx_type == "rent" else "buy"
    orpi_ad_prefix = "annonce-location-" if tx_type == "rent" else "annonce-vente-"

    urls_to_fetch: List[Tuple[str, str]] = []
    for city in cities:
        meta = _get_city_meta(city)
        orpi_slug = meta.get("orpi_slug") or _normalize_city(city).replace(" ", "-")
        urls_to_fetch.append(
            (
                city,
                f"https://www.orpi.com/recherche/{orpi_endpoint}?locations[0][value]={urllib.parse.quote(orpi_slug)}{type_query}&maxPrice={max_budget}&minSurface={int(min_surface)}",
            )
        )

    raw_candidates_to_enrich: List[Dict[str, Any]] = []
    headers = get_browser_headers(accept_json=False)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, verify=False) as client:
        for city, url in urls_to_fetch:
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

                    if item.get("sold") is True or item.get("isSold") is True or item.get("enabled") is False:
                        continue
                    item_short_ad = _clean_html_text(str(item.get("longAd") or ""))
                    item_tags = [str(t) for t in (item.get("tags") or [])]
                    if detect_unavailable_reason("", item_short_ad, tags=item_tags, tx_type=tx_type):
                        continue

                    raw_type = str(item.get("type") or "").lower()
                    prop_type = "Maison" if "maison" in raw_type else "Appartement"
                    if selected_types and prop_type.lower() not in selected_types:
                        continue

                    price = int(item.get("price") or 0)
                    surface = float(item.get("surface") or 0.0)
                    if price < min_price_floor or price > max_budget or surface < min_surface:
                        continue

                    raw_city = item.get("city")
                    if isinstance(raw_city, dict):
                        ad_city = str(raw_city.get("name") or city).strip()
                    else:
                        ad_city = str(raw_city or city).strip()
                    ad_city = _canonical_city_name(ad_city, cities)

                    if not _city_matches_targets(ad_city, cities):
                        continue

                    rooms = int(item.get("nbRooms") or 0)
                    if min_rooms > 0 and rooms > 0 and rooms < min_rooms:
                        continue

                    seen_refs.add(ref)
                    raw_candidates_to_enrich.append(
                        {
                            "item": item,
                            "slug": slug,
                            "ref": ref,
                            "prop_type": prop_type,
                            "price": price,
                            "surface": surface,
                            "rooms": rooms,
                            "ad_city": ad_city,
                        }
                    )
            except Exception:
                continue

        # Enrichissement parallèle des fiches détails Orpi (avec court-circuit sur le cache SQLite)
        def _enrich_candidate(cand: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            item = cand["item"]
            slug = cand["slug"]
            ref = cand["ref"]
            prop_type = cand["prop_type"]
            price = cand["price"]
            surface = cand["surface"]
            rooms = cand["rooms"]
            ad_city = cand["ad_city"]
            ext_id = f"orpi-{ref}"
            ad_url = f"https://www.orpi.com/{orpi_ad_prefix}{slug}/"

            raw_district = item.get("district")
            if isinstance(raw_district, dict):
                district_name = str(raw_district.get("name") or "").strip()
            else:
                district_name = str(raw_district or "").strip()

            short_ad = _clean_html_text(str(item.get("longAd") or ""))
            tags = [str(t) for t in (item.get("tags") or [])]
            if item.get("isLifeAnnuity"):
                tags.append("Viager")
            if item.get("isRentalInvestment"):
                tags.append("Investissement locatif / Vendu loué")

            cached = orpi_cache.get(ext_id)
            if cached and len(cached[0]) > len(short_ad) + 40 and cached[1] in ("A", "B", "C", "D", "E", "F", "G"):
                detail_desc, detail_dpe, detail_cp, has_parking_dl = (
                    cached[0],
                    cached[1],
                    cached[2] or None,
                    bool(cached[3]),
                )
                detail_unavailable = False
            else:
                detail_desc, detail_dpe, detail_cp, has_parking_dl, detail_unavailable = _fetch_orpi_detail_enrichment(
                    client, ad_url
                )

            if detail_unavailable:
                return None

            full_desc = detail_desc if len(detail_desc) > len(short_ad) else short_ad
            if tags and "[Tags Orpi :" not in full_desc:
                full_desc += f" [Tags Orpi : {', '.join(tags)}]"

            bedrooms = int(item.get("nbBedrooms") or 0)
            if bedrooms == 0:
                bedrooms = _extract_bedrooms_from_text(full_desc, rooms)
            if min_bedrooms > 0 and bedrooms > 0 and bedrooms < min_bedrooms:
                return None

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

            if detect_unavailable_reason(title, full_desc, tags=tags, tx_type=tx_type):
                return None

            has_parking_tags = any(any(w in t.lower() for w in PARKING_KEYWORDS) for t in tags)
            has_parking = _detect_parking_in_text(
                f"{title} {full_desc}",
                explicit_flag=(has_parking_dl or has_parking_tags),
            )

            agency_info = item.get("agency") or {}
            agency_name = agency_info.get("name") if isinstance(agency_info, dict) else "Orpi"
            ad_city_meta = _get_city_meta(ad_city)
            postal_code = detail_cp or ad_city_meta.get("postal_code", "59000")

            return {
                "external_id": ext_id,
                "source": f"Orpi ({agency_name})" if agency_name else "Orpi",
                "url": ad_url,
                "title": title,
                "transaction_type": tx_type,
                "property_type": prop_type,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": bedrooms,
                "city": ad_city,
                "postal_code": postal_code,
                "description": full_desc or f"Annonce Orpi {slug}",
                "dpe": dpe,
                "has_parking": has_parking,
            }

        if raw_candidates_to_enrich:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                for enriched in pool.map(_enrich_candidate, raw_candidates_to_enrich):
                    if enriched is not None:
                        results.append(enriched)

    return results


def parse_leboncoin_payload(
    raw_content: str,
    criteria: Dict[str, Any],
    fallback_city: str = "",
) -> List[Dict[str, Any]]:
    """
    Extrait et normalise les annonces immobilières LeBonCoin depuis :
    - une page HTML contenant `<script id="__NEXT_DATA__" type="application/json">`
    - ou un payload JSON brut de l'API LeBonCoin (`ads` / `props.pageProps.searchData.ads`).
    Fonctionne aussi bien pour une page de résultats de recherche que pour une fiche annonce unique.
    """
    results: List[Dict[str, Any]] = []
    if not raw_content or "geo.captcha-delivery.com" in raw_content:
        return results

    tx_type = "rent" if str(criteria.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    min_price_floor = 250 if tx_type == "rent" else 70000
    max_budget = int(criteria.get("max_budget", 1500 if tx_type == "rent" else 280000))
    min_surface = float(criteria.get("min_surface", 80.0))
    min_rooms = int(criteria.get("min_rooms", 0) or 0)
    min_bedrooms = int(criteria.get("min_bedrooms", 0) or 0)
    cities = criteria.get("cities", list(CITY_METADATA.keys()))
    selected_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]

    data = None
    stripped = raw_content.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None

    if data is None:
        soup = BeautifulSoup(raw_content, "html.parser")
        nd = soup.find("script", id="__NEXT_DATA__")
        if nd and nd.string:
            try:
                data = json.loads(nd.string)
            except Exception:
                data = None

    if not isinstance(data, dict):
        return results

    page_props = (data.get("props") or {}).get("pageProps") or {}
    search_data = page_props.get("searchData") or data.get("searchData") or {}
    ads_list = search_data.get("ads") or data.get("ads") or []
    if not ads_list and page_props.get("ad"):
        ads_list = [page_props["ad"]]

    for ad in ads_list:
        if not isinstance(ad, dict):
            continue
        list_id = str(ad.get("list_id") or ad.get("id") or "").strip()
        if not list_id:
            continue
        ad_status = str(ad.get("status") or "").strip().lower()
        if ad_status in ("deleted", "expired", "inactive", "sold", "refused", "paused", "closed"):
            continue

        # Prix
        raw_price = ad.get("price")
        if isinstance(raw_price, list) and raw_price:
            price = int(raw_price[0] or 0)
        elif isinstance(raw_price, (int, float)):
            price = int(raw_price)
        else:
            price = 0

        # Attributs LeBonCoin (square, rooms, bedrooms, real_estate_type, energy_rate, etc.)
        attrs_map: Dict[str, str] = {}
        attrs_label_map: Dict[str, str] = {}
        for attr in (ad.get("attributes") or []):
            if isinstance(attr, dict) and attr.get("key"):
                k = str(attr["key"])
                val = str(attr.get("value") or "").strip()
                lbl = str(attr.get("value_label") or val).strip()
                attrs_map[k] = val
                attrs_label_map[k] = lbl

        re_type_val = attrs_map.get("real_estate_type", "")
        re_type_lbl = attrs_label_map.get("real_estate_type", "").lower()
        if re_type_val == "1" or "maison" in re_type_lbl:
            prop_type = "Maison"
        elif re_type_val == "2" or "appartement" in re_type_lbl:
            prop_type = "Appartement"
        else:
            subject_lower = str(ad.get("subject") or "").lower()
            prop_type = "Maison" if "maison" in subject_lower else "Appartement"

        if selected_types and prop_type.lower() not in selected_types:
            continue

        try:
            surface = float(re.sub(r"[^\d.]", "", attrs_map.get("square", "0")) or 0.0)
        except Exception:
            surface = 0.0

        try:
            rooms = int(re.sub(r"\D", "", attrs_map.get("rooms", "0")) or 0)
        except Exception:
            rooms = 0

        try:
            bedrooms = int(re.sub(r"\D", "", attrs_map.get("bedrooms", "0")) or 0)
        except Exception:
            bedrooms = 0

        if price < min_price_floor or price > max_budget or (surface > 0 and surface < min_surface):
            continue

        loc = ad.get("location") or {}
        raw_city = str(loc.get("city") or fallback_city or "").strip()
        ad_city = _canonical_city_name(raw_city, cities)
        if cities and ad_city and not _city_matches_targets(ad_city, cities):
            continue

        postal_code = str(loc.get("zipcode") or _get_city_meta(ad_city).get("postal_code", "59000"))
        district = str(loc.get("district") or "").strip()

        title = _clean_html_text(str(ad.get("subject") or "")).strip()
        body = _clean_html_text(str(ad.get("body") or "")).strip()

        if bedrooms == 0:
            bedrooms = _extract_bedrooms_from_text(f"{title} {body}", rooms)

        if min_rooms > 0 and rooms > 0 and rooms < min_rooms:
            continue
        if min_bedrooms > 0 and bedrooms > 0 and bedrooms < min_bedrooms:
            continue

        dpe = str(attrs_map.get("energy_rate") or "").strip().upper()
        if dpe not in ("A", "B", "C", "D", "E", "F", "G"):
            m_dpe = re.search(r"\bdpe\s*[:\-]?\s*([a-g])\b", f"{title} {body}", re.IGNORECASE)
            dpe = m_dpe.group(1).upper() if m_dpe else "Non renseigné"

        extra_lbc = []
        if bedrooms:
            extra_lbc.append(f"{bedrooms} chambres")
        if attrs_label_map.get("outside_access"):
            extra_lbc.append(f"Extérieur : {attrs_label_map['outside_access']}")
        if attrs_label_map.get("ges"):
            extra_lbc.append(f"GES : {attrs_label_map['ges']}")
        if attrs_label_map.get("immo_sell_type"):
            extra_lbc.append(f"Type de vente : {attrs_label_map['immo_sell_type']}")

        full_desc = body or title
        if extra_lbc:
            full_desc += f" [Attributs LeBonCoin : {', '.join(extra_lbc)}]"

        if detect_unavailable_reason(title, full_desc, tx_type=tx_type):
            continue

        has_parking_attr = any(
            k in attrs_label_map.get("outside_access", "").lower()
            or k in attrs_label_map.get("custom_ref", "").lower()
            for k in PARKING_KEYWORDS
        )
        has_parking = _detect_parking_in_text(f"{title} {full_desc}", explicit_flag=has_parking_attr)

        owner = ad.get("owner") or {}
        owner_type = str(owner.get("type") or "").lower()
        owner_name = str(owner.get("name") or "").strip()
        if owner_type == "private":
            source_label = f"LeBonCoin (Particulier{' - ' + owner_name if owner_name else ''})"
        else:
            source_label = f"LeBonCoin (Pro{' - ' + owner_name if owner_name else ''})"

        ad_url = str(ad.get("url") or f"https://www.leboncoin.fr/ad/ventes_immobilieres/{list_id}")
        if district and district.lower() not in title.lower():
            title = f"{title} ({district})"

        results.append(
            {
                "external_id": f"lbc-{list_id}",
                "source": source_label,
                "url": ad_url,
                "title": title or f"{prop_type} {rooms} pièces {surface:g} m² - {ad_city}",
                "transaction_type": tx_type,
                "property_type": prop_type,
                "price": price,
                "surface": surface,
                "rooms": rooms,
                "bedrooms": bedrooms,
                "city": ad_city,
                "postal_code": postal_code,
                "description": full_desc,
                "dpe": dpe,
                "has_parking": has_parking,
            }
        )

    return results


def check_single_listing_availability(client: httpx.Client, listing: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """
    Vérifie si une annonce est toujours disponible :
    1. Contrôle textuel instantané (titre + description).
    2. Vérification API directe pour Bien'ici (`realEstateAd.json?id=...`) et fiche HTML pour Orpi.
    Retourne `(True, "")` si confirmée disponible, `(False, raison)` si confirmée indisponible,
    ou `(None, "")` si la vérification est impossible ou non concluante (erreur transitoire).
    """
    tx_type = "rent" if str(listing.get("transaction_type") or "buy").strip().lower() in ("rent", "location") else "buy"
    title = str(listing.get("title") or "")
    desc = str(listing.get("description") or "")

    text_reason = detect_unavailable_reason(title, desc, tx_type=tx_type)
    if text_reason:
        return False, text_reason

    ext_id = str(listing.get("external_id") or "").strip()
    if ext_id.startswith("bienici-"):
        ad_id = ext_id[len("bienici-") :]
        if not ad_id:
            return None, ""
        url = f"https://www.bienici.com/realEstateAd.json?id={urllib.parse.quote(ad_id)}"
        try:
            resp = robust_http_get(client, url, max_retries=1)
            if not resp:
                return None, ""
            if resp.status_code in (404, 410):
                return False, f"Annonce supprimée de Bien'ici (HTTP {resp.status_code})"
            if resp.status_code != 200:
                return None, ""
            data = resp.json()
            if not isinstance(data, dict):
                return None, ""
            st = data.get("status")
            if isinstance(st, dict):
                if st.get("onTheMarket") is False:
                    return False, "Retirée du marché (Bien'ici)"
                if st.get("closedByUser") is True:
                    return False, "Clôturée par l'annonceur (Bien'ici)"
            if data.get("isSold") is True or data.get("sold") is True:
                return False, "Bien vendu (Bien'ici)"
            live_title = str(data.get("title") or title)
            live_desc = _clean_html_text(str(data.get("description") or ""))
            live_reason = detect_unavailable_reason(live_title, live_desc, tx_type=tx_type)
            if live_reason:
                return False, live_reason
            return True, ""
        except Exception:
            return None, ""

    if ext_id.startswith("orpi-"):
        ad_url = str(listing.get("url") or "").strip()
        if not ad_url:
            return None, ""
        try:
            resp = robust_http_get(client, ad_url, max_retries=1)
            if not resp:
                return None, ""
            if resp.status_code in (404, 410):
                return False, f"Annonce retirée d'Orpi (HTTP {resp.status_code})"
            if resp.status_code != 200:
                return None, ""
            final_url = str(getattr(resp, "url", ad_url) or ad_url)
            if "/annonce-" not in final_url.lower():
                return False, "Annonce redirigée / retirée d'Orpi"
            html = resp.text or ""
            if re.search(r'\bSold\s*=\s*1\b|"sold"\s*:\s*true|\bisSold\s*:\s*true\b', html, re.IGNORECASE):
                return False, "Bien vendu (Orpi)"
            soup = BeautifulSoup(html, "html.parser")
            cms_div = soup.find("div", class_=re.compile(r"\bs-cms\b"))
            live_desc = _clean_html_text(str(cms_div)) if cms_div else ""
            live_reason = detect_unavailable_reason(title, live_desc, tx_type=tx_type)
            if live_reason:
                return False, live_reason
            return True, ""
        except Exception:
            return None, ""

    return None, ""


def check_listings_availability_batch(
    listings: List[Dict[str, Any]],
    max_workers: int = 8,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[Tuple[int, bool, str]]:
    """
    Vérifie en parallèle la disponibilité réelle d'une liste d'annonces en base
    (contrôle textuel + API Bien'ici + fiches Orpi).
    Retourne uniquement les statuts confirmés `(listing_id, is_available, reason)`.
    """
    results: List[Tuple[int, bool, str]] = []
    total = len(listings)
    if total == 0:
        return results

    workers = max(1, min(max_workers, 8))
    headers = get_browser_headers(accept_json=False)
    with httpx.Client(headers=headers, timeout=10.0, follow_redirects=True, verify=False) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_item = {
                executor.submit(check_single_listing_availability, client, item): item
                for item in listings
            }
            done = 0
            for future in concurrent.futures.as_completed(future_to_item):
                item = future_to_item[future]
                done += 1
                try:
                    is_avail, reason = future.result()
                    if is_avail is not None and item.get("id") is not None:
                        results.append((int(item["id"]), bool(is_avail), str(reason or "")))
                except Exception:
                    pass
                if on_progress:
                    try:
                        on_progress(done, total)
                    except Exception:
                        pass
    return results


def scrape_all_live_sources(
    criteria: Dict[str, Any],
    existing_orpi_cache: Optional[Dict[str, Tuple[str, str, str, Optional[int]]]] = None,
) -> Dict[str, Any]:
    """
    Exécute le scraping live multi-sources (Bien'ici + Réseaux d'Agences + Orpi)
    EN PARALLÈLE sur 2 threads avec sessions HTTP/2 Keep-Alive et cache d'enrichissement Orpi.
    """
    bienici_ads: List[Dict[str, Any]] = []
    orpi_ads: List[Dict[str, Any]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_bienici = executor.submit(scrape_bienici_live, criteria, None)
        f_orpi = executor.submit(scrape_orpi_live, criteria, 12.0, existing_orpi_cache)

        try:
            bienici_ads = f_bienici.result()
        except Exception:
            bienici_ads = []

        try:
            orpi_ads = f_orpi.result()
        except Exception:
            orpi_ads = []

    combined = bienici_ads + orpi_ads
    return {
        "ads": combined,
        "stats": {
            "bienici_agences": len(bienici_ads),
            "orpi_direct": len(orpi_ads),
            "total": len(combined),
        },
    }


def build_portal_search_links(criteria: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """
    Génère les liens directs vers les recherches pré-filtrées (Transaction, Budget, Surface, Pièces, Chambres, Ville)
    sur Bien'ici, Orpi, LeBonCoin et SeLoger pour chaque ville sélectionnée.
    """
    tx_type = "rent" if str(criteria.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    max_budget = int(criteria.get("max_budget", 1500 if tx_type == "rent" else 280000))
    min_surface = int(criteria.get("min_surface", 80))
    min_rooms = int(criteria.get("min_rooms", 0) or 0)
    min_bedrooms = int(criteria.get("min_bedrooms", 0) or 0)
    cities = criteria.get("cities", list(CITY_METADATA.keys()))

    bienici_tx = "location" if tx_type == "rent" else "achat"
    orpi_tx = "rent" if tx_type == "rent" else "buy"
    lbc_cat = "10" if tx_type == "rent" else "9"
    seloger_dist = "Rent" if tx_type == "rent" else "Buy"

    links_by_city: Dict[str, List[Dict[str, str]]] = {}
    for city in cities:
        meta = _get_city_meta(city)
        cp = meta["postal_code"]
        slug = meta["slug"]
        orpi_slug = meta.get("orpi_slug", slug)

        bienici_url = (
            f"https://www.bienici.com/recherche/{bienici_tx}/{slug}"
            f"?prix-max={max_budget}&surface-min={min_surface}&parking=true"
        )
        if min_rooms > 0:
            bienici_url += f"&nb-pieces-min={min_rooms}"
        if min_bedrooms > 0:
            bienici_url += f"&nb-chambres-min={min_bedrooms}"

        orpi_url = (
            f"https://www.orpi.com/recherche/{orpi_tx}?locations[0][value]={orpi_slug}"
            f"&maxPrice={max_budget}&minSurface={min_surface}"
        )

        leboncoin_query = urllib.parse.quote(f"{city} {cp}")
        leboncoin_url = (
            f"https://www.leboncoin.fr/recherche?category={lbc_cat}&locations={leboncoin_query}"
            f"&price=min-{max_budget}&square={min_surface}-max&real_estate_type=1,2"
        )
        if min_rooms > 0:
            leboncoin_url += f"&rooms={min_rooms}-max"
        if min_bedrooms > 0:
            leboncoin_url += f"&bedrooms={min_bedrooms}-max"

        seloger_place = meta.get("seloger_place_id", "")
        if seloger_place:
            seloger_url = (
                f"https://www.seloger.com/classified-search?distributionTypes={seloger_dist}"
                f"&estateTypes=House,Apartment&locations={seloger_place}"
                f"&priceMax={max_budget}&spaceMin={min_surface}"
            )
            if min_rooms > 0:
                seloger_url += f"&nbOfRoomsMin={min_rooms}"
            if min_bedrooms > 0:
                seloger_url += f"&nbOfBedroomsMin={min_bedrooms}"
        else:
            seloger_url = f"https://www.seloger.com/immobilier/{bienici_tx}/immo-{slug}/"

        links_by_city[city] = [
            {"portail": "Bien'ici (Agrégateur)", "url": bienici_url},
            {"portail": "Orpi Agences", "url": orpi_url},
            {"portail": "LeBonCoin", "url": leboncoin_url},
            {"portail": "SeLoger", "url": seloger_url},
        ]
    return links_by_city
