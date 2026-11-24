import concurrent.futures
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from scraper import detect_unavailable_reason

load_dotenv()

PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"


class ListingAnalysisSchema(BaseModel):
    statut: Literal["a_visiter", "a_verifier", "rejete"] = Field(
        description="Décision finale : 'a_visiter' (7-10), 'a_verifier' (4-6), ou 'rejete' (0-3)."
    )
    score: int = Field(
        ge=0,
        le=10,
        description="Note globale de 0 à 10 évaluant l'adéquation avec la recherche de résidence principale.",
    )
    prix_m2: float = Field(description="Ratio Prix / Surface en €/m² arrondi.")
    dpe_detecte: str = Field(
        description="Lettre DPE détectée (A, B, C, D, E, F, G ou 'Non renseigné')."
    )
    parking_detecte: bool = Field(
        description="True si un garage, box ou parking privatif est inclus, False sinon."
    )
    raison_rejet: Optional[str] = Field(
        default=None,
        description="Si statut='rejete', indiquer la cause principale courte (ex: 'Hors budget', 'Pas de garage/parking', 'Passoire DPE F', 'Vendu loué'). Sinon null.",
    )
    resume: str = Field(
        description="Synthèse pragmatique et froide de l'annonce en 1 à 2 phrases maximum."
    )
    points_forts: List[str] = Field(
        default_factory=list,
        description="Liste courte des atouts concrets (ex: 'Garage 1 voiture', 'Jardin Sud-Ouest', 'Proche Tramway').",
    )
    points_vigilance: List[str] = Field(
        default_factory=list,
        description="Liste courte des points à vérifier ou défauts (ex: 'Cuisine à rafraîchir', 'DPE D proche E').",
    )
    signaux_faibles: List[str] = Field(
        default_factory=list,
        description="Termes ou indices implicites relevés dans le texte (ex: 'idéal investisseur', 'fort potentiel', 'secteur recherché').",
    )


def build_system_prompt(criteria: Dict[str, Any]) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    tx_type = "rent" if str(criteria.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    tx_label = "Location (Loyer mensuel charges comprises)" if tx_type == "rent" else "Achat (Prix FAI)"
    min_rooms = int(criteria.get("min_rooms", 0) or 0)
    min_bedrooms = int(criteria.get("min_bedrooms", 0) or 0)
    return template.format(
        transaction_label=tx_label,
        max_budget=criteria.get("max_budget", 1500 if tx_type == "rent" else 280000),
        min_surface=criteria.get("min_surface", 80.0),
        min_rooms=f"{min_rooms} pièce(s) min." if min_rooms > 0 else "Indifférent",
        min_bedrooms=f"{min_bedrooms} chambre(s) min." if min_bedrooms > 0 else "Indifférent",
        property_types=", ".join(criteria.get("property_types", ["Maison", "Appartement"])),
        cities=", ".join(criteria.get("cities", [])),
        require_parking="Oui (OBLIGATOIRE : Garage, Box ou Parking privatif)"
        if criteria.get("require_parking", True)
        else "Non obligatoire",
        exclude_dpe_fg="Oui" if criteria.get("exclude_dpe_fg", True) else "Non",
        exclude_occupied="Oui" if (criteria.get("exclude_occupied", True) and tx_type == "buy") else "Non",
        exclude_heavy_works="Oui" if criteria.get("exclude_heavy_works", True) else "Non",
        exclude_copro_issues="Oui" if criteria.get("exclude_copro_issues", True) else "Non",
        custom_notes=criteria.get("custom_notes") or "Aucune consigne supplémentaire.",
    )


def _normalize_city(text: str) -> str:
    if not text:
        return ""
    text = str(text).lower().strip()
    text = text.replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[-'’_./]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def heuristic_analyze_listing(listing: Dict[str, Any], criteria: Dict[str, Any]) -> Dict[str, Any]:
    """
    Moteur d'analyse déterministe intelligent (utilisé en pré-filtrage ou si aucune clé API Gemini n'est fournie,
    permettant à l'application de fonctionner immédiatement hors-ligne).
    """
    tx_type = "rent" if str(criteria.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    price = int(listing.get("price", 0))
    surface = float(listing.get("surface", 0.0))
    prix_m2 = round(price / surface, 2) if surface > 0 else 0.0
    rooms = int(listing.get("rooms", 0) or 0)
    bedrooms = int(listing.get("bedrooms", 0) or 0)
    title = listing.get("title", "")
    desc = listing.get("description", "")
    full_text = f"{title} {desc}".lower()

    if bedrooms == 0:
        m_bed = re.search(r"\b(\d+)\s*chambres?\b", full_text, re.IGNORECASE)
        if m_bed:
            bedrooms = int(m_bed.group(1))

    # Détection DPE
    dpe = listing.get("dpe") or "Non renseigné"
    if dpe == "Non renseigné":
        m = re.search(r"\bdpe\s*[:\-]?\s*([a-g])\b", full_text, re.IGNORECASE)
        if m:
            dpe = m.group(1).upper()

    # Détection Parking / Garage (sans faux négatif si "garage + stationnement facile dans la rue")
    has_parking_col = listing.get("has_parking")
    strong_parking_keywords = ["garage", "box", "parking priv", "place de parking", "stationnement priv", "carport", "2 places"]
    strict_no_parking = ["sans garage", "pas de garage", "aucun garage", "sans parking", "pas de parking"]
    street_parking = ["stationnement dans la rue", "parking facile dans la rue", "stationnement facile dans la rue"]

    if any(k in full_text for k in strict_no_parking):
        parking_detecte = False
    elif has_parking_col is True or any(k in full_text for k in strong_parking_keywords):
        parking_detecte = True
    elif any(k in full_text for k in street_parking):
        parking_detecte = False
    elif has_parking_col is not None:
        parking_detecte = bool(has_parking_col)
    else:
        parking_detecte = "parking" in full_text or "stationnement" in full_text

    reasons: List[str] = []
    pros: List[str] = []
    cons: List[str] = []
    signals: List[str] = []

    # 0a. Vérification disponibilité réelle de l'annonce
    raw_avail = listing.get("is_available")
    if raw_avail is False or raw_avail == 0:
        unavail_msg = str(listing.get("unavailable_reason") or "Annonce retirée ou vendue").strip()
        reasons.append(f"Bien plus disponible ({unavail_msg})")
        signals.append(unavail_msg)
    else:
        unavail_reason = detect_unavailable_reason(title, desc, tx_type=tx_type)
        if unavail_reason:
            reasons.append(f"Bien plus disponible ({unavail_reason})")
            signals.append(unavail_reason)

    # 0. Vérification cohérence type de transaction (Achat vs Location)
    ad_tx = str(listing.get("transaction_type") or tx_type).strip().lower()
    if ad_tx in ("buy", "rent") and ad_tx != tx_type:
        reasons.append(f"Type de transaction différent ({'Location' if ad_tx == 'rent' else 'Achat'})")

    # 1. Vérification Budget
    max_budget = int(criteria.get("max_budget", 1500 if tx_type == "rent" else 280000))
    if price > max_budget:
        reasons.append(f"Hors budget ({price:,} € > {max_budget:,} €)".replace(",", " "))

    # 2. Vérification Surface
    min_surface = float(criteria.get("min_surface", 80.0))
    if surface < min_surface:
        reasons.append(f"Surface insuffisante ({surface:g} m² < {min_surface:g} m²)")

    # 2b. Vérification Nombre de pièces et de chambres
    min_rooms = int(criteria.get("min_rooms", 0) or 0)
    if min_rooms > 0:
        if rooms > 0 and rooms < min_rooms:
            reasons.append(f"Nombre de pièces insuffisant ({rooms} p. < {min_rooms} p.)")
        elif rooms >= min_rooms:
            pros.append(f"{rooms} pièces")

    min_bedrooms = int(criteria.get("min_bedrooms", 0) or 0)
    if min_bedrooms > 0:
        if bedrooms > 0 and bedrooms < min_bedrooms:
            reasons.append(f"Nombre de chambres insuffisant ({bedrooms} ch. < {min_bedrooms} ch.)")
        elif bedrooms == 0:
            cons.append(f"Nombre de chambres non précisé ({min_bedrooms} ch. min. souhaitées)")
        else:
            pros.append(f"{bedrooms} chambres")
    elif bedrooms > 0:
        pros.append(f"{bedrooms} chambres")

    # 3. Vérification Type de bien
    allowed_types = [t.lower() for t in criteria.get("property_types", ["Maison", "Appartement"])]
    prop_type = (listing.get("property_type") or "Autre").lower()
    if allowed_types and prop_type not in allowed_types:
        reasons.append(f"Type de bien exclu ({listing.get('property_type')})")

    # 4. Vérification Ville
    allowed_cities = [_normalize_city(c) for c in criteria.get("cities", [])]
    listing_city_norm = _normalize_city(listing.get("city", ""))
    if allowed_cities and not any(c in listing_city_norm or listing_city_norm in c for c in allowed_cities):
        reasons.append(f"Hors secteur ({listing.get('city')})")

    # 5. Vérification Parking / Garage obligatoire
    if criteria.get("require_parking", True) and not parking_detecte:
        reasons.append("Absence de parking privatif / garage obligatoire")
    elif parking_detecte:
        pros.append("Stationnement inclus (Garage / Box / Parking privatif)")

    # 6. Vérification Passoire thermique DPE F/G
    if criteria.get("exclude_dpe_fg", True) and dpe in ("F", "G"):
        reasons.append(f"Passoire thermique (DPE {dpe})")
    elif dpe in ("A", "B", "C"):
        pros.append(f"Excellente performance énergétique (DPE {dpe})")
    elif dpe == "D":
        pros.append("DPE D correct")
    elif dpe == "E":
        cons.append("DPE E (isolation à surveiller)")

    # 7. Vérification Occupation (vendu loué / viager — applicable en Achat)
    if tx_type == "buy":
        occupied_terms = ["vendu loué", "vendue louée", "locataire en place", "viager", "nue-propriété", "bien occupé"]
        if any(t in full_text for t in occupied_terms):
            signals.append("Bien vendu occupé / investisseur")
            if criteria.get("exclude_occupied", True):
                reasons.append("Bien vendu loué / occupé (indisponible pour RP)")

    # 8. Vérification Gros œuvre / travaux lourds
    heavy_works_terms = [
        "toiture à refaire",
        "gros oeuvre",
        "gros œuvre",
        "rénovation totale",
        "rénovation complète",
        "tout à refaire",
        "humidité",
        "fissures structurelles",
        "mérule",
        "plateau brut",
    ]
    if any(t in full_text for t in heavy_works_terms):
        signals.append("Travaux lourds détectés")
        if criteria.get("exclude_heavy_works", True):
            reasons.append("Gros œuvre ou rénovation lourde à prévoir")

    # 9. Vérification Copropriété en difficulté
    negated_copro = ["aucune procédure", "pas de procédure", "sans procédure"]
    copro_terms = ["procédure en cours", "syndic provisoire", "copropriété en difficulté", "ravalement voté à charge"]
    has_copro_issue = any(t in full_text for t in copro_terms) and not any(n in full_text for n in negated_copro)
    if has_copro_issue:
        signals.append("Alerte copropriété")
        if criteria.get("exclude_copro_issues", True):
            reasons.append("Copropriété avec procédure ou charges lourdes")

    # Bonus Confort / Extérieur / Quartier
    if any(k in full_text for k in ["jardin", "terrasse", "extérieur"]):
        pros.append("Espace extérieur (Jardin / Terrasse)")
    if any(k in full_text for k in ["sud", "sud-ouest", "lumineux", "traversant"]):
        pros.append("Belle exposition / Luminosité")
    if any(k in full_text for k in ["tramway", "métro", "barbieux", "grand boulevard", "calme", "impasse"]):
        pros.append("Emplacement recherché / Proche transports")
    if any(k in full_text for k in ["aucun travaux", "rénové", "refait à neuf", "clé en main"]):
        pros.append("Aucun travaux à prévoir (Clé en main)")
    if any(k in full_text for k in ["rafraîchissement", "rafraichissement", "cuisine à moderniser", "déco à prévoir"]):
        cons.append("Rafraîchissement esthétique à prévoir")
        signals.append("Marge de négociation potentielle (rafraîchissement)")

    if reasons:
        statut = "rejete"
        score = max(0, 3 - len(reasons))
        raison_rejet = " | ".join(reasons)
        resume = f"Annonce écartée automatiquement : {raison_rejet}."
    else:
        base_score = 7
        if len(pros) >= 3:
            base_score += 1
        if len(pros) >= 4:
            base_score += 1
        attractive_threshold = 13.5 if tx_type == "rent" else 2850
        prix_m2_fmt = f"{prix_m2:.1f} €/m²/mois" if tx_type == "rent" else f"{prix_m2:.0f} €/m²"
        if prix_m2 > 0 and prix_m2 < attractive_threshold:
            base_score += 1
            if tx_type == "rent":
                pros.append(f"Loyer/m² très attractif ({prix_m2_fmt})")
            else:
                pros.append(f"Prix/m² très attractif ({prix_m2_fmt})")
        if cons:
            base_score -= 1
        if dpe == "Non renseigné":
            statut = "a_verifier"
            score = min(6, base_score)
            cons.append("DPE non renseigné dans l'annonce")
            resume = f"Bien dans les critères à {listing.get('city')} ({prix_m2_fmt}), mais DPE ou détails techniques à confirmer."
        else:
            statut = "a_visiter" if base_score >= 7 else "a_verifier"
            score = max(4, min(10, base_score))
            tx_word = "location" if tx_type == "rent" else "résidence principale"
            price_unit = "€/mois" if tx_type == "rent" else "€"
            resume = (
                f"Opportunité solide pour une {tx_word} à {listing.get('city')} "
                f"({surface:g} m² à {price:,} {price_unit}, soit {prix_m2_fmt}).".replace(",", " ")
            )
        raison_rejet = None

    return {
        "statut": statut,
        "score": score,
        "prix_m2": prix_m2,
        "dpe_detecte": dpe,
        "parking_detecte": parking_detecte,
        "raison_rejet": raison_rejet,
        "resume": resume,
        "points_forts": pros[:5],
        "points_vigilance": cons[:4],
        "signaux_faibles": signals[:4],
        "moteur": "Regles_Metier_Locales",
    }


def test_gemini_api_key(api_key: Optional[str] = None, model_name: str = DEFAULT_GEMINI_MODEL) -> Dict[str, Any]:
    """
    Teste instantanément (< 300 ms) la validité de la clé API Gemini auprès des serveurs Google
    sans retry bloquant ni erreur de certificat SSL local Windows.
    """
    effective_key = (os.getenv("GEMINI_API_KEY") if api_key is None else api_key).strip()
    if not effective_key:
        return {
            "ok": False,
            "message": "Aucune clé API Gemini n'est renseignée (saisissez votre clé AIzaSy... ci-dessus).",
        }

    t0 = time.perf_counter()
    try:
        with httpx.Client(verify=False, timeout=5.0) as client:
            resp = client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": effective_key},
                params={"pageSize": 5},
            )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if resp.status_code == 200:
            return {
                "ok": True,
                "model": model_name,
                "message": f"Clé API valide et opérationnelle (vérifiée en {elapsed_ms} ms) — Prête pour {model_name}.",
            }
        else:
            err_msg = "Clé API refusée par Google."
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", err_msg)
            except Exception:
                pass
            return {
                "ok": False,
                "model": model_name,
                "message": f"Clé invalide (HTTP {resp.status_code} en {elapsed_ms} ms) : {err_msg}",
            }
    except Exception as exc:
        return {
            "ok": False,
            "model": model_name,
            "message": f"Erreur réseau lors du test de la clé ({type(exc).__name__})",
        }


def _extract_interaction_output_text(data: Dict[str, Any]) -> str:
    """Extrait le texte final de la réponse REST Interactions API (schéma `steps` ou repli)."""
    direct_text = data.get("output_text")
    if isinstance(direct_text, str) and direct_text:
        return direct_text

    steps = data.get("steps") or []
    if isinstance(steps, list) and steps:
        trailing_texts: List[str] = []
        for step in reversed(steps):
            if not isinstance(step, dict):
                continue
            if step.get("type") == "model_output":
                step_parts: List[str] = []
                for part in step.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        step_parts.append(part["text"])
                if step_parts:
                    trailing_texts.insert(0, "".join(step_parts))
            elif trailing_texts:
                break
        if trailing_texts:
            return "".join(trailing_texts)

    outputs = data.get("outputs") or []
    if isinstance(outputs, list) and outputs and isinstance(outputs[-1], dict):
        legacy_text = outputs[-1].get("text")
        if isinstance(legacy_text, str) and legacy_text:
            return legacy_text

    return "{}"


def call_gemini_interactions_api(
    user_prompt: str,
    system_instruction: str,
    api_key: str,
    model_name: str = DEFAULT_GEMINI_MODEL,
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """
    Appelle directement l'API REST Interactions v1beta de Google Gemini avec sortie JSON structurée Pydantic
    et passage sécurisé de la clé API dans l'en-tête HTTP `x-goog-api-key`.
    """
    url = "https://generativelanguage.googleapis.com/v1beta/interactions"
    headers = {
        "x-goog-api-key": api_key,
        "Api-Revision": "2026-05-20",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name,
        "system_instruction": system_instruction,
        "input": user_prompt,
        "generation_config": {"thinking_level": "low"},
        "response_format": {
            "type": "text",
            "mime_type": "application/json",
            "schema": ListingAnalysisSchema.model_json_schema(),
        },
    }
    with httpx.Client(verify=False, timeout=timeout) as client:
        resp = client.post(url, headers=headers, json=payload)
        if resp.status_code == 200:
            data = resp.json()
            output_text = _extract_interaction_output_text(data)
            parsed = ListingAnalysisSchema.model_validate_json(output_text)
            res = parsed.model_dump()
            res["moteur"] = f"Gemini ({model_name})"
            return res
        elif resp.status_code == 429:
            raise RuntimeError("RESOURCE_EXHAUSTED: Quota Gemini dépassé (429)")
        else:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")


def analyze_listing(
    listing: Dict[str, Any],
    criteria: Dict[str, Any],
    api_key: Optional[str] = None,
    model_name: str = DEFAULT_GEMINI_MODEL,
) -> Dict[str, Any]:
    """
    Pipeline Hybride Haute Performance :
    1. Exécute d'abord l'évaluation déterministe locale (< 0.1 ms).
    2. Si l'annonce est rejetée par un critère strict (budget, surface, pièces, chambres, hors-secteur,
       DPE F/G, absence de stationnement, etc.) ou si aucune clé API n'est fournie, retourne immédiatement
       le résultat déterministe sans gaspiller de quota Gemini.
    3. Sinon, appelle Gemini pour l'analyse qualitative fine des annonces éligibles.
    """
    local_eval = heuristic_analyze_listing(listing, criteria)
    effective_key = (os.getenv("GEMINI_API_KEY") if api_key is None else api_key).strip()

    if not effective_key or local_eval.get("statut") == "rejete":
        return local_eval

    system_instruction = build_system_prompt(criteria)
    price = int(listing.get("price", 0))
    surface = float(listing.get("surface", 0.0))
    prix_m2 = round(price / surface, 2) if surface > 0 else 0.0

    user_prompt = (
        f"Analyse cette annonce immobilière selon les critères stricts du client :\n"
        f"<annonce_brute>\n"
        f"- Titre : {listing.get('title')}\n"
        f"- Transaction : {'Location' if listing.get('transaction_type') == 'rent' else 'Achat'}\n"
        f"- Type : {listing.get('property_type')}\n"
        f"- Ville : {listing.get('city')} ({listing.get('postal_code')})\n"
        f"- Prix : {price} €\n"
        f"- Surface : {surface} m² (Ratio calculé : {prix_m2} €/m²)\n"
        f"- Pièces : {listing.get('rooms', 0)} | Chambres : {listing.get('bedrooms', 0)}\n"
        f"- DPE affiché : {listing.get('dpe')}\n"
        f"- Parking affiché : {listing.get('has_parking')}\n"
        f"- Description complète :\n{listing.get('description')}\n"
        f"</annonce_brute>\n"
    )

    try:
        return call_gemini_interactions_api(
            user_prompt=user_prompt,
            system_instruction=system_instruction,
            api_key=effective_key,
            model_name=model_name,
        )
    except Exception as exc:
        err_str = str(exc)
        if "RESOURCE_EXHAUSTED" in err_str or "429" in err_str:
            reason = "Quota Gemini dépassé (429)"
        else:
            reason = f"Erreur API ({type(exc).__name__})"

        local_eval["moteur"] = f"Fallback Local ({reason})"
        return local_eval


def analyze_listings_parallel(
    listings: List[Dict[str, Any]],
    criteria: Dict[str, Any],
    api_key: Optional[str] = None,
    model_name: str = DEFAULT_GEMINI_MODEL,
    max_workers: int = 4,
    on_completed: Optional[Callable[[Dict[str, Any], Dict[str, Any], int, int], None]] = None,
    check_cancelled: Optional[Callable[[], bool]] = None,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    Exécute l'analyse d'un lot d'annonces en parallèle à l'aide d'un ThreadPoolExecutor.
    Pour chaque annonce terminée, invoque on_completed(listing, analysis, completed_count, total).
    Interrompt proprement l'exécution si check_cancelled() retourne True.
    Court-circuite les appels suivants en mode local dès détection de l'épuisement du quota Gemini.
    """
    results: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    total = len(listings)
    if total == 0:
        return results

    workers = max(1, min(max_workers, 8))
    quota_exhausted = [False]

    def _worker_task(item: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        if quota_exhausted[0]:
            analysis = heuristic_analyze_listing(item, criteria)
            analysis["moteur"] = "Fallback Local (Quota Gemini dépassé)"
            return item, analysis

        analysis = analyze_listing(item, criteria, api_key=api_key, model_name=model_name)
        if "Quota Gemini dépassé" in analysis.get("moteur", ""):
            quota_exhausted[0] = True
        return item, analysis

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_item = {
            executor.submit(_worker_task, item): item
            for item in listings
        }
        completed_count = 0
        for future in concurrent.futures.as_completed(future_to_item):
            if check_cancelled and check_cancelled():
                executor.shutdown(wait=False, cancel_futures=True)
                break

            orig_item = future_to_item[future]
            try:
                item, analysis = future.result()
            except Exception as exc:
                item = orig_item
                analysis = heuristic_analyze_listing(item, criteria)
                analysis["moteur"] = f"Fallback Local (Exception thread: {exc})"

            results.append((item, analysis))
            completed_count += 1
            if on_completed:
                try:
                    on_completed(item, analysis, completed_count, total)
                except Exception:
                    pass

    return results
