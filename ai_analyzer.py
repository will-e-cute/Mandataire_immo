import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


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
    return template.format(
        max_budget=criteria.get("max_budget", 280000),
        min_surface=criteria.get("min_surface", 80.0),
        property_types=", ".join(criteria.get("property_types", ["Maison", "Appartement"])),
        cities=", ".join(criteria.get("cities", [])),
        require_parking="Oui (OBLIGATOIRE : Garage, Box ou Parking privatif)"
        if criteria.get("require_parking", True)
        else "Non obligatoire",
        exclude_dpe_fg="Oui" if criteria.get("exclude_dpe_fg", True) else "Non",
        exclude_occupied="Oui" if criteria.get("exclude_occupied", True) else "Non",
        exclude_heavy_works="Oui" if criteria.get("exclude_heavy_works", True) else "Non",
        exclude_copro_issues="Oui" if criteria.get("exclude_copro_issues", True) else "Non",
        custom_notes=criteria.get("custom_notes") or "Aucune consigne supplémentaire.",
    )


def _normalize_city(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[-'’œ]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.replace("oe", "o")


def heuristic_analyze_listing(listing: Dict[str, Any], criteria: Dict[str, Any]) -> Dict[str, Any]:
    """
    Moteur d'analyse déterministe intelligent (utilisé en pré-filtrage ou si aucune clé API Gemini n'est fournie,
    permettant à l'application de fonctionner immédiatement hors-ligne).
    """
    price = int(listing.get("price", 0))
    surface = float(listing.get("surface", 0.0))
    prix_m2 = round(price / surface, 2) if surface > 0 else 0.0
    title = listing.get("title", "")
    desc = listing.get("description", "")
    full_text = f"{title} {desc}".lower()

    # Détection DPE
    dpe = listing.get("dpe") or "Non renseigné"
    if dpe == "Non renseigné":
        m = re.search(r"\bdpe\s*[:\-]?\s*([a-g])\b", full_text, re.IGNORECASE)
        if m:
            dpe = m.group(1).upper()

    # Détection Parking / Garage
    has_parking_col = listing.get("has_parking")
    parking_keywords = ["garage", "box", "parking priv", "place de parking", "stationnement priv", "carport", "2 places"]
    no_parking_keywords = ["sans garage", "pas de garage", "stationnement dans la rue", "parking facile dans la rue", "sans parking"]

    if any(k in full_text for k in no_parking_keywords):
        parking_detecte = False
    elif has_parking_col is not None:
        parking_detecte = bool(has_parking_col)
    else:
        parking_detecte = any(k in full_text for k in parking_keywords)

    reasons: List[str] = []
    pros: List[str] = []
    cons: List[str] = []
    signals: List[str] = []

    # 1. Vérification Budget
    max_budget = int(criteria.get("max_budget", 280000))
    if price > max_budget:
        reasons.append(f"Hors budget ({price:,} € > {max_budget:,} €)".replace(",", " "))

    # 2. Vérification Surface
    min_surface = float(criteria.get("min_surface", 80.0))
    if surface < min_surface:
        reasons.append(f"Surface insuffisante ({surface:g} m² < {min_surface:g} m²)")

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

    # 7. Vérification Occupation (vendu loué / viager)
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
        if prix_m2 < 2850:
            base_score += 1
            pros.append(f"Prix/m² très attractif ({prix_m2:.0f} €/m²)")
        if cons:
            base_score -= 1
        if dpe == "Non renseigné":
            statut = "a_verifier"
            score = min(6, base_score)
            cons.append("DPE non renseigné dans l'annonce")
            resume = f"Bien dans les critères à {listing.get('city')} ({prix_m2:.0f} €/m²), mais DPE ou détails techniques à confirmer."
        else:
            statut = "a_visiter" if base_score >= 7 else "a_verifier"
            score = max(4, min(10, base_score))
            resume = (
                f"Opportunité solide pour une résidence principale à {listing.get('city')} "
                f"({surface:g} m² à {price:,} €, soit {prix_m2:.0f} €/m²) avec stationnement inclus.".replace(",", " ")
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


def test_gemini_api_key(api_key: Optional[str] = None, model_name: str = "gemini-3.8-flash") -> Dict[str, Any]:
    """
    Teste instantanément (< 300 ms) la validité de la clé API Gemini auprès des serveurs Google
    sans retry bloquant ni erreur de certificat SSL local Windows.
    """
    effective_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
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
                params={"key": effective_key, "pageSize": 5},
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
            "message": f"Erreur réseau lors du test de la clé ({type(exc).__name__}) : {exc}",
        }


def analyze_listing(
    listing: Dict[str, Any],
    criteria: Dict[str, Any],
    api_key: Optional[str] = None,
    model_name: str = "gemini-3.8-flash",
) -> Dict[str, Any]:
    """
    Analyse une annonce immobilière via l'API Gemini (google-genai SDK >= 2.30.1 avec Interactions API
    et schéma JSON strict) ou bascule proprement sur le moteur d'analyse déterministe si la clé API
    n'est pas renseignée.
    """
    effective_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()

    # Si aucune clé API Gemini n'est disponible, utiliser le moteur expert local sans erreur
    if not effective_key:
        return heuristic_analyze_listing(listing, criteria)

    system_instruction = build_system_prompt(criteria)
    price = int(listing.get("price", 0))
    surface = float(listing.get("surface", 0.0))
    prix_m2 = round(price / surface, 2) if surface > 0 else 0.0

    user_prompt = (
        f"Analyse cette annonce immobilière selon les critères stricts du client :\n"
        f"- Titre : {listing.get('title')}\n"
        f"- Type : {listing.get('property_type')}\n"
        f"- Ville : {listing.get('city')} ({listing.get('postal_code')})\n"
        f"- Prix : {price} €\n"
        f"- Surface : {surface} m² (Ratio calculé : {prix_m2} €/m²)\n"
        f"- Pièces : {listing.get('rooms')}\n"
        f"- DPE affiché : {listing.get('dpe')}\n"
        f"- Parking affiché : {listing.get('has_parking')}\n"
        f"- Description complète :\n{listing.get('description')}\n"
    )

    try:
        from google import genai
        from google.genai import types

        http_opts = types.HttpOptions(httpx_client=httpx.Client(verify=False, timeout=20.0))
        client = genai.Client(api_key=effective_key, http_options=http_opts)
        interaction = client.interactions.create(
            model=model_name,
            system_instruction=system_instruction,
            input=user_prompt,
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": ListingAnalysisSchema.model_json_schema(),
            },
        )
        parsed = ListingAnalysisSchema.model_validate_json(interaction.output_text)
        result = parsed.model_dump()
        result["moteur"] = f"Gemini ({model_name})"
        return result
    except Exception as exc:
        # En cas d'erreur réseau/quota, on sécurise avec le moteur local + note
        fallback = heuristic_analyze_listing(listing, criteria)
        fallback["moteur"] = f"Fallback Local (Erreur API: {type(exc).__name__})"
        return fallback
