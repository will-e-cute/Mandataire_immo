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
| **Orchestration / Scraping** | Python (`httpx` / `BeautifulSoup` / `Playwright`) | Collecte multi-sources (Bien'ici, SeLoger, LeBonCoin) + mode simulation/import d'annonces. |
| **Base de données** | SQLite (`mandataire.db` + `init.sql`) | Stockage relationnel local sans serveur, dédoublonnage par URL/ID et persistance des critères et analyses IA. |
| **Moteur d'Analyse (IA)** | API Gemini (`google-genai` / `gemini-3.8-flash` & `litellm`) | Extraction structurée JSON (Pydantic) : Prix/m², DPE, présence stationnement, détection des "No-Go" et scoring sur 10. |
| **Interface Utilisateur** | Streamlit (`app.py`) | Cockpit interactif avec panneau de configuration des critères, bouton **"Lancer la recherche"** et tableau de bord filtrable. |

---

## 3. Pipeline de Données (Workflow)

1. **Configuration dynamique :** L'utilisateur ajuste ses critères dans l'interface Streamlit (pré-remplis par défaut) et clique sur **"Lancer la recherche"**.
2. **Collecte & Pré-filtrage :** Le scraper interroge les portails ciblés pour les communes sélectionnées, en filtrant selon le budget max et la surface minimale.
3. **Dédoublonnage :** Chaque annonce est vérifiée dans `mandataire.db` via son identifiant unique / URL. Les nouvelles annonces sont insérées avec le statut `pending_analysis`.
4. **Traitement IA (Prompt de Tri) :** Le moteur `ai_analyzer.py` analyse chaque annonce `pending_analysis` avec les critères actifs de l'utilisateur et retourne un JSON validé par schéma.
5. **Restitution Cockpit :** Les annonces sont classées par statut (`À visiter`, `À vérifier`, `Rejeté`) et par score IA (de 0 à 10) avec mise en évidence des points forts et des alertes.

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

## 5. Livrables du Projet

* `.venv/` & `requirements.txt` : Environnement virtuel Python local.
* `init.sql` & `database.py` : Schéma relationnel SQLite et gestionnaire de requêtes / critères.
* `system_prompt.md` & `ai_analyzer.py` : Compétence IA immobilière et intégration Gemini (`gemini-3.8-flash` avec sortie JSON structurée).
* `scraper.py` : Moteur de collecte multi-sources pour les 5 villes cibles + injecteur d'annonces réalistes de test/fallback anti-blocage.
* `app.py` & `run.ps1` : Application Streamlit complète avec contrôle des variables, lancement du pipeline en direct et cockpit de visualisation.
