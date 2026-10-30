# Plan de Développement : Mandataire Immobilier Virtuel (100% Python Local)

**À l'attention de :** Antigravity  
**Sujet :** Architecture et déploiement d'un système automatisé de scraping et de tri immobilier par IA (Résidence Principale - Métropole Lilloise)

---

## 1. Objectif du Projet

Développer une application autonome en local (via `.venv` Python, sans Docker) capable d'automatiser la recherche immobilière pour l'achat d'une **Résidence Principale**. Le système extrait les annonces depuis les plateformes immobilières, les normalise en base locale, et utilise l'API Gemini pour effectuer un tri intelligent basé sur des critères métiers modifiables directement dans l'interface utilisateur.

---

## 2. Architecture Technique Cible (100% Locale & Sans Conteneur)

| Composant | Technologie Retenue | Rôle dans le Système |
| --- | --- | --- |
| **Environnement** | Python 3.13 (`.venv` local) | Isolation complète des dépendances sans Docker. |
| **Orchestration / Scraping** | Python (`httpx` / `BeautifulSoup` / `Playwright`) | Collecte multi-sources (Bien'ici, SeLoger, LeBonCoin) + résilience anti-WAF automatique. |
| **Base de données** | SQLite (`mandataire.db` + `init.sql`) | Stockage relationnel local sans serveur, dédoublonnage par URL/ID, persistance des critères, statuts CRM et quarantaine. |
| **Moteur d'Analyse (IA)** | API Gemini (`gemini-3.8-flash` & fallback local) | Extraction structurée JSON (Pydantic) avec parallélisation multi-threads et circuit-breaker anti-quota. |
| **Interface Utilisateur** | Streamlit (`app.py`) | Cockpit interactif avec gestion de sélection, suivi d'agences/visites, quarantaine restaurable et exports Excel/CSV. |

---

## 3. Pipeline de Données (Workflow)

1. **Configuration dynamique :** L'utilisateur ajuste ses critères dans l'interface Streamlit (pré-remplis par défaut) et clique sur **"Lancer la recherche"**.
2. **Collecte & Pré-filtrage :** Le scraper interroge les portails ciblés pour les communes sélectionnées, en filtrant selon le budget max et la surface minimale (avec fallback Playwright si blocage WAF / DataDome).
3. **Dédoublonnage :** Chaque annonce est vérifiée dans `mandataire.db` via son identifiant unique / URL. Les nouvelles annonces sont insérées avec le statut `pending_analysis`.
4. **Traitement IA Parallélisé :** Le moteur `ai_analyzer.py` analyse en multi-threads les annonces avec les critères actifs et sauvegarde atomiquement chaque résultat en base.
5. **Gestion & Suivi CRM :**
   - **Sélection personnalisée :** Fiche dédiée avec suivi des contacts (`Contact oui/non`), programmation des visites (`Visite programmée oui/non`), nom de l'agence et téléphone (pré-remplissage automatique depuis la source et description).
   - **Mise en Quarantaine :** Suppression réversible masquant les annonces indésirables du flux principal, avec onglet dédié permettant la restauration ou la purge définitive.
6. **Restitution & Export :** Les annonces sont classées par statut et score IA avec export ciblé (Sélectionnées, Filtres, Pépites, Tout) en Excel (`.xlsx`) ou CSV France.

---

## 4. Spécifications du "Cerveau" IA & Critères par Défaut

### A. Paramètres par défaut (Modifiables dans l'UI Streamlit)
* **Objectif :** Résidence principale (confort, quartier, extérieur, coup de cœur)
* **Budget maximum :** `280 000 €` FAI
* **Surface minimale :** `80 m²`
* **Type de bien :** `Maison` et `Appartement` (Les deux)
* **Localisation (Secteur Métropole Lilloise) :** `Croix (59170)`, `Wasquehal (59290)`, `Roubaix (59100)`, `Villeneuve-d'Ascq (59491 / 59650)`, `Marcq-en-Barœul (59700)`
* **Exigence obligatoire :** **Parking, box ou garage inclus obligatoire**

### B. Critères Rédhibitoires ("No-Go" stricts -> Rejet automatique / Score = 0)
1. **Absence de stationnement :** Aucun parking, garage ou box mentionné (lorsque l'option "Parking/Garage obligatoire" est activée).
2. **Passoire thermique :** DPE **F** ou **G**.
3. **Occupation :** Bien vendu loué, occupé, en viager ou nue-propriété.
4. **Gros œuvre à prévoir :** Toiture, façade, structure, fissures, humidité, ou rénovation lourde complète dépassant l'enveloppe.
5. **Copropriété en difficulté :** Procédure en cours, syndic provisoire ou charges excessives.

---

## 5. Livrables et Améliorations Implémentées

* `.venv/` & `requirements.txt` : Environnement local incluant `openpyxl`, `playwright`, `httpx`, `pandas`, `streamlit`.
* `init.sql` & `database.py` :
  - Schéma SQLite relationnel avec migration dynamique au démarrage.
  - Champs CRM et suivi (`is_selected`, `is_quarantined`, `contact_made`, `visit_scheduled`, `agency_name`, `agency_phone`, `followup_notes`).
  - Dédoublonnage robuste à l'insertion et préservation des annotations utilisateur lors des réanalyses.
* `system_prompt.md` & `ai_analyzer.py` :
  - **Parallélisation multi-threads** (`analyze_listings_parallel`) avec `ThreadPoolExecutor` (1 à 8 workers configurables).
  - **Circuit-breaker anti-quota** : bascule automatique et instantanée sur le moteur déterministe local en cas de saturation de quota Gemini (429).
* `scraper.py` :
  - **Bypass WAF & Anti-bot** : rotation de User-Agents récents et headers navigateurs réalistes.
  - **Retries avec backoff exponentiel** sur requêtes HTTP `httpx`.
  - **Bascule Playwright automatique** : utilisation de Chromium headless pour contourner les challenges DataDome / Cloudflare sur SeLoger, Bien'ici et Orpi.
* `app.py` & `run.ps1` :
  - **Gestion de la Sélection :** Onglet dédié avec indicateurs clés (contacts pris, visites programmées, budget moyen), formulaires interactifs de suivi agence et bouton d'appel direct.
  - **Page Quarantaine :** Suppression douce masquant l'annonce du flux du marché, avec options de restauration unitaire ou globale, et purge définitive.
  - **Exportation binaire double format enrichie** : Export Excel (`.xlsx`) et CSV France intégrant le statut de sélection et les informations agence/visite.
