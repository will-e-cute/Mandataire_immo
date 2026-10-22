import hashlib
import os
from pathlib import Path
import streamlit as st
from dotenv import load_dotenv, set_key

from database import (
    delete_all_listings,
    get_all_listings,
    get_criteria,
    get_pending_listings,
    init_db,
    reset_all_listings_to_pending,
    save_criteria,
    update_listing_analysis,
    upsert_listing,
)
from ai_analyzer import analyze_listing, test_gemini_api_key
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

# Initialisation de la base SQLite locale
init_db()
current_criteria = get_criteria()

if "gemini_api_key" not in st.session_state:
    st.session_state["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "")

# --- BARRE LATÉRALE : CLÉ API GEMINI (AVEC BOUTON TEST) & CRITÈRES DE RECHERCHE ---
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

    if st.button("🗑️ Vider la base d'annonces", use_container_width=True):
        delete_all_listings()
        st.toast("Base de données vidée.", icon="🗑️")
        st.rerun()


# --- ACTION : LANCEMENT DU PIPELINE DE RECHERCHE LIVE & ANALYSE IA ---
if launch_search:
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

    with st.status("🔍 Scraping temps réel (SeLoger + Bien'ici + Agences & Orpi) et Tri IA...", expanded=True) as status_box:
        st.write(
            f"1️⃣ Interrogation en direct de **SeLoger.com**, **Bien'ici** (IAD, Safti, Citya, Square Habitat, Guy Hoquet, ERA, Sergic...) "
            f"et **Orpi** sur : **{', '.join(parsed_cities)}**..."
        )
        scrape_report = scrape_all_live_sources(current_criteria)
        all_fetched = scrape_report["ads"]
        stats = scrape_report["stats"]

        new_count = 0
        for ad in all_fetched:
            if upsert_listing(ad):
                new_count += 1

        st.write(
            f"📥 Collecte Live terminée : **{stats['total']}** annonces réelles récupérées "
            f"({stats.get('seloger', 0)} via SeLoger, {stats['bienici_agences']} via Bien'ici/Agences, {stats['orpi_direct']} via Orpi direct) — "
            f"**{new_count}** nouvelle(s) insérée(s) en base."
        )

        if reanalyze_existing:
            reset_all_listings_to_pending()

        pending = get_pending_listings()
        active_key = st.session_state["gemini_api_key"]
        engine_label = "API Gemini (gemini-3.8-flash)" if active_key else "Moteur de règles local (sans clé API)"
        st.write(f"🧠 Analyse et tri via **{engine_label}** sur **{len(pending)}** annonce(s)...")

        progress = st.progress(0)
        for idx, item in enumerate(pending):
            analysis = analyze_listing(item, current_criteria, api_key=active_key)
            update_listing_analysis(item["id"], analysis)
            progress.progress((idx + 1) / max(1, len(pending)))

        status_box.update(
            label=f"✅ Recherche Live et tri terminés ({len(pending)} annonces réelles analysées) !",
            state="complete",
            expanded=False,
        )


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

all_listings = get_all_listings()

if not all_listings:
    st.info(
        "👋 Aucune annonce en base. Cliquez sur **🚀 Lancer la recherche (Live Scraping)** dans le panneau de gauche pour extraire les vraies annonces en direct depuis Bien'ici et les réseaux d'agences !"
    )

# --- INDICATEURS CLÉS (KPIs) ---
a_visiter_list = [x for x in all_listings if x["status"] == "a_visiter"]
a_verifier_list = [x for x in all_listings if x["status"] == "a_verifier"]
rejete_list = [x for x in all_listings if x["status"] == "rejete"]

col1, col2, col3, col4 = st.columns(4)
col1.metric("🟢 À Visiter (Validées IA)", len(a_visiter_list))
col2.metric("🟡 À Vérifier (À confirmer)", len(a_verifier_list))
col3.metric("🔴 Ignorées / Rejetées (No-Go)", len(rejete_list))
if a_visiter_list:
    avg_m2 = sum(x["price_per_m2"] or 0 for x in a_visiter_list) / len(a_visiter_list)
    col4.metric("💶 Prix/m² moyen (À visiter)", f"{avg_m2:,.0f} €/m²".replace(",", " "))
else:
    col4.metric("💶 Prix/m² moyen (À visiter)", "—")

st.divider()

# --- ONGLETS DU COCKPIT ---
tab_cockpit, tab_portails, tab_ajout = st.tabs(
    [
        f"🎯 Annonces Réelles Triées ({len(all_listings)})",
        "🌐 Liens Directs Portails (Bien'ici / Orpi / LeBonCoin / SeLoger)",
        "➕ Tester / Importer une Annonce",
    ]
)

with tab_cockpit:
    if all_listings:
        f_col1, f_col2, f_col3 = st.columns([2, 2, 1])
        with f_col1:
            status_choice = st.multiselect(
                "Filtrer par décision IA",
                options=["a_visiter", "a_verifier", "rejete"],
                default=["a_visiter", "a_verifier"],
                format_func=lambda s: {
                    "a_visiter": "🟢 À visiter (Opportunités)",
                    "a_verifier": "🟡 À vérifier (Infos manquantes)",
                    "rejete": "🔴 Rejeté (Critères No-Go)",
                }.get(s, s),
            )
        with f_col2:
            available_cities = sorted(list({x["city"] for x in all_listings if x.get("city")}))
            city_choice = st.multiselect(
                "Filtrer par ville",
                options=available_cities,
                default=available_cities,
            )
        with f_col3:
            min_score_filter = st.slider("Score IA minimum", 0, 10, 0)

        filtered = [
            x
            for x in all_listings
            if x["status"] in status_choice
            and (not city_choice or x["city"] in city_choice)
            and (x["ai_score"] is None or x["ai_score"] >= min_score_filter)
        ]

        st.caption(f"Affichage de **{len(filtered)}** annonce(s) réelle(s) sur {len(all_listings)} en base.")

        for ad in filtered:
            score = ad.get("ai_score") or 0
            status = ad.get("status")
            if status == "a_visiter":
                badge = f"🟢 **À VISITER** — Score IA : **{score}/10**"
            elif status == "a_verifier":
                badge = f"🟡 **À VÉRIFIER** — Score IA : **{score}/10**"
            else:
                badge = f"🔴 **REJETÉ** — Score IA : **{score}/10**"

            with st.container(border=True):
                top_left, top_right = st.columns([4, 1])
                with top_left:
                    st.markdown(f"### {ad['title']}")
                    st.markdown(
                        f"{badge} &nbsp;|&nbsp; 📍 **{ad['city']} ({ad['postal_code']})** &nbsp;|&nbsp; "
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

                c_pros, c_cons, c_link = st.columns([2, 2, 1])
                with c_pros:
                    if ad.get("ai_pros"):
                        st.markdown("**✅ Points forts :**")
                        for p in ad["ai_pros"]:
                            st.markdown(f"- {p}")
                with c_cons:
                    if ad.get("ai_cons") or ad.get("ai_signals"):
                        st.markdown("**⚠️ Vigilance & Signaux :**")
                        for c in ad.get("ai_cons", []) + ad.get("ai_signals", []):
                            st.markdown(f"- {c}")
                with c_link:
                    st.write("")
                    st.link_button(
                        "🔗 Ouvrir l'annonce réelle",
                        url=ad["url"],
                        use_container_width=True,
                    )

                with st.expander("📄 Lire la description complète de l'annonce"):
                    st.write(ad["description"])

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
