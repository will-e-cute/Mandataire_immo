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
    get_all_listings,
    get_criteria,
    get_pending_listings,
    get_quarantined_listings,
    get_selected_listings,
    init_db,
    reset_all_listings_to_pending,
    restore_all_quarantine,
    save_criteria,
    set_listing_quarantine,
    set_listing_selection,
    update_listing_analysis,
    update_listing_followup,
    upsert_listing,
)
from ai_analyzer import analyze_listing, analyze_listings_parallel, test_gemini_api_key
from scraper import (
    CITY_METADATA,
    build_portal_search_links,
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

if "gemini_api_key" not in st.session_state:
    st.session_state["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "")
if "analysis_running" not in st.session_state:
    st.session_state["analysis_running"] = False
if "analysis_paused" not in st.session_state:
    st.session_state["analysis_paused"] = False


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
                "Score IA (/10)": l.get("ai_score"),
                "Titre": l.get("title"),
                "Ville": l.get("city"),
                "Code Postal": l.get("postal_code"),
                "Prix (€)": l.get("price"),
                "Surface (m²)": l.get("surface"),
                "Prix/m² (€/m²)": l.get("price_per_m2"),
                "Pièces": l.get("rooms"),
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


# --- BARRE LATÉRALE : CLÉ API GEMINI, PARALLÉLISME & CRITÈRES DE RECHERCHE ---
with st.sidebar:
    st.title("🤖 Moteur IA (Gemini)")
    api_key_input = st.text_input(
        "Clé API Gemini",
        value=st.session_state["gemini_api_key"],
        type="password",
        placeholder="AIzaSy...",
        help="Votre clé API est utilisée avec gemini-3.8-flash et peut être sauvegardée dans le fichier local .env.",
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
        st.subheader("1. Budget & Surface")
        max_budget = st.number_input(
            "Budget maximum FAI (€)",
            min_value=50000,
            max_value=2000000,
            value=int(current_criteria.get("max_budget", 280000)),
            step=5000,
        )
        min_surface = st.number_input(
            "Surface minimale (m²)",
            min_value=20.0,
            max_value=500.0,
            value=float(current_criteria.get("min_surface", 80.0)),
            step=5.0,
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
            "🔄 Ré-évaluer aussi les annonces déjà en base avec ces critères",
            value=True,
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

    if st.button("🗑️ Vider la base d'annonces", use_container_width=True):
        st.session_state["analysis_running"] = False
        delete_all_listings()
        st.toast("Base de données vidée.", icon="🗑️")
        st.rerun()


# --- ACTION 1 : LANCEMENT DU SCRAPING LIVE AVEC RÉSILIENCE WAF ---
if launch_search:
    st.session_state["analysis_paused"] = False
    parsed_cities = [c.strip() for c in cities_text.split(",") if c.strip()]
    save_criteria(
        max_budget=max_budget,
        min_surface=min_surface,
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

    with st.status("🔍 Collecte Live (SeLoger + Bien'ici + Agences & Orpi + Fallback Playwright)...", expanded=True) as status_box:
        st.write(
            f"1️⃣ Interrogation en direct de **SeLoger.com**, **Bien'ici** (IAD, Safti, Citya, Square Habitat, Guy Hoquet, ERA, Sergic...) "
            f"et **Orpi** sur : **{', '.join(parsed_cities)}** (avec protection anti-WAF active)..."
        )
        scrape_report = scrape_all_live_sources(current_criteria)
        all_fetched = scrape_report["ads"]
        stats = scrape_report["stats"]

        new_count = 0
        for ad in all_fetched:
            if upsert_listing(ad):
                new_count += 1

        if reanalyze_existing:
            reset_all_listings_to_pending()

        pending_after_scrape = get_pending_listings()
        status_box.update(
            label=(
                f"📥 Collecte terminée : {stats['total']} annonces récupérées "
                f"({stats.get('seloger', 0)} SeLoger, {stats['bienici_agences']} Bien'ici, {stats['orpi_direct']} Orpi) — "
                f"{new_count} nouvelles en base. Démarrage du tri parallèle sur {len(pending_after_scrape)} annonces..."
            ),
            state="complete",
            expanded=False,
        )
    st.session_state["analysis_running"] = True


# --- ACTION 2 : EXÉCUTION DE L'ANALYSE IA EN PARALLÈLE ---
if st.session_state.get("analysis_running") and not st.session_state.get("analysis_paused"):
    pending = get_pending_listings()
    if pending:
        active_key = st.session_state["gemini_api_key"]
        engine_label = "API Gemini (gemini-3.8-flash)" if active_key else "Moteur de règles local (sans clé API)"
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

        def on_item_completed(item: Dict[str, Any], analysis: Dict[str, Any], count: int, total: int):
            update_listing_analysis(item["id"], analysis)
            completed_counter[0] = count
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
            model_name="gemini-3.8-flash",
            max_workers=max_workers,
            on_completed=on_item_completed,
            check_cancelled=check_cancelled,
        )

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


# --- CHARGEMENT DES ANNONCES PAR CATÉGORIE ---
all_active_listings = get_all_listings(include_quarantined=False)
selected_listings = get_selected_listings()
quarantined_listings = get_quarantined_listings()

# --- EN-TÊTE DU COCKPIT ---
st.title("🏡 Cockpit — Mandataire Immobilier Virtuel (100% Live)")
api_badge = "🟢 Clé Gemini configurée" if st.session_state["gemini_api_key"] else "⚪ Mode Local (Clé Gemini non renseignée)"
st.markdown(
    f"**{api_badge}** | "
    f"💰 Budget max : **{current_criteria['max_budget']:,} €** | "
    f"📐 Surface min : **{current_criteria['min_surface']:g} m²** | "
    f"🏠 Types : **{', '.join(current_criteria['property_types'])}** | "
    f"📍 Secteurs : **{', '.join(current_criteria['cities'])}** | "
    f"🚗 Parking/Garage : **{'Obligatoire' if current_criteria['require_parking'] else 'Optionnel'}**".replace(",", " ")
)

# --- INDICATEURS CLÉS (KPIs) ---
a_visiter_list = [x for x in all_active_listings if x["status"] == "a_visiter"]
a_verifier_list = [x for x in all_active_listings if x["status"] == "a_verifier"]
rejete_list = [x for x in all_active_listings if x["status"] == "rejete"]
pending_list = [x for x in all_active_listings if x["status"] == "pending_analysis"]

col_sel, col1, col2, col3, col4, col5 = st.columns(6)
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
    "🗑️ Quarantaine",
    len(quarantined_listings),
    help="Annonces supprimées / masquées du flux actif",
)

st.divider()

# --- ONGLETS PRINCIPAUX DU COCKPIT ---
tab_cockpit, tab_selection, tab_quarantaine, tab_portails, tab_ajout = st.tabs(
    [
        f"🎯 Annonces du Marché ({len(all_active_listings)})",
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
    if not all_active_listings:
        st.info(
            "👋 Aucune annonce active en base. Cliquez sur **🚀 Lancer la recherche (Live Scraping)** "
            "dans le panneau de gauche pour extraire les annonces en direct depuis Bien'ici et les agences !"
        )
    else:
        f_col1, f_col2, f_col3, f_col4 = st.columns([1.5, 1.5, 1, 1])
        with f_col1:
            default_statuses = ["a_visiter", "a_verifier"]
            if pending_list and not a_visiter_list and not a_verifier_list:
                default_statuses.append("pending_analysis")
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
            available_cities = sorted(list({x["city"] for x in all_active_listings if x.get("city")}))
            city_choice = st.multiselect(
                "Filtrer par ville",
                options=available_cities,
                default=available_cities,
            )
        with f_col3:
            min_score_filter = st.slider("Score IA min", 0, 10, 0)
        with f_col4:
            selection_filter = st.selectbox(
                "Filtre sélection",
                options=["Toutes", "⭐ Sélectionnées uniquement", "Non sélectionnées"],
            )

        filtered = [
            x
            for x in all_active_listings
            if x["status"] in status_choice
            and (not city_choice or x["city"] in city_choice)
            and (x["ai_score"] is None or x["ai_score"] >= min_score_filter)
            and (
                selection_filter == "Toutes"
                or (selection_filter == "⭐ Sélectionnées uniquement" and x.get("is_selected"))
                or (selection_filter == "Non sélectionnées" and not x.get("is_selected"))
            )
        ]

        # --- MODULE D'EXPORTATION EXCEL & CSV ---
        with st.expander("📥 Exporter les données (Excel .xlsx / CSV France)", expanded=False):
            exp_col1, exp_col2 = st.columns(2)
            with exp_col1:
                scope_choice = st.radio(
                    "Périmètre de l'export :",
                    options=["selection_user", "filtres_actifs", "pepites", "tout"],
                    format_func=lambda k: {
                        "selection_user": f"⭐ Annonces sélectionnées pour visites ({len(selected_listings)} annonces)",
                        "filtres_actifs": f"🎯 Filtres actuels ({len(filtered)} annonces)",
                        "pepites": f"💡 Opportunités retenues IA ({len(a_visiter_list) + len(a_verifier_list)} annonces)",
                        "tout": f"📁 Marché actif ({len(all_active_listings)} annonces)",
                    }.get(k, k),
                    horizontal=True,
                )

            if scope_choice == "selection_user":
                export_pool = selected_listings
            elif scope_choice == "filtres_actifs":
                export_pool = filtered
            elif scope_choice == "pepites":
                export_pool = [x for x in all_active_listings if x["status"] in ("a_visiter", "a_verifier")]
            else:
                export_pool = all_active_listings

            if export_pool:
                csv_bytes, excel_bytes = generate_export_files(export_pool)
                timestamp_str = datetime.now().strftime("%Y%m%d_%H%M")
                with exp_col2:
                    st.write("")
                    dl_col1, dl_col2 = st.columns(2)
                    with dl_col1:
                        st.download_button(
                            label="📊 Exporter en Excel (.xlsx)",
                            data=excel_bytes,
                            file_name=f"mandataire_immo_{scope_choice}_{timestamp_str}.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                        )
                    with dl_col2:
                        st.download_button(
                            label="📄 Exporter en CSV (Excel FR)",
                            data=csv_bytes,
                            file_name=f"mandataire_immo_{scope_choice}_{timestamp_str}.csv",
                            mime="text/csv",
                            use_container_width=True,
                        )
            else:
                st.caption("Aucune annonce à exporter pour ce périmètre.")

        st.caption(f"Affichage de **{len(filtered)}** annonce(s) sur {len(all_active_listings)} en base active.")

        for ad in filtered:
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
                    st.markdown(f"### {ad['title']}")
                    st.markdown(
                        f"{badge}{sel_pill} &nbsp;|&nbsp; 📍 **{ad['city']} ({ad['postal_code']})** &nbsp;|&nbsp; "
                        f"🏢 **{ad['source']}** &nbsp;|&nbsp; "
                        f"🏠 **{ad['property_type']}** ({ad.get('rooms', '?')} p.) &nbsp;|&nbsp; "
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
                top_left, top_right = st.columns([4, 1])
                with top_left:
                    st.markdown(f"### ⭐ {ad['title']}")
                    contact_badge = "🟢 Contact pris" if ad.get("contact_made") else "⚪ À contacter"
                    visit_badge = "🗓️ Visite programmée" if ad.get("visit_scheduled") else "⚪ Visite à planifier"
                    st.markdown(
                        f"**{contact_badge}** &nbsp;|&nbsp; **{visit_badge}** &nbsp;|&nbsp; "
                        f"📍 **{ad['city']} ({ad['postal_code']})** &nbsp;|&nbsp; "
                        f"🏠 **{ad['property_type']}** ({ad.get('rooms', '?')} p.) &nbsp;|&nbsp; "
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
# ONGLET 5 : TESTER / IMPORTER UNE ANNONCE MANUELLE
# ==============================================================================
with tab_ajout:
    st.subheader("➕ Analyser une annonce spécifique (Copier-coller)")
    st.write("Vous avez repéré une annonce sur LeBonCoin, SeLoger ou en agence ? Soumettez-la au Cerveau IA :")

    with st.form("manual_ad_form"):
        m_col1, m_col2 = st.columns(2)
        with m_col1:
            m_title = st.text_input("Titre de l'annonce", placeholder="Ex: Maison 95m² avec garage - Wasquehal")
            m_city = st.selectbox(
                "Ville",
                options=current_criteria.get("cities", list(CITY_METADATA.keys())) + ["Autre commune"],
            )
            m_type = st.selectbox("Type de bien", options=["Maison", "Appartement"])
            m_url = st.text_input("URL de l'annonce (optionnel)", placeholder="https://www.leboncoin.fr/...")
        with m_col2:
            m_price = st.number_input("Prix FAI (€)", min_value=10000, max_value=2000000, value=265000, step=1000)
            m_surface = st.number_input("Surface (m²)", min_value=15.0, max_value=500.0, value=90.0, step=1.0)
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
        ad_obj = {
            "external_id": ext_id,
            "source": "Ajout Manuel",
            "url": m_url.strip() or f"https://localhost/manual/{ext_id}",
            "title": m_title.strip(),
            "property_type": m_type,
            "price": int(m_price),
            "surface": float(m_surface),
            "rooms": 4,
            "city": m_city,
            "postal_code": CITY_METADATA.get(m_city, {}).get("postal_code", "59000"),
            "description": m_desc.strip(),
            "dpe": m_dpe,
            "has_parking": m_parking,
        }
        upsert_listing(ad_obj)
        pending = get_pending_listings()
        for p in pending:
            res = analyze_listing(p, current_criteria, api_key=st.session_state["gemini_api_key"])
            update_listing_analysis(p["id"], res)
        st.success("✅ Annonce analysée et ajoutée au tableau de bord !")
        st.rerun()
