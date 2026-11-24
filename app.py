import hashlib
import io
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
import pandas as pd
import streamlit as st
from dotenv import load_dotenv, set_key

from database import (
    delete_all_listings,
    delete_listing_permanently,
    empty_quarantine,
    export_db_to_json,
    get_all_listings,
    get_criteria,
    get_existing_orpi_descriptions,
    get_pending_listings,
    get_quarantined_listings,
    get_selected_listings,
    get_unavailable_listings,
    import_db_from_json,
    init_db,
    mark_listings_availability_batch,
    quarantine_unavailable_listings,
    reset_all_listings_to_pending,
    restore_all_quarantine,
    save_criteria,
    set_listing_quarantine,
    set_listing_selection,
    update_listing_analysis,
    update_listing_followup,
    update_listings_analysis_batch,
    upsert_listing,
    upsert_listings_batch,
)
from ai_analyzer import analyze_listing, analyze_listings_parallel, test_gemini_api_key
from scraper import (
    CITY_METADATA,
    build_portal_search_links,
    check_listings_availability_batch,
    detect_unavailable_reason,
    parse_leboncoin_payload,
    scrape_all_live_sources,
)

ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(ENV_FILE)

st.set_page_config(
    page_title="Mandataire Immobilier Virtuel — Cockpit IA",
    page_icon="🏡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialisation de la base SQLite locale et migration automatique du schéma
init_db()
current_criteria = get_criteria()


def _get_default_gemini_key() -> str:
    """Récupère la clé Gemini depuis st.secrets (Streamlit Cloud) ou depuis les variables d'environnement (.env)."""
    try:
        if "GEMINI_API_KEY" in st.secrets:
            return str(st.secrets["GEMINI_API_KEY"]).strip()
    except Exception:
        pass
    return os.getenv("GEMINI_API_KEY", "").strip()


if "gemini_api_key" not in st.session_state:
    st.session_state["gemini_api_key"] = _get_default_gemini_key()
if "analysis_running" not in st.session_state:
    st.session_state["analysis_running"] = False
if "analysis_paused" not in st.session_state:
    st.session_state["analysis_paused"] = False


def _safe_md_text(text: str) -> str:
    """Neutralise les séquences d'images/liens Markdown externes dans les titres/sources issus du web."""
    if not text:
        return ""
    return re.sub(r"[!\[\]]", "", str(text)).strip()


def extract_phone_number(text: str) -> str:
    """Extrait le premier numéro de téléphone français valide trouvé dans une description."""
    if not text:
        return ""
    pattern = r"(?:(?:\+|00)33\s?[1-9]|0[1-9])(?:[\s.-]?\d{2}){4}"
    match = re.search(pattern, text)
    if match:
        clean = match.group(0).strip()
        digits = re.sub(r"\D", "", clean)
        if len(digits) == 10 and digits.startswith("0"):
            return f"{digits[0:2]} {digits[2:4]} {digits[4:6]} {digits[6:8]} {digits[8:10]}"
        return clean
    return ""


def extract_default_agency_name(source: str) -> str:
    """Extrait un nom d'agence lisible par défaut à partir du libellé source."""
    if not source:
        return ""
    if "/" in source:
        parts = source.split("/", 1)
        cleaned = re.sub(r"\(via.*?\)", "", parts[1]).strip()
        return cleaned or source
    cleaned = re.sub(r"\(via.*?\)", "", source).strip()
    if "Agrégateur" in cleaned or "Agregateur" in cleaned or "Manuel" in cleaned:
        return ""
    return cleaned


def generate_export_files(listings_to_export: List[Dict[str, Any]]) -> Tuple[bytes, bytes]:
    """Génère les exports binaires CSV (séparateur point-virgule Excel FR) et Excel (.xlsx)."""
    rows = []
    for l in listings_to_export:
        status_fr = {
            "a_visiter": "🟢 À visiter (Opportunité)",
            "a_verifier": "🟡 À vérifier (À confirmer)",
            "pending_analysis": "⏳ En attente",
            "rejete": "🔴 Rejeté (Critères No-Go)",
        }.get(l.get("status"), l.get("status"))

        pros = l.get("ai_pros") or []
        cons = l.get("ai_cons") or []
        signals = l.get("ai_signals") or []

        rows.append(
            {
                "Sélectionné": "⭐ Oui" if l.get("is_selected") else "Non",
                "Contact Agence": "✅ Oui" if l.get("contact_made") else "❌ Non",
                "Visite Programmée": "🗓️ Oui" if l.get("visit_scheduled") else "❌ Non",
                "Nom Agence": l.get("agency_name") or "",
                "Téléphone Agence": l.get("agency_phone") or "",
                "Notes Suivi": l.get("followup_notes") or "",
                "Statut IA": status_fr,
                "Disponibilité": "✅ Disponible"
                if l.get("is_available", True)
                else f"🚫 Indisponible ({l.get('unavailable_reason') or 'Retirée / vendue'})",
                "Score IA (/10)": l.get("ai_score"),
                "Transaction": "Location" if l.get("transaction_type") == "rent" else "Achat",
                "Titre": l.get("title"),
                "Ville": l.get("city"),
                "Code Postal": l.get("postal_code"),
                "Prix (€)": l.get("price"),
                "Surface (m²)": l.get("surface"),
                "Prix/m² (€/m²)": l.get("price_per_m2"),
                "Pièces": l.get("rooms"),
                "Chambres": l.get("bedrooms", 0),
                "DPE": l.get("dpe"),
                "Parking / Garage": "Oui" if l.get("has_parking") else "Non",
                "Source": l.get("source"),
                "URL Annonce": l.get("url"),
                "Synthèse IA": l.get("ai_summary") or "",
                "Motif de rejet": l.get("ai_rejection_reason") or "",
                "Points forts": " | ".join(pros) if isinstance(pros, list) else str(pros),
                "Points de vigilance": " | ".join(cons) if isinstance(cons, list) else str(cons),
                "Signaux faibles": " | ".join(signals) if isinstance(signals, list) else str(signals),
                "Date Scraping": l.get("scraped_at"),
                "Date Analyse": l.get("analyzed_at"),
            }
        )

    df = pd.DataFrame(rows)
    # CSV formaté pour Excel France (point-virgule et encodage UTF-8 avec BOM)
    csv_bytes = df.to_csv(index=False, sep=";", encoding="utf-8-sig").encode("utf-8-sig")

    # Excel natif (.xlsx)
    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Opportunités Immo")
    excel_bytes = excel_buffer.getvalue()

    return csv_bytes, excel_bytes


TABLE_COLUMNS = [
    "Sélectionné",
    "Statut IA",
    "Disponibilité",
    "Score IA",
    "Transaction",
    "Type",
    "Ville",
    "Code Postal",
    "Titre",
    "Prix (€)",
    "Surface (m²)",
    "Prix/m² (€)",
    "Pièces",
    "Chambres",
    "DPE",
    "Parking",
    "Source",
    "URL",
    "Synthèse IA",
    "Motif de rejet",
    "Points forts",
    "Points de vigilance",
    "Signaux faibles",
    "Contact Agence",
    "Visite Programmée",
    "Nom Agence",
    "Téléphone Agence",
    "Date Scraping",
    "Date Analyse",
]

DEFAULT_TABLE_COLUMNS = [
    "Sélectionné",
    "Statut IA",
    "Score IA",
    "Type",
    "Ville",
    "Titre",
    "Prix (€)",
    "Surface (m²)",
    "Prix/m² (€)",
    "Pièces",
    "Chambres",
    "DPE",
    "Parking",
    "Source",
    "URL",
]


def _format_list_field(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        return " | ".join(str(x) for x in value)
    return str(value)


def build_table_dataframe(listings: List[Dict[str, Any]]) -> pd.DataFrame:
    """Construit un DataFrame typé pour l'affichage tableau interactif (tri numérique/booléen natif)."""
    if not listings:
        empty_df = pd.DataFrame(columns=TABLE_COLUMNS)
        empty_df["Sélectionné"] = empty_df["Sélectionné"].astype(bool)
        empty_df["Score IA"] = pd.array([], dtype="Int64")
        empty_df["Prix (€)"] = pd.Series(dtype="int64")
        empty_df["Surface (m²)"] = pd.Series(dtype="float64")
        empty_df["Prix/m² (€)"] = pd.Series(dtype="float64")
        empty_df["Pièces"] = pd.Series(dtype="int64")
        empty_df["Chambres"] = pd.Series(dtype="int64")
        empty_df["Parking"] = empty_df["Parking"].astype(bool)
        empty_df["Contact Agence"] = empty_df["Contact Agence"].astype(bool)
        empty_df["Visite Programmée"] = empty_df["Visite Programmée"].astype(bool)
        return empty_df

    status_labels = {
        "a_visiter": "🟢 À visiter",
        "a_verifier": "🟡 À vérifier",
        "pending_analysis": "⏳ En attente",
        "rejete": "🔴 Rejeté",
    }
    rows = []
    scores = []
    for l in listings:
        price = int(l.get("price") or 0)
        surface = float(l.get("surface") or 0.0)
        raw_pm2 = l.get("price_per_m2")
        price_per_m2 = float(raw_pm2) if raw_pm2 is not None else (round(price / surface, 2) if surface > 0 else 0.0)
        raw_score = l.get("ai_score")
        scores.append(int(raw_score) if raw_score is not None else pd.NA)

        rows.append(
            {
                "Sélectionné": bool(l.get("is_selected")),
                "Statut IA": status_labels.get(l.get("status"), str(l.get("status") or "")),
                "Disponibilité": "✅ Disponible"
                if l.get("is_available", True)
                else f"🚫 Indisponible ({l.get('unavailable_reason') or 'Retirée / vendue'})",
                "Score IA": 0,
                "Transaction": "Location" if l.get("transaction_type") == "rent" else "Achat",
                "Type": str(l.get("property_type") or ""),
                "Ville": str(l.get("city") or ""),
                "Code Postal": str(l.get("postal_code") or ""),
                "Titre": str(l.get("title") or ""),
                "Prix (€)": price,
                "Surface (m²)": surface,
                "Prix/m² (€)": price_per_m2,
                "Pièces": int(l.get("rooms") or 0),
                "Chambres": int(l.get("bedrooms") or 0),
                "DPE": str(l.get("dpe") or "Non renseigné"),
                "Parking": bool(l.get("has_parking")),
                "Source": str(l.get("source") or ""),
                "URL": str(l.get("url") or ""),
                "Synthèse IA": str(l.get("ai_summary") or ""),
                "Motif de rejet": str(l.get("ai_rejection_reason") or ""),
                "Points forts": _format_list_field(l.get("ai_pros")),
                "Points de vigilance": _format_list_field(l.get("ai_cons")),
                "Signaux faibles": _format_list_field(l.get("ai_signals")),
                "Contact Agence": bool(l.get("contact_made")),
                "Visite Programmée": bool(l.get("visit_scheduled")),
                "Nom Agence": str(l.get("agency_name") or ""),
                "Téléphone Agence": str(l.get("agency_phone") or ""),
                "Date Scraping": str(l.get("scraped_at") or ""),
                "Date Analyse": str(l.get("analyzed_at") or ""),
            }
        )

    df = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    df["Score IA"] = pd.array(scores, dtype="Int64")
    return df


# --- BARRE LATÉRALE : CLÉ API GEMINI, PARALLÉLISME & CRITÈRES DE RECHERCHE ---
with st.sidebar:
    st.title("🤖 Moteur IA (Gemini)")
    api_key_input = st.text_input(
        "Clé API Gemini",
        value=st.session_state["gemini_api_key"],
        type="password",
        placeholder="AIzaSy...",
        help="Votre clé API est utilisée avec gemini-4-hydrogen (via st.secrets en Cloud ou .env en local).",
    )
    st.session_state["gemini_api_key"] = api_key_input.strip()

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        test_api_btn = st.button("🔌 Tester la clé API", use_container_width=True)
    with btn_col2:
        save_api_btn = st.button("💾 Sauver (.env)", use_container_width=True)

    if save_api_btn:
        if st.session_state["gemini_api_key"]:
            set_key(str(ENV_FILE), "GEMINI_API_KEY", st.session_state["gemini_api_key"])
            os.environ["GEMINI_API_KEY"] = st.session_state["gemini_api_key"]
            st.success("Clé API enregistrée dans `.env` !")
        else:
            st.warning("Veuillez saisir une clé API avant de sauvegarder.")

    if test_api_btn:
        with st.spinner("Vérification de la clé auprès de l'API Gemini..."):
            test_res = test_gemini_api_key(st.session_state["gemini_api_key"])
        if test_res["ok"]:
            st.success(f"✅ {test_res['message']}")
        else:
            st.error(f"❌ {test_res['message']}")

    max_workers = st.slider(
        "⚡ Vitesse de tri (threads parallèles)",
        min_value=1,
        max_value=8,
        value=4,
        help="Nombre d'analyses menées simultanément pour accélérer le tri sans saturer l'API.",
    )

    st.divider()
    st.title("⚙️ Critères de Recherche")
    st.caption("Modifiez vos variables ci-dessous puis cliquez sur **Lancer la recherche**.")

    with st.form("criteria_form"):
        st.subheader("1. Transaction, Budget & Surface")
        current_tx = str(current_criteria.get("transaction_type", "buy")).strip().lower()
        tx_choice_label = st.radio(
            "Type de transaction",
            options=["Achat", "Location"],
            index=1 if current_tx == "rent" else 0,
            horizontal=True,
        )
        selected_tx_type = "rent" if tx_choice_label == "Location" else "buy"

        saved_budget = int(current_criteria.get("max_budget", 280000))
        max_budget = st.number_input(
            "Budget maximum (€ FAI en Achat / €/mois CC en Location)",
            min_value=250,
            max_value=3000000,
            value=max(250, saved_budget),
            step=100 if selected_tx_type == "rent" else 5000,
            help="En Achat : ex. 280 000 €. En Location : loyer mensuel max (ex. 1 200 €).",
        )
        min_surface = st.number_input(
            "Surface minimale (m²)",
            min_value=15.0,
            max_value=500.0,
            value=float(current_criteria.get("min_surface", 80.0)),
            step=5.0,
        )

        rc_col1, rc_col2 = st.columns(2)
        with rc_col1:
            min_rooms = st.number_input(
                "Pièces min.",
                min_value=0,
                max_value=15,
                value=int(current_criteria.get("min_rooms", 0)),
                step=1,
                help="0 = sans filtre sur le nombre de pièces.",
            )
        with rc_col2:
            min_bedrooms = st.number_input(
                "Chambres min.",
                min_value=0,
                max_value=10,
                value=int(current_criteria.get("min_bedrooms", 0)),
                step=1,
                help="0 = sans filtre sur le nombre de chambres.",
            )

        st.subheader("2. Type de bien & Secteur")
        default_types = current_criteria.get("property_types", ["Maison", "Appartement"])
        property_types = st.multiselect(
            "Types de biens recherchés",
            options=["Maison", "Appartement"],
            default=[t for t in default_types if t in ["Maison", "Appartement"]] or ["Maison", "Appartement"],
        )

        cities_text = st.text_area(
            "Villes ciblées (séparées par une virgule)",
            value=", ".join(
                current_criteria.get(
                    "cities",
                    ["Croix", "Wasquehal", "Roubaix", "Villeneuve-d'Ascq", "Marcq-en-Barœul"],
                )
            ),
            help="Par défaut : Croix, Wasquehal, Roubaix, Villeneuve-d'Ascq, Marcq-en-Barœul",
        )

        st.subheader("3. Exigences & Filtres No-Go")
        require_parking = st.checkbox(
            "🚗 Parking, Box ou Garage OBLIGATOIRE",
            value=bool(current_criteria.get("require_parking", True)),
        )
        exclude_dpe_fg = st.checkbox(
            "🔥 Exclure passoires thermiques (DPE F et G)",
            value=bool(current_criteria.get("exclude_dpe_fg", True)),
        )
        exclude_occupied = st.checkbox(
            "🔑 Exclure biens vendus loués / occupés / viager",
            value=bool(current_criteria.get("exclude_occupied", True)),
        )
        exclude_heavy_works = st.checkbox(
            "🧱 Exclure gros œuvre à prévoir (toiture, structure, humidité)",
            value=bool(current_criteria.get("exclude_heavy_works", True)),
        )
        exclude_copro_issues = st.checkbox(
            "🏢 Exclure copropriétés en difficulté / procédure",
            value=bool(current_criteria.get("exclude_copro_issues", True)),
        )

        custom_notes = st.text_input(
            "Consignes spécifiques pour l'IA (optionnel)",
            value=current_criteria.get("custom_notes", ""),
            placeholder="Ex: Proche tramway, jardin orienté Sud...",
        )

        reanalyze_existing = st.checkbox(
            "🔄 Ré-évaluer aussi toutes les annonces déjà en base avec ces critères",
            value=False,
            help="Cochez uniquement si vous venez de modifier vos critères et souhaitez recalculer le tri sur tout l'historique.",
        )

        launch_search = st.form_submit_button(
            "🚀 Lancer la recherche (Live Scraping)",
            type="primary",
            use_container_width=True,
        )

    pending_now = get_pending_listings()
    ctrl_col1, ctrl_col2 = st.columns(2)
    with ctrl_col1:
        stop_analysis_btn = st.button(
            "⏸️ Stopper l'IA",
            use_container_width=True,
            help="Interrompt immédiatement l'analyse IA en cours en conservant toutes les annonces et résultats déjà enregistrés dans mandataire.db.",
        )
    with ctrl_col2:
        resume_analysis_btn = st.button(
            f"▶️ Reprendre ({len(pending_now)})",
            use_container_width=True,
            disabled=(len(pending_now) == 0),
            help="Reprend l'analyse IA uniquement sur les annonces restantes en attente.",
        )

    if stop_analysis_btn:
        st.session_state["analysis_running"] = False
        st.session_state["analysis_paused"] = True
        st.toast("⏸️ Analyse IA interrompue ! La base de données et les annonces sont intactes.", icon="⏸️")

    if resume_analysis_btn:
        st.session_state["analysis_paused"] = False
        st.session_state["analysis_running"] = True

    active_for_check = get_all_listings(include_quarantined=False, include_unavailable=False)
    check_avail_btn = st.button(
        f"🔍 Vérifier la disponibilité ({len(active_for_check)})",
        use_container_width=True,
        disabled=(len(active_for_check) == 0),
        help="Interroge en direct Bien'ici et Orpi et détecte les mentions 'vendu / sous compromis' pour masquer les annonces retirées du marché.",
    )
    if check_avail_btn:
        avail_prog = st.progress(0, text=f"🔍 Vérification de la disponibilité (0 / {len(active_for_check)})...")
        avail_step = max(1, min(15, len(active_for_check) // 20))

        def _on_avail_prog(done: int, total: int) -> None:
            if done % avail_step == 0 or done == total:
                avail_prog.progress(done / max(1, total), text=f"🔍 Vérification en direct : {done} / {total}...")

        avail_updates = check_listings_availability_batch(
            active_for_check,
            max_workers=8,
            on_progress=_on_avail_prog,
        )
        marked_unavail, confirmed_avail = mark_listings_availability_batch(avail_updates)
        st.session_state["availability_report"] = {
            "checked": len(active_for_check),
            "confirmed_available": confirmed_avail,
            "marked_unavailable": marked_unavail,
        }
        st.rerun()

    if st.button("🗑️ Vider la base d'annonces", use_container_width=True):
        st.session_state["analysis_running"] = False
        delete_all_listings()
        st.toast("Base de données vidée.", icon="🗑️")
        st.rerun()

    st.divider()
    with st.expander("☁️ Sauvegarde / Restauration Cloud (JSON)"):
        st.caption(
            "Sur Streamlit Cloud, le conteneur peut se réinitialiser lors des mises en veille. "
            "Téléchargez ou restaurez vos annonces, sélections et suivis CRM en 1 clic :"
        )
        st.download_button(
            label="📥 Exporter la BDD complète (.json)",
            data=export_db_to_json(),
            file_name=f"mandataire_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            mime="application/json",
            use_container_width=True,
        )
        uploaded_backup = st.file_uploader("📤 Importer une sauvegarde (.json)", type=["json"])
        if uploaded_backup is not None:
            if st.button("Restaurer ce fichier", use_container_width=True):
                restored_count = import_db_from_json(uploaded_backup.getvalue().decode("utf-8"))
                st.success(f"✅ {restored_count} annonce(s) et critères restaurés avec succès !")
                st.rerun()


# --- ACTION 1 : LANCEMENT DU SCRAPING LIVE AVEC RÉSILIENCE WAF ---
if launch_search:
    st.session_state["analysis_paused"] = False
    parsed_cities = [c.strip() for c in cities_text.split(",") if c.strip()]
    save_criteria(
        transaction_type=selected_tx_type,
        max_budget=max_budget,
        min_surface=min_surface,
        min_rooms=min_rooms,
        min_bedrooms=min_bedrooms,
        property_types=property_types or ["Maison", "Appartement"],
        cities=parsed_cities,
        require_parking=require_parking,
        exclude_dpe_fg=exclude_dpe_fg,
        exclude_occupied=exclude_occupied,
        exclude_heavy_works=exclude_heavy_works,
        exclude_copro_issues=exclude_copro_issues,
        custom_notes=custom_notes,
    )
    current_criteria = get_criteria()

    with st.status("🔍 Collecte Live Parallèle (SeLoger + Bien'ici + Orpi + LeBonCoin)...", expanded=True) as status_box:
        st.write(
            f"1️⃣ Interrogation simultanée sur 4 threads de **SeLoger.com**, **Bien'ici**, "
            f"**Orpi** et **LeBonCoin** sur : **{', '.join(parsed_cities)}**..."
        )
        orpi_cache = get_existing_orpi_descriptions()
        scrape_report = scrape_all_live_sources(current_criteria, existing_orpi_cache=orpi_cache)
        all_fetched = scrape_report["ads"]
        stats = scrape_report["stats"]

        if stats.get("leboncoin_blocked"):
            st.warning(
                "⚠️ **LeBonCoin a présenté un portail anti-bot (DataDome 403).** "
                "Pour importer les annonces LeBonCoin (Particuliers & Pros) en 1 clic, ouvrez le lien de recherche LeBonCoin "
                "dans votre navigateur (onglet *🌐 Liens Directs Portails*), faites `Ctrl+U` (Code source) et collez-le dans "
                "l'onglet *➕ Tester / Importer une Annonce* (ou renseignez `LEBONCOIN_COOKIE` dans `.env`)."
            )

        new_count, updated_count = upsert_listings_batch(all_fetched)

        # Vérification automatique de disponibilité sur les annonces déjà en base absentes de ce scraping
        fresh_ext_ids = {ad["external_id"] for ad in all_fetched}
        existing_active = get_all_listings(include_quarantined=False, include_unavailable=False)
        stale_candidates = [x for x in existing_active if x.get("external_id") not in fresh_ext_ids]
        priority_rank = {"a_visiter": 1, "a_verifier": 2, "pending_analysis": 3, "rejete": 4}
        stale_candidates.sort(
            key=lambda x: (0 if x.get("is_selected") else priority_rank.get(x.get("status"), 5))
        )
        stale_to_check = stale_candidates[:200]
        auto_unavail_count = 0
        if stale_to_check:
            st.write(
                f"2️⃣ Vérification de disponibilité en direct sur **{len(stale_to_check)}** annonce(s) "
                "déjà en base non remontées par cette collecte..."
            )
            avail_updates = check_listings_availability_batch(stale_to_check, max_workers=8)
            auto_unavail_count, _ = mark_listings_availability_batch(avail_updates)

        if reanalyze_existing:
            reset_all_listings_to_pending()

        pending_after_scrape = get_pending_listings()
        unavail_suffix = f" | 🚫 {auto_unavail_count} indisponible(s) masquée(s)" if auto_unavail_count > 0 else ""
        status_box.update(
            label=(
                f"📥 Collecte terminée : {stats['total']} annonces récupérées "
                f"({stats.get('seloger', 0)} SeLoger, {stats['bienici_agences']} Bien'ici, "
                f"{stats['orpi_direct']} Orpi, {stats.get('leboncoin', 0)} LeBonCoin) — "
                f"{new_count} nouvelles + {updated_count} mises à jour en base{unavail_suffix}. "
                f"Démarrage du tri parallèle sur {len(pending_after_scrape)} annonces..."
            ),
            state="complete",
            expanded=False,
        )
    st.session_state["analysis_running"] = True


# --- ACTION 2 : EXÉCUTION DE L'ANALYSE IA EN PARALLÈLE (AVEC PRÉ-FILTRAGE HYBRIDE & BATCH SQLITE) ---
if st.session_state.get("analysis_running") and not st.session_state.get("analysis_paused"):
    pending = get_pending_listings()
    if pending:
        active_key = st.session_state["gemini_api_key"]
        engine_label = "Pipeline Hybride (Pré-filtre + Gemini 4 Hydrogen)" if active_key else "Moteur de règles local (sans clé API)"
        total_to_process = len(pending)

        info_col, stop_inline_col = st.columns([4, 1])
        with info_col:
            progress_bar = st.progress(
                0,
                text=f"🧠 Analyse IA parallèle ({max_workers} threads | {engine_label}) : 0 / {total_to_process} annonces...",
            )
        with stop_inline_col:
            if st.button("⏹️ Interrompre l'analyse IA", type="secondary", use_container_width=True):
                st.session_state["analysis_running"] = False
                st.session_state["analysis_paused"] = True
                st.rerun()

        completed_counter = [0]
        pending_db_updates: List[Tuple[int, Dict[str, Any]]] = []
        ui_refresh_step = max(1, min(25, total_to_process // 30))

        def on_item_completed(item: Dict[str, Any], analysis: Dict[str, Any], count: int, total: int):
            pending_db_updates.append((item["id"], analysis))
            completed_counter[0] = count
            if len(pending_db_updates) >= 25 or count == total:
                update_listings_analysis_batch(pending_db_updates)
                pending_db_updates.clear()
            if count % ui_refresh_step == 0 or count == total:
                progress_bar.progress(
                    count / max(1, total),
                    text=(
                        f"🧠 Analyse IA parallèle ({max_workers} threads) : {count} / {total} — "
                        f"{item.get('city', '')} ({item.get('price', 0):,} €)".replace(",", " ")
                    ),
                )

        def check_cancelled() -> bool:
            return bool(st.session_state.get("analysis_paused"))

        analyze_listings_parallel(
            listings=pending,
            criteria=current_criteria,
            api_key=active_key,
            model_name="gemini-4-hydrogen",
            max_workers=max_workers,
            on_completed=on_item_completed,
            check_cancelled=check_cancelled,
        )

        if pending_db_updates:
            update_listings_analysis_batch(pending_db_updates)
            pending_db_updates.clear()

        st.session_state["analysis_running"] = False
        st.success(f"✅ Tri IA terminé avec succès ({completed_counter[0]} annonces analysées et enregistrées) !")
        st.rerun()
    else:
        st.session_state["analysis_running"] = False

if st.session_state.get("analysis_paused"):
    remaining = len(get_pending_listings())
    st.warning(
        f"⏸️ **Analyse IA interrompue à votre demande.** Vos annonces et analyses déjà effectuées sont sauvegardées dans `mandataire.db`. "
        f"Il reste **{remaining}** annonce(s) en statut *En attente* (cliquez sur **▶️ Reprendre ({remaining})** dans la barre latérale quand vous le souhaitez)."
    )

avail_report = st.session_state.pop("availability_report", None)
if avail_report:
    if avail_report["marked_unavailable"] > 0:
        st.info(
            f"🔍 **Vérification de disponibilité terminée :** "
            f"**{avail_report['marked_unavailable']}** annonce(s) détectée(s) comme vendue(s) ou retirée(s) du marché et masquée(s) par défaut "
            f"(**{avail_report['confirmed_available']}** confirmée(s) en ligne)."
        )
    else:
        st.success(
            f"✅ **Vérification de disponibilité terminée :** aucune annonce indisponible détectée "
            f"(**{avail_report['confirmed_available']}** confirmée(s) en ligne)."
        )

manual_unavail_notice = st.session_state.pop("manual_unavail_notice", None)
if manual_unavail_notice:
    st.warning(
        f"⚠️ **Annonce manuelle enregistrée mais marquée comme indisponible** ({manual_unavail_notice}) "
        "et masquée par défaut dans le flux actif."
    )


# --- CHARGEMENT DES ANNONCES PAR CATÉGORIE ---
all_available_listings = get_all_listings(include_quarantined=False, include_unavailable=False)
unavailable_listings = get_unavailable_listings()
selected_listings = get_selected_listings()
quarantined_listings = get_quarantined_listings()

# --- EN-TÊTE DU COCKPIT ---
st.title("🏡 Cockpit — Mandataire Immobilier Virtuel (100% Live)")
has_key = bool(st.session_state["gemini_api_key"])
api_badge = "🟢 Clé Gemini active" if has_key else "⚪ Mode Local (Clé Gemini non renseignée)"
tx_badge = "🔑 Location" if current_criteria.get("transaction_type") == "rent" else "🏷️ Achat"
price_suffix = "€/mois" if current_criteria.get("transaction_type") == "rent" else "€"
rooms_badge = f"{current_criteria.get('min_rooms', 0)}+ p." if current_criteria.get("min_rooms", 0) > 0 else "Toutes pièces"
beds_badge = f"{current_criteria.get('min_bedrooms', 0)}+ ch." if current_criteria.get("min_bedrooms", 0) > 0 else "Toutes ch."
st.markdown(
    f"**{api_badge}** | **{tx_badge}** | "
    f"💰 Budget max : **{current_criteria['max_budget']:,} {price_suffix}** | "
    f"📐 Surface min : **{current_criteria['min_surface']:g} m²** | "
    f"🛏️ Pièces/Ch. : **{rooms_badge} / {beds_badge}** | "
    f"🏠 Types : **{', '.join(current_criteria['property_types'])}** | "
    f"📍 Secteurs : **{', '.join(current_criteria['cities'])}** | "
    f"🚗 Parking/Garage : **{'Obligatoire' if current_criteria['require_parking'] else 'Optionnel'}**".replace(",", " ")
)

# --- INDICATEURS CLÉS (KPIs) ---
a_visiter_list = [x for x in all_available_listings if x["status"] == "a_visiter"]
a_verifier_list = [x for x in all_available_listings if x["status"] == "a_verifier"]
rejete_list = [x for x in all_available_listings if x["status"] == "rejete"]
pending_list = [x for x in all_available_listings if x["status"] == "pending_analysis"]

col_sel, col1, col2, col3, col4, col5, col6 = st.columns(7)
col_sel.metric(
    "⭐ Sélectionnées",
    len(selected_listings),
    help="Annonces retenues pour vos contacts et visites programmées",
)
col1.metric("🟢 À Visiter (Opportunités)", len(a_visiter_list))
col2.metric("🟡 À Vérifier", len(a_verifier_list))
col3.metric("🔴 Rejetées IA (No-Go)", len(rejete_list))
col4.metric("⏳ En attente", len(pending_list))
col5.metric(
    "🚫 Indisponibles",
    len(unavailable_listings),
    help="Annonces vendues, sous compromis ou retirées (masquées par défaut)",
)
col6.metric(
    "🗑️ Quarantaine",
    len(quarantined_listings),
    help="Annonces supprimées / masquées du flux actif",
)

st.divider()

# --- ONGLETS PRINCIPAUX DU COCKPIT ---
tab_cockpit, tab_selection, tab_quarantaine, tab_portails, tab_ajout = st.tabs(
    [
        f"🎯 Annonces du Marché ({len(all_available_listings)})",
        f"⭐ Sélectionnées pour Visites ({len(selected_listings)})",
        f"🗑️ Quarantaine ({len(quarantined_listings)})",
        "🌐 Liens Directs Portails (Bien'ici / Orpi / LeBonCoin / SeLoger)",
        "➕ Tester / Importer une Annonce",
    ]
)

# ==============================================================================
# ONGLET 1 : TOUTES LES ANNONCES DU MARCHÉ (TRIÉES PAR L'IA)
# ==============================================================================
with tab_cockpit:
    show_unavailable = False
    if unavailable_listings:
        u_col1, u_col2 = st.columns([3, 1.4])
        with u_col1:
            show_unavailable = st.checkbox(
                f"🚫 Afficher aussi les annonces plus disponibles ({len(unavailable_listings)})",
                value=False,
                key="show_unavailable_listings",
                help="Cochez pour revoir les annonces vendues, sous compromis ou retirées du marché.",
            )
        with u_col2:
            if st.button(
                f"🧹 Masquer en quarantaine ({len(unavailable_listings)})",
                key="btn_quarantine_unavailable",
                use_container_width=True,
                help="Déplace définitivement les annonces indisponibles vers l'onglet Quarantaine.",
            ):
                q_cnt = quarantine_unavailable_listings()
                st.toast(f"🧹 {q_cnt} annonce(s) indisponible(s) déplacée(s) en Quarantaine.", icon="🧹")
                st.rerun()

    pool_listings = all_available_listings + unavailable_listings if show_unavailable else all_available_listings

    if not all_available_listings and not unavailable_listings:
        st.info(
            "👋 Aucune annonce active en base. Cliquez sur **🚀 Lancer la recherche (Live Scraping)** "
            "dans le panneau de gauche pour extraire les annonces en direct depuis Bien'ici et les agences !"
        )
    elif not pool_listings:
        st.warning(
            f"🚫 Toutes les annonces en base ({len(unavailable_listings)}) sont actuellement marquées comme "
            "indisponibles / vendues et masquées par défaut. Lancez une nouvelle recherche ou cochez la case ci-dessus pour les afficher."
        )
    else:
        f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns([1.4, 1.4, 1.2, 0.8, 1])
        with f_col1:
            default_statuses = ["a_visiter", "a_verifier"]
            if pending_list and not a_visiter_list and not a_verifier_list:
                default_statuses.append("pending_analysis")
            if show_unavailable and not a_visiter_list and not a_verifier_list and not pending_list:
                default_statuses.append("rejete")
            status_choice = st.multiselect(
                "Filtrer par décision IA",
                options=["a_visiter", "a_verifier", "pending_analysis", "rejete"],
                default=default_statuses,
                format_func=lambda s: {
                    "a_visiter": "🟢 À visiter (Opportunités)",
                    "a_verifier": "🟡 À vérifier (Infos manquantes)",
                    "pending_analysis": "⏳ En attente d'analyse IA",
                    "rejete": "🔴 Rejeté (Critères No-Go)",
                }.get(s, s),
            )
        with f_col2:
            available_cities = sorted(list({x["city"] for x in pool_listings if x.get("city")}))
            city_choice = st.multiselect(
                "Filtrer par ville",
                options=available_cities,
                default=available_cities,
            )
        with f_col3:
            search_kw = st.text_input(
                "🔍 Recherche mot-clé",
                value="",
                placeholder="Ex: jardin, Barbieux, plain-pied...",
            ).strip().lower()
        with f_col4:
            min_score_filter = st.slider("Score IA min", 0, 10, 0)
        with f_col5:
            selection_filter = st.selectbox(
                "Filtre sélection",
                options=["Toutes", "⭐ Sélectionnées uniquement", "Non sélectionnées"],
            )

        filtered = [
            x
            for x in pool_listings
            if x["status"] in status_choice
            and (not city_choice or x["city"] in city_choice)
            and (x["ai_score"] is None or x["ai_score"] >= min_score_filter)
            and (
                not search_kw
                or search_kw in (x.get("title") or "").lower()
                or search_kw in (x.get("description") or "").lower()
                or search_kw in (x.get("source") or "").lower()
            )
            and (
                selection_filter == "Toutes"
                or (selection_filter == "⭐ Sélectionnées uniquement" and x.get("is_selected"))
                or (selection_filter == "Non sélectionnées" and not x.get("is_selected"))
            )
        ]

        # --- SÉLECTEUR DE MODE D'AFFICHAGE ---
        view_mode = st.radio(
            "Mode d'affichage",
            options=["🃏 Cartes détaillées", "📊 Tableau (type CSV)"],
            horizontal=True,
            key="market_view_mode",
        )

        active_view_listings = filtered
        selected_columns = DEFAULT_TABLE_COLUMNS

        if view_mode == "📊 Tableau (type CSV)":
            def _sync_multiselect_state(state_key: str, valid_options: List[str], default_all: bool = True) -> None:
                if state_key not in st.session_state:
                    st.session_state[state_key] = list(valid_options) if default_all else []
                else:
                    current = st.session_state[state_key]
                    cleaned = [v for v in current if v in valid_options]
                    if cleaned != current:
                        st.session_state[state_key] = cleaned

            with st.expander("🔎 Filtres par colonnes & choix des colonnes", expanded=True):
                dpe_order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "F": 5, "G": 6}
                available_types = sorted({str(x.get("property_type") or "") for x in filtered if x.get("property_type")})
                available_tx = sorted({"Location" if x.get("transaction_type") == "rent" else "Achat" for x in filtered})
                available_dpes = sorted(
                    {str(x.get("dpe") or "Non renseigné") for x in filtered},
                    key=lambda d: (dpe_order.get(d, 99), d),
                )
                available_sources = sorted({str(x.get("source") or "") for x in filtered if x.get("source")})

                _sync_multiselect_state("tbl_filter_type", available_types, default_all=True)
                _sync_multiselect_state("tbl_filter_tx", available_tx, default_all=True)
                _sync_multiselect_state("tbl_filter_dpe", available_dpes, default_all=True)
                _sync_multiselect_state("tbl_filter_sources", available_sources, default_all=False)
                _sync_multiselect_state("tbl_visible_cols", TABLE_COLUMNS, default_all=False)
                if not st.session_state["tbl_visible_cols"]:
                    st.session_state["tbl_visible_cols"] = list(DEFAULT_TABLE_COLUMNS)

                r1_c1, r1_c2, r1_c3, r1_c4 = st.columns(4)
                with r1_c1:
                    type_choice_tbl = st.multiselect(
                        "Type de bien",
                        options=available_types,
                        key="tbl_filter_type",
                    )
                with r1_c2:
                    tx_choice_tbl = st.multiselect(
                        "Transaction",
                        options=available_tx,
                        key="tbl_filter_tx",
                    )
                with r1_c3:
                    dpe_choice_tbl = st.multiselect(
                        "DPE",
                        options=available_dpes,
                        key="tbl_filter_dpe",
                    )
                with r1_c4:
                    parking_choice_tbl = st.selectbox(
                        "Parking / Garage",
                        options=["Tous", "Oui ✅", "Non ❌"],
                        key="tbl_filter_parking",
                    )

                r2_c1, r2_c2, r2_c3, r2_c4 = st.columns(4)
                prices_vals = [int(x.get("price") or 0) for x in filtered]
                price_range = None
                with r2_c1:
                    if prices_vals and max(prices_vals) > min(prices_vals):
                        min_p, max_p = min(prices_vals), max(prices_vals)
                        if "tbl_filter_price" not in st.session_state:
                            st.session_state["tbl_filter_price"] = (min_p, max_p)
                        else:
                            lo_p, hi_p = st.session_state["tbl_filter_price"]
                            lo_p = max(min_p, min(max_p, lo_p))
                            hi_p = max(lo_p, min(max_p, hi_p))
                            st.session_state["tbl_filter_price"] = (lo_p, hi_p)
                        price_range = st.slider(
                            "Prix (€)",
                            min_value=min_p,
                            max_value=max_p,
                            key="tbl_filter_price",
                        )
                    elif prices_vals:
                        st.caption(f"Prix unique : {prices_vals[0]:,} €".replace(",", " "))

                surface_vals = [float(x.get("surface") or 0.0) for x in filtered]
                surface_range = None
                with r2_c2:
                    if surface_vals:
                        min_s = int(min(surface_vals))
                        max_s = int(max(surface_vals)) + (1 if max(surface_vals) % 1 else 0)
                        if max_s > min_s:
                            if "tbl_filter_surface" not in st.session_state:
                                st.session_state["tbl_filter_surface"] = (min_s, max_s)
                            else:
                                lo_s, hi_s = st.session_state["tbl_filter_surface"]
                                lo_s = max(min_s, min(max_s, lo_s))
                                hi_s = max(lo_s, min(max_s, hi_s))
                                st.session_state["tbl_filter_surface"] = (lo_s, hi_s)
                            surface_range = st.slider(
                                "Surface (m²)",
                                min_value=min_s,
                                max_value=max_s,
                                key="tbl_filter_surface",
                            )
                        else:
                            st.caption(f"Surface unique : {min_s} m²")

                with r2_c3:
                    min_rooms_tbl = st.number_input(
                        "Pièces min",
                        min_value=0,
                        max_value=20,
                        value=0,
                        step=1,
                        key="tbl_filter_rooms",
                    )
                with r2_c4:
                    min_beds_tbl = st.number_input(
                        "Chambres min",
                        min_value=0,
                        max_value=15,
                        value=0,
                        step=1,
                        key="tbl_filter_beds",
                    )

                r3_c1, r3_c2 = st.columns(2)
                with r3_c1:
                    source_choice_tbl = st.multiselect(
                        "Source / Agence",
                        options=available_sources,
                        placeholder="Toutes les sources...",
                        key="tbl_filter_sources",
                    )
                with r3_c2:
                    selected_columns = st.multiselect(
                        "Colonnes affichées",
                        options=TABLE_COLUMNS,
                        key="tbl_visible_cols",
                    )

            active_view_listings = [
                x
                for x in filtered
                if (not available_types or str(x.get("property_type") or "") in type_choice_tbl)
                and (not available_tx or ("Location" if x.get("transaction_type") == "rent" else "Achat") in tx_choice_tbl)
                and (not available_dpes or str(x.get("dpe") or "Non renseigné") in dpe_choice_tbl)
                and (
                    parking_choice_tbl == "Tous"
                    or (parking_choice_tbl == "Oui ✅" and bool(x.get("has_parking")))
                    or (parking_choice_tbl == "Non ❌" and not bool(x.get("has_parking")))
                )
                and (price_range is None or price_range[0] <= int(x.get("price") or 0) <= price_range[1])
                and (surface_range is None or surface_range[0] <= float(x.get("surface") or 0.0) <= surface_range[1])
                and (min_rooms_tbl == 0 or int(x.get("rooms") or 0) >= min_rooms_tbl)
                and (min_beds_tbl == 0 or int(x.get("bedrooms") or 0) >= min_beds_tbl)
                and (not source_choice_tbl or str(x.get("source") or "") in source_choice_tbl)
            ]

        # --- MODULE D'EXPORTATION EXCEL & CSV (GÉNÉRATION À LA DEMANDE POUR NE PAS RALENTIR LE RENDU) ---
        with st.expander("📥 Exporter les données (Excel .xlsx / CSV France)", expanded=False):
            exp_col1, exp_col2 = st.columns([2, 1])
            with exp_col1:
                scope_choice = st.radio(
                    "Périmètre de l'export :",
                    options=["selection_user", "filtres_actifs", "pepites", "tout"],
                    format_func=lambda k: {
                        "selection_user": f"⭐ Annonces sélectionnées pour visites ({len(selected_listings)} annonces)",
                        "filtres_actifs": f"🎯 Filtres actuels ({len(active_view_listings)} annonces)",
                        "pepites": f"💡 Opportunités retenues IA ({len(a_visiter_list) + len(a_verifier_list)} annonces)",
                        "tout": f"📁 Marché disponible ({len(all_available_listings)} annonces)",
                    }.get(k, k),
                    horizontal=True,
                )

            if scope_choice == "selection_user":
                export_pool = selected_listings
            elif scope_choice == "filtres_actifs":
                export_pool = active_view_listings
            elif scope_choice == "pepites":
                export_pool = [x for x in all_available_listings if x["status"] in ("a_visiter", "a_verifier")]
            else:
                export_pool = all_available_listings

            with exp_col2:
                if export_pool:
                    if st.button("⚙️ Préparer les fichiers d'export (.xlsx & .csv)", use_container_width=True):
                        csv_b, excel_b = generate_export_files(export_pool)
                        st.session_state["ready_export_csv"] = csv_b
                        st.session_state["ready_export_xlsx"] = excel_b
                        st.session_state["ready_export_scope"] = scope_choice
                        st.session_state["ready_export_ts"] = datetime.now().strftime("%Y%m%d_%H%M")

                    if st.session_state.get("ready_export_scope") == scope_choice and st.session_state.get("ready_export_xlsx"):
                        ts = st.session_state["ready_export_ts"]
                        dl_col1, dl_col2 = st.columns(2)
                        with dl_col1:
                            st.download_button(
                                label="📊 Télécharger Excel (.xlsx)",
                                data=st.session_state["ready_export_xlsx"],
                                file_name=f"mandataire_immo_{scope_choice}_{ts}.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True,
                            )
                        with dl_col2:
                            st.download_button(
                                label="📄 Télécharger CSV (FR)",
                                data=st.session_state["ready_export_csv"],
                                file_name=f"mandataire_immo_{scope_choice}_{ts}.csv",
                                mime="text/csv",
                                use_container_width=True,
                            )
                else:
                    st.caption("Aucune annonce à exporter pour ce périmètre.")

        if view_mode == "📊 Tableau (type CSV)":
            st.caption(
                f"Affichage de **{len(active_view_listings)}** annonce(s) — "
                "Cliquez sur l'en-tête d'une colonne pour trier, utilisez la loupe du tableau pour rechercher, "
                "ou cochez des lignes pour agir dessus."
            )
            df_table = build_table_dataframe(active_view_listings)
            event = st.dataframe(
                df_table,
                width="stretch",
                height=620,
                hide_index=True,
                column_order=selected_columns if selected_columns else None,
                on_select="rerun",
                selection_mode="multi-row",
                key="market_table_df",
                column_config={
                    "Score IA": st.column_config.ProgressColumn("Score IA", min_value=0, max_value=10, format="%d/10"),
                    "Prix (€)": st.column_config.NumberColumn("Prix (€)", format="%d €"),
                    "Surface (m²)": st.column_config.NumberColumn("Surface (m²)", format="%.0f m²"),
                    "Prix/m² (€)": st.column_config.NumberColumn("Prix/m² (€)", format="%.0f €/m²"),
                    "Sélectionné": st.column_config.CheckboxColumn("⭐"),
                    "Parking": st.column_config.CheckboxColumn("🚗"),
                    "URL": st.column_config.LinkColumn("Annonce", display_text="🔗 Ouvrir"),
                },
            )

            selected_row_indices = (
                [idx for idx in event.selection.rows if 0 <= idx < len(active_view_listings)]
                if event and getattr(event, "selection", None)
                else []
            )
            if selected_row_indices:
                n_sel = len(selected_row_indices)
                t_act1, t_act2, t_act3 = st.columns(3)
                with t_act1:
                    if st.button(
                        f"⭐ Ajouter à ma sélection ({n_sel})",
                        key="tbl_bulk_sel",
                        type="primary",
                        use_container_width=True,
                    ):
                        for idx in selected_row_indices:
                            ad_item = active_view_listings[idx]
                            def_agency = extract_default_agency_name(ad_item.get("source", ""))
                            def_phone = extract_phone_number(ad_item.get("description", ""))
                            set_listing_selection(ad_item["id"], True, default_agency_name=def_agency, default_phone=def_phone)
                        st.toast(f"⭐ {n_sel} annonce(s) ajoutée(s) à votre sélection !", icon="⭐")
                        st.rerun()
                with t_act2:
                    if st.button(
                        f"❌ Retirer de ma sélection ({n_sel})",
                        key="tbl_bulk_unsel",
                        use_container_width=True,
                    ):
                        for idx in selected_row_indices:
                            set_listing_selection(active_view_listings[idx]["id"], False)
                        st.toast(f"{n_sel} annonce(s) retirée(s) de la sélection.", icon="ℹ️")
                        st.rerun()
                with t_act3:
                    if st.button(
                        f"🗑️ Mettre en quarantaine ({n_sel})",
                        key="tbl_bulk_quar",
                        use_container_width=True,
                    ):
                        for idx in selected_row_indices:
                            set_listing_quarantine(active_view_listings[idx]["id"], True)
                        st.session_state.pop("market_table_df", None)
                        st.toast(f"🗑️ {n_sel} annonce(s) déplacée(s) en Quarantaine.", icon="🗑️")
                        st.rerun()

                if n_sel == 1:
                    ad_detail = active_view_listings[selected_row_indices[0]]
                    with st.expander("📄 Aperçu détaillé de l'annonce sélectionnée", expanded=True):
                        st.markdown(f"#### {_safe_md_text(ad_detail.get('title', ''))}")
                        st.link_button("🔗 Ouvrir l'annonce réelle", url=ad_detail["url"])
                        if not ad_detail.get("is_available", True):
                            st.error(
                                f"🚫 **Annonce plus disponible** : "
                                f"{ad_detail.get('unavailable_reason') or 'Bien vendu, sous compromis ou retiré du marché'}"
                            )
                        if ad_detail.get("ai_rejection_reason"):
                            st.error(f"🚫 **Motif d'exclusion :** {ad_detail['ai_rejection_reason']}")
                        elif ad_detail.get("ai_summary"):
                            st.info(f"💡 **Avis du Mandataire IA :** {ad_detail['ai_summary']}")
                        d_pros, d_cons = st.columns(2)
                        with d_pros:
                            if ad_detail.get("ai_pros"):
                                st.markdown("**✅ Points forts :**")
                                for p in ad_detail["ai_pros"]:
                                    st.markdown(f"- {p}")
                        with d_cons:
                            if ad_detail.get("ai_cons") or ad_detail.get("ai_signals"):
                                st.markdown("**⚠️ Vigilance & Signaux :**")
                                for c in (ad_detail.get("ai_cons") or []) + (ad_detail.get("ai_signals") or []):
                                    st.markdown(f"- {c}")
                        st.markdown("**📝 Description complète :**")
                        st.write(ad_detail.get("description") or "")
        else:
            # --- PAGINATION HAUTE PERFORMANCE DU DOM STREAMLIT ---
            total_filtered = len(active_view_listings)
            p_col1, p_col2, p_col3 = st.columns([2, 1, 1])
            with p_col2:
                page_size = st.selectbox(
                    "Annonces par page",
                    options=[25, 50, 100],
                    index=0,
                    key="market_page_size",
                )
            total_pages = max(1, (total_filtered + page_size - 1) // page_size)
            with p_col3:
                current_page = st.number_input(
                    f"Page (sur {total_pages})",
                    min_value=1,
                    max_value=total_pages,
                    value=1,
                    step=1,
                    key="market_page_num",
                )
            start_idx = (current_page - 1) * page_size
            end_idx = min(total_filtered, start_idx + page_size)
            paginated_ads = active_view_listings[start_idx:end_idx]

            with p_col1:
                st.caption(
                    f"Affichage des annonces **{start_idx + 1 if total_filtered else 0} à {end_idx}** "
                    f"sur **{total_filtered}** filtrée(s) ({len(all_available_listings)} disponible(s) en base)."
                )

            for ad in paginated_ads:
                score = ad.get("ai_score") or 0
                status = ad.get("status")
                if status == "a_visiter":
                    badge = f"🟢 **À VISITER** — Score IA : **{score}/10**"
                elif status == "a_verifier":
                    badge = f"🟡 **À VÉRIFIER** — Score IA : **{score}/10**"
                elif status == "pending_analysis":
                    badge = "⏳ **EN ATTENTE D'ANALYSE IA**"
                else:
                    badge = f"🔴 **REJETÉ** — Score IA : **{score}/10**"

                with st.container(border=True):
                    top_left, top_right = st.columns([4, 1])
                    with top_left:
                        sel_pill = " &nbsp;|&nbsp; 🌟 **SÉLECTIONNÉE**" if ad.get("is_selected") else ""
                        tx_pill = "🔑 Location" if ad.get("transaction_type") == "rent" else "🏷️ Achat"
                        beds_str = f", {ad.get('bedrooms')} ch." if ad.get("bedrooms") else ""
                        safe_title = _safe_md_text(ad.get("title", ""))
                        safe_source = _safe_md_text(ad.get("source", ""))
                        st.markdown(f"### {safe_title}")
                        st.markdown(
                            f"{badge}{sel_pill} &nbsp;|&nbsp; **{tx_pill}** &nbsp;|&nbsp; 📍 **{ad['city']} ({ad['postal_code']})** &nbsp;|&nbsp; "
                            f"🏢 **{safe_source}** &nbsp;|&nbsp; "
                            f"🏠 **{ad['property_type']}** ({ad.get('rooms', '?')} p.{beds_str}) &nbsp;|&nbsp; "
                            f"🔋 DPE : **`{ad.get('dpe', 'N/C')}`** &nbsp;|&nbsp; "
                            f"🚗 Parking/Garage : **{'Oui ✅' if ad.get('has_parking') else 'Non ❌'}**"
                        )
                    with top_right:
                        st.metric(
                            label=f"{ad['surface']:g} m²",
                            value=f"{ad['price']:,} €".replace(",", " "),
                            delta=f"{ad.get('price_per_m2', 0):,.0f} €/m²".replace(",", " "),
                            delta_color="off",
                        )

                    if not ad.get("is_available", True):
                        st.error(
                            f"🚫 **Annonce plus disponible** : "
                            f"{ad.get('unavailable_reason') or 'Bien vendu, sous compromis ou retiré du marché'}"
                        )

                    if ad.get("ai_rejection_reason"):
                        st.error(f"🚫 **Motif d'exclusion :** {ad['ai_rejection_reason']}")
                    elif ad.get("ai_summary"):
                        st.info(f"💡 **Avis du Mandataire IA :** {ad['ai_summary']}")

                    c_pros, c_cons = st.columns(2)
                    with c_pros:
                        if ad.get("ai_pros"):
                            st.markdown("**✅ Points forts :**")
                            for p in ad["ai_pros"]:
                                st.markdown(f"- {p}")
                    with c_cons:
                        if ad.get("ai_cons") or ad.get("ai_signals"):
                            st.markdown("**⚠️ Vigilance & Signaux :**")
                            for c in (ad.get("ai_cons") or []) + (ad.get("ai_signals") or []):
                                st.markdown(f"- {c}")

                    # Ligne des boutons d'action
                    act_col1, act_col2, act_col3 = st.columns([2, 1.5, 1.5])
                    with act_col1:
                        st.link_button(
                            "🔗 Ouvrir l'annonce réelle",
                            url=ad["url"],
                            use_container_width=True,
                        )
                    with act_col2:
                        if ad.get("is_selected"):
                            if st.button("⭐ Sélectionnée (Retirer)", key=f"unsel_main_{ad['id']}", use_container_width=True):
                                set_listing_selection(ad["id"], False)
                                st.toast("Annonce retirée de la sélection.", icon="ℹ️")
                                st.rerun()
                        else:
                            if st.button("⭐ Sélectionner", key=f"sel_main_{ad['id']}", type="primary", use_container_width=True):
                                def_agency = extract_default_agency_name(ad.get("source", ""))
                                def_phone = extract_phone_number(ad.get("description", ""))
                                set_listing_selection(ad["id"], True, default_agency_name=def_agency, default_phone=def_phone)
                                st.toast("⭐ Annonce ajoutée à vos annonces sélectionnées !", icon="⭐")
                                st.rerun()
                    with act_col3:
                        if st.button("🗑️ Supprimer", key=f"del_main_{ad['id']}", help="Masquer de la sélection et déplacer en Quarantaine", use_container_width=True):
                            set_listing_quarantine(ad["id"], True)
                            st.toast("🗑️ Annonce déplacée en Quarantaine (masquée).", icon="🗑️")
                            st.rerun()

                    with st.expander("📄 Lire la description complète de l'annonce"):
                        st.write(ad["description"])


# ==============================================================================
# ONGLET 2 : ANNONCES SÉLECTIONNÉES (SUIVI DE CONTACT, VISITES & CRM)
# ==============================================================================
with tab_selection:
    st.subheader(f"⭐ Vos Annonces Sélectionnées pour Visites ({len(selected_listings)})")
    st.caption(
        "Retrouvez ici les biens retenus pour vos démarches. Renseignez et suivez les prises de contact avec les agences "
        "et la programmation des visites."
    )

    if not selected_listings:
        st.info(
            "💡 **Aucune annonce n'est encore sélectionnée.**\n\n"
            "Pour ajouter une annonce ici, parcourez l'onglet **🎯 Annonces du Marché** et cliquez sur le bouton "
            "**⭐ Sélectionner** sur les fiches des biens qui retiennent votre attention."
        )
    else:
        cnt_contact = sum(1 for x in selected_listings if x.get("contact_made"))
        cnt_visit = sum(1 for x in selected_listings if x.get("visit_scheduled"))
        avg_price = sum(x["price"] for x in selected_listings) / len(selected_listings)

        kpi_s1, kpi_s2, kpi_s3, kpi_s4 = st.columns(4)
        kpi_s1.metric("⭐ Annonces retenues", len(selected_listings))
        kpi_s2.metric("📞 Agences contactées", f"{cnt_contact} / {len(selected_listings)}")
        kpi_s3.metric("📅 Visites programmées", f"{cnt_visit} / {len(selected_listings)}")
        kpi_s4.metric("💶 Budget moyen sélectionné", f"{avg_price:,.0f} €".replace(",", " "))

        st.divider()

        for ad in selected_listings:
            score = ad.get("ai_score") or 0
            with st.container(border=True):
                if not ad.get("is_available", True):
                    st.error(
                        f"🚫 **Attention : cette annonce n'est plus disponible** "
                        f"({ad.get('unavailable_reason') or 'Retirée du marché / vendue'})."
                    )
                top_left, top_right = st.columns([4, 1])
                with top_left:
                    st.markdown(f"### ⭐ {ad['title']}")
                    contact_badge = "🟢 Contact pris" if ad.get("contact_made") else "⚪ À contacter"
                    visit_badge = "🗓️ Visite programmée" if ad.get("visit_scheduled") else "⚪ Visite à planifier"
                    beds_str = f", {ad.get('bedrooms')} ch." if ad.get("bedrooms") else ""
                    st.markdown(
                        f"**{contact_badge}** &nbsp;|&nbsp; **{visit_badge}** &nbsp;|&nbsp; "
                        f"📍 **{ad['city']} ({ad['postal_code']})** &nbsp;|&nbsp; "
                        f"🏠 **{ad['property_type']}** ({ad.get('rooms', '?')} p.{beds_str}) &nbsp;|&nbsp; "
                        f"🔋 DPE : **`{ad.get('dpe', 'N/C')}`** &nbsp;|&nbsp; "
                        f"🚗 Parking/Garage : **{'Oui ✅' if ad.get('has_parking') else 'Non ❌'}**"
                    )
                with top_right:
                    st.metric(
                        label=f"{ad['surface']:g} m²",
                        value=f"{ad['price']:,} €".replace(",", " "),
                        delta=f"{ad.get('price_per_m2', 0):,.0f} €/m²".replace(",", " "),
                        delta_color="off",
                    )

                if ad.get("ai_summary"):
                    st.info(f"💡 **Synthèse IA :** {ad['ai_summary']}")

                # Formulaire interactif de suivi de l'annonce
                st.markdown("##### 📋 Suivi de la prospection & Agence :")
                with st.form(key=f"followup_form_{ad['id']}"):
                    f_col1, f_col2, f_col3, f_col4 = st.columns([1, 1, 1.5, 1.5])
                    with f_col1:
                        contact_radio = st.radio(
                            "Contact agence établi ?",
                            options=["Non", "Oui"],
                            index=1 if ad.get("contact_made") else 0,
                            horizontal=True,
                            key=f"c_radio_{ad['id']}",
                        )
                    with f_col2:
                        visit_radio = st.radio(
                            "Visite programmée ?",
                            options=["Non", "Oui"],
                            index=1 if ad.get("visit_scheduled") else 0,
                            horizontal=True,
                            key=f"v_radio_{ad['id']}",
                        )
                    with f_col3:
                        init_agency = ad.get("agency_name") or extract_default_agency_name(ad.get("source", ""))
                        agency_name_val = st.text_input(
                            "Nom de l'agence",
                            value=init_agency,
                            placeholder="Ex: Deleu Immobilier, Orpi, IAD...",
                            key=f"ag_name_{ad['id']}",
                        )
                    with f_col4:
                        init_phone = ad.get("agency_phone") or extract_phone_number(ad.get("description", ""))
                        agency_phone_val = st.text_input(
                            "Numéro de téléphone",
                            value=init_phone,
                            placeholder="Ex: 06 12 34 56 78",
                            key=f"ag_phone_{ad['id']}",
                        )

                    followup_notes_val = st.text_area(
                        "Notes de suivi (créneau de visite, interlocuteur, points clés à vérifier...)",
                        value=ad.get("followup_notes") or "",
                        placeholder="Ex: Contacté le 30/10 avec Mme Leroy. Visite samedi à 14h. Vérifier l'isolation de la toiture.",
                        height=65,
                        key=f"fu_notes_{ad['id']}",
                    )

                    btn_save = st.form_submit_button(
                        "💾 Enregistrer le suivi de cette annonce",
                        use_container_width=True,
                        type="primary",
                    )
                    if btn_save:
                        update_listing_followup(
                            listing_id=ad["id"],
                            contact_made=(contact_radio == "Oui"),
                            visit_scheduled=(visit_radio == "Oui"),
                            agency_name=agency_name_val,
                            agency_phone=agency_phone_val,
                            followup_notes=followup_notes_val,
                        )
                        st.toast("✅ Suivi mis à jour et sauvegardé !", icon="💾")
                        st.rerun()

                # Boutons d'action sous le suivi
                b_col1, b_col2, b_col3, b_col4 = st.columns([1.5, 1.5, 1, 1])
                with b_col1:
                    st.link_button("🔗 Ouvrir l'annonce réelle", url=ad["url"], use_container_width=True)
                with b_col2:
                    current_phone = (ad.get("agency_phone") or "").strip()
                    if current_phone:
                        clean_tel = re.sub(r"[^0-9+]", "", current_phone)
                        st.link_button(
                            f"📞 Appeler ({current_phone})",
                            url=f"tel:{clean_tel}",
                            use_container_width=True,
                        )
                    else:
                        st.caption("Numéro de téléphone non renseigné")
                with b_col3:
                    if st.button("❌ Retirer", key=f"unsel_tab_{ad['id']}", help="Retirer de la sélection", use_container_width=True):
                        set_listing_selection(ad["id"], False)
                        st.toast("Annonce retirée de la sélection.", icon="ℹ️")
                        st.rerun()
                with b_col4:
                    if st.button("🗑️ Supprimer", key=f"del_tab_{ad['id']}", help="Déplacer en Quarantaine", use_container_width=True):
                        set_listing_quarantine(ad["id"], True)
                        st.toast("Annonce déplacée en Quarantaine.", icon="🗑️")
                        st.rerun()

                with st.expander("📄 Lire la description complète de l'annonce"):
                    st.write(ad["description"])


# ==============================================================================
# ONGLET 3 : QUARANTAINE (ANNONCES SUPPRIMÉES / MASQUÉES)
# ==============================================================================
with tab_quarantaine:
    st.subheader(f"🗑️ Quarantaine des Annonces ({len(quarantined_listings)})")
    st.caption(
        "Ces annonces ont été supprimées et ne sont plus affichées dans votre sélection principale. "
        "Vous pouvez les restaurer à tout moment dans le flux actif ou les purger définitivement."
    )

    if not quarantined_listings:
        st.success("✅ La quarantaine est vide. Aucune annonce n'a été supprimée.")
    else:
        q_act1, q_act2 = st.columns([1, 1])
        with q_act1:
            if st.button("♻️ Tout restaurer vers les annonces actives", use_container_width=True):
                restored_cnt = restore_all_quarantine()
                st.toast(f"✅ {restored_cnt} annonce(s) restaurée(s) !", icon="♻️")
                st.rerun()
        with q_act2:
            if st.button("❌ Vider définitivement la quarantaine", use_container_width=True, type="secondary"):
                deleted_cnt = empty_quarantine()
                st.toast(f"🗑️ {deleted_cnt} annonce(s) définitivement supprimée(s).", icon="🗑️")
                st.rerun()

        st.divider()

        for ad in quarantined_listings:
            score = ad.get("ai_score") or 0
            with st.container(border=True):
                top_left, top_right = st.columns([4, 1])
                with top_left:
                    st.markdown(f"### 🗑️ {ad['title']}")
                    st.markdown(
                        f"🔴 **EN QUARANTAINE** (Score IA : **{score}/10**) &nbsp;|&nbsp; "
                        f"📍 **{ad['city']} ({ad['postal_code']})** &nbsp;|&nbsp; "
                        f"🏢 **{ad['source']}** &nbsp;|&nbsp; "
                        f"🏠 **{ad['property_type']}** ({ad.get('rooms', '?')} p.) &nbsp;|&nbsp; "
                        f"🔋 DPE : **`{ad.get('dpe', 'N/C')}`**"
                    )
                with top_right:
                    st.metric(
                        label=f"{ad['surface']:g} m²",
                        value=f"{ad['price']:,} €".replace(",", " "),
                        delta=f"{ad.get('price_per_m2', 0):,.0f} €/m²".replace(",", " "),
                        delta_color="off",
                    )

                if ad.get("ai_rejection_reason"):
                    st.error(f"🚫 **Motif d'exclusion IA :** {ad['ai_rejection_reason']}")
                elif ad.get("ai_summary"):
                    st.caption(f"💡 {ad['ai_summary']}")

                q_col1, q_col2, q_col3, q_col4 = st.columns([1.5, 1.5, 1.5, 1.5])
                with q_col1:
                    if st.button("♻️ Restaurer", key=f"q_res_{ad['id']}", use_container_width=True):
                        set_listing_quarantine(ad["id"], False)
                        st.toast("Annonce restaurée dans les annonces du marché.", icon="♻️")
                        st.rerun()
                with q_col2:
                    if st.button("⭐ Restaurer & Sélectionner", key=f"q_sel_{ad['id']}", type="primary", use_container_width=True):
                        def_agency = extract_default_agency_name(ad.get("source", ""))
                        def_phone = extract_phone_number(ad.get("description", ""))
                        set_listing_selection(ad["id"], True, default_agency_name=def_agency, default_phone=def_phone)
                        st.toast("Annonce restaurée et ajoutée à la sélection !", icon="⭐")
                        st.rerun()
                with q_col3:
                    if st.button("❌ Supprimer définitivement", key=f"q_purge_{ad['id']}", use_container_width=True):
                        delete_listing_permanently(ad["id"])
                        st.toast("Annonce supprimée définitivement.", icon="🗑️")
                        st.rerun()
                with q_col4:
                    st.link_button("🔗 Ouvrir l'annonce", url=ad["url"], use_container_width=True)

                with st.expander("📄 Lire la description complète de l'annonce"):
                    st.write(ad["description"])


# ==============================================================================
# ONGLET 4 : LIENS DIRECTS PORTAILS
# ==============================================================================
with tab_portails:
    st.subheader("🌐 Recherches pré-filtrées en temps réel par commune")
    st.write(
        "Ces liens ouvrent directement les portails immobiliers avec vos filtres actuels "
        f"(Budget max **{current_criteria['max_budget']:,} €**, Surface min **{current_criteria['min_surface']:g} m²**, Parking)."
    )
    portal_links = build_portal_search_links(current_criteria)
    cols = st.columns(min(3, max(1, len(portal_links))))
    for idx, (city, links) in enumerate(portal_links.items()):
        with cols[idx % len(cols)]:
            with st.container(border=True):
                st.markdown(f"#### 📍 {city}")
                for item in links:
                    st.link_button(
                        f"Ouvrir sur {item['portail']}",
                        url=item["url"],
                        use_container_width=True,
                    )


# ==============================================================================
# ONGLET 5 : TESTER / IMPORTER UNE ANNONCE MANUELLE OU UN LOT LEBONCOIN
# ==============================================================================
with tab_ajout:
    st.subheader("🟠 Import Rapide d'une page de résultats LeBonCoin (Particuliers & Pros)")
    st.caption(
        "Si DataDome bloque le scraping direct LeBonCoin, ouvrez une recherche LeBonCoin depuis l'onglet "
        "**🌐 Liens Directs Portails** dans votre navigateur habituel, affichez le code source de la page (`Ctrl+U`), "
        "copiez tout (`Ctrl+A` puis `Ctrl+C`) et collez-le ci-dessous : les 35 annonces complètes (avec description, DPE, "
        "chambres, particulier/pro) seront extraites et analysées par l'IA d'un seul coup !"
    )
    with st.form("lbc_bulk_import_form"):
        lbc_raw_html = st.text_area(
            "Code source HTML (`Ctrl+U`) ou JSON `__NEXT_DATA__` d'une page LeBonCoin",
            height=130,
            placeholder="Collez ici le code source HTML complet de la page de recherche ou d'une annonce LeBonCoin...",
        )
        lbc_import_btn = st.form_submit_button("📥 Extraire et analyser toutes les annonces LeBonCoin", type="primary")

    if lbc_import_btn and lbc_raw_html.strip():
        extracted_lbc = parse_leboncoin_payload(lbc_raw_html, current_criteria)
        if not extracted_lbc:
            st.error("Aucune annonce LeBonCoin correspondant à vos critères n'a été détectée dans ce contenu (vérifiez qu'il contient bien la balise `__NEXT_DATA__`).")
        else:
            added_lbc, updated_lbc = upsert_listings_batch(extracted_lbc)
            st.success(
                f"✅ {len(extracted_lbc)} annonce(s) LeBonCoin extraite(s) ({added_lbc} nouvelles, {updated_lbc} mises à jour) ! "
                "Lancement de l'analyse IA..."
            )
            st.session_state["analysis_running"] = True
            st.rerun()

    st.divider()
    st.subheader("➕ Analyser une annonce unitaire (Copier-coller)")
    st.write("Vous avez repéré une annonce spécifique ? Soumettez-la au Cerveau IA :")

    with st.form("manual_ad_form"):
        m_col1, m_col2 = st.columns(2)
        with m_col1:
            m_title = st.text_input("Titre de l'annonce", placeholder="Ex: Maison 95m² avec garage - Wasquehal")
            m_tx = st.selectbox(
                "Transaction",
                options=["Achat", "Location"],
                index=1 if current_criteria.get("transaction_type") == "rent" else 0,
            )
            m_city = st.selectbox(
                "Ville",
                options=current_criteria.get("cities", list(CITY_METADATA.keys())) + ["Autre commune"],
            )
            m_type = st.selectbox("Type de bien", options=["Maison", "Appartement"])
            m_url = st.text_input("URL de l'annonce (optionnel)", placeholder="https://www.leboncoin.fr/...")
        with m_col2:
            m_price = st.number_input("Prix / Loyer (€)", min_value=200, max_value=3000000, value=1200 if current_criteria.get("transaction_type") == "rent" else 265000, step=100)
            m_surface = st.number_input("Surface (m²)", min_value=15.0, max_value=500.0, value=90.0, step=1.0)
            mr_col1, mr_col2 = st.columns(2)
            with mr_col1:
                m_rooms = st.number_input("Nombre de pièces", min_value=1, max_value=15, value=4, step=1)
            with mr_col2:
                m_bedrooms = st.number_input("Nombre de chambres", min_value=0, max_value=12, value=3, step=1)
            m_dpe = st.selectbox("DPE affiché", options=["Non renseigné", "A", "B", "C", "D", "E", "F", "G"])
            m_parking = st.checkbox("Parking / Garage mentionné", value=True)

        m_desc = st.text_area(
            "Texte complet de l'annonce",
            height=130,
            placeholder="Collez ici la description de l'annonce pour détecter les signaux faibles et travaux...",
        )
        submit_manual = st.form_submit_button("🧠 Analyser et ajouter au Cockpit", type="primary")

    if submit_manual and m_title and m_desc:
        ext_id = "manual-" + hashlib.md5(f"{m_title}-{m_price}-{m_surface}".encode("utf-8")).hexdigest()[:10]
        tx_val = "rent" if m_tx == "Location" else "buy"
        manual_unavail = detect_unavailable_reason(m_title.strip(), m_desc.strip(), tx_type=tx_val)
        ad_obj = {
            "external_id": ext_id,
            "source": "Ajout Manuel",
            "url": m_url.strip() or f"https://localhost/manual/{ext_id}",
            "title": m_title.strip(),
            "transaction_type": tx_val,
            "property_type": m_type,
            "price": int(m_price),
            "surface": float(m_surface),
            "rooms": int(m_rooms),
            "bedrooms": int(m_bedrooms),
            "city": m_city,
            "postal_code": CITY_METADATA.get(m_city, {}).get("postal_code", "59000"),
            "description": m_desc.strip(),
            "dpe": m_dpe,
            "has_parking": m_parking,
            "is_available": manual_unavail is None,
            "unavailable_reason": manual_unavail or "",
        }
        upsert_listing(ad_obj)
        pending = get_pending_listings()
        for p in pending:
            res = analyze_listing(p, current_criteria, api_key=st.session_state["gemini_api_key"])
            update_listing_analysis(p["id"], res)
        if manual_unavail:
            st.session_state["manual_unavail_notice"] = manual_unavail
        else:
            st.success("✅ Annonce analysée et ajoutée au tableau de bord !")
        st.rerun()
