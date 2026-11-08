import json
import os
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional
from scraper import _canonical_city_name

SEED_DB_PATH = Path(__file__).parent / "mandataire.db"
INIT_SQL_PATH = Path(__file__).parent / "init.sql"

# Sur Streamlit Cloud (/mount/src), utiliser une copie de travail dans /tmp pour ne jamais
# salir le fichier mandataire.db suivi par Git (ce qui bloquerait les git pull automatiques de Streamlit Cloud)
IS_CLOUD_ENV = Path("/mount/src").exists() or bool(os.getenv("STREAMLIT_RUNTIME_ENV"))
DB_PATH = Path("/tmp/mandataire_cloud_runtime.db") if IS_CLOUD_ENV else SEED_DB_PATH
_LAST_SYNCED_SEED_MTIME: Optional[float] = None


def _ensure_cloud_db_synced() -> None:
    """
    Sur Streamlit Cloud, initialise la BDD de travail depuis mandataire.db (seed Git)
    et fusionne automatiquement les nouvelles annonces ou mises à jour des scrapers
    si mandataire.db a été mis à jour par un git push/pull, sans écraser le suivi CRM.
    """
    global _LAST_SYNCED_SEED_MTIME
    if not IS_CLOUD_ENV or not SEED_DB_PATH.exists():
        return

    seed_mtime = SEED_DB_PATH.stat().st_mtime
    if not DB_PATH.exists():
        shutil.copy2(SEED_DB_PATH, DB_PATH)
        _LAST_SYNCED_SEED_MTIME = seed_mtime
        return

    if _LAST_SYNCED_SEED_MTIME is not None and seed_mtime <= _LAST_SYNCED_SEED_MTIME:
        return

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("ATTACH DATABASE ? AS seed_db", (str(SEED_DB_PATH),))
            # Fusionner les nouvelles annonces du seed et mettre à jour les métadonnées scrapées des existantes
            conn.execute(
                """
                UPDATE listings
                SET source = COALESCE((SELECT s.source FROM seed_db.listings s WHERE s.external_id = listings.external_id), source),
                    title = COALESCE((SELECT s.title FROM seed_db.listings s WHERE s.external_id = listings.external_id), title),
                    city = COALESCE((SELECT s.city FROM seed_db.listings s WHERE s.external_id = listings.external_id), city),
                    postal_code = COALESCE((SELECT s.postal_code FROM seed_db.listings s WHERE s.external_id = listings.external_id), postal_code),
                    description = CASE
                        WHEN length(COALESCE((SELECT s.description FROM seed_db.listings s WHERE s.external_id = listings.external_id), '')) > length(COALESCE(listings.description, ''))
                        THEN (SELECT s.description FROM seed_db.listings s WHERE s.external_id = listings.external_id)
                        ELSE listings.description
                    END,
                    dpe = CASE
                        WHEN listings.dpe IS NULL OR listings.dpe = 'Non renseigné'
                        THEN COALESCE((SELECT s.dpe FROM seed_db.listings s WHERE s.external_id = listings.external_id), listings.dpe)
                        ELSE listings.dpe
                    END,
                    has_parking = COALESCE((SELECT s.has_parking FROM seed_db.listings s WHERE s.external_id = listings.external_id), listings.has_parking)
                WHERE EXISTS (SELECT 1 FROM seed_db.listings s WHERE s.external_id = listings.external_id)
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO listings (
                    external_id, source, url, title, property_type,
                    price, surface, rooms, city, postal_code,
                    description, dpe, has_parking, raw_json,
                    status, ai_score, price_per_m2, ai_summary,
                    ai_rejection_reason, ai_pros, ai_cons, ai_signals,
                    ai_analysis_json,
                    is_selected, is_quarantined, contact_made, visit_scheduled,
                    agency_name, agency_phone, followup_notes,
                    selected_at, quarantined_at, scraped_at, analyzed_at
                )
                SELECT
                    external_id, source, url, title, property_type,
                    price, surface, rooms, city, postal_code,
                    description, dpe, has_parking, raw_json,
                    status, ai_score, price_per_m2, ai_summary,
                    ai_rejection_reason, ai_pros, ai_cons, ai_signals,
                    ai_analysis_json,
                    is_selected, is_quarantined, contact_made, visit_scheduled,
                    agency_name, agency_phone, followup_notes,
                    selected_at, quarantined_at, scraped_at, analyzed_at
                FROM seed_db.listings
                """
            )
            conn.commit()
            conn.execute("DETACH DATABASE seed_db")
        _LAST_SYNCED_SEED_MTIME = seed_mtime
    except Exception:
        _LAST_SYNCED_SEED_MTIME = seed_mtime


def get_connection() -> sqlite3.Connection:
    _ensure_cloud_db_synced()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialise la base de données SQLite et applique les migrations de schéma si nécessaire."""
    with get_connection() as conn:
        # Vérifier si la table listings existe déjà pour appliquer les migrations avant la création des index
        has_listings = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='listings'"
        ).fetchone()[0] > 0

        if has_listings:
            cursor = conn.execute("PRAGMA table_info(listings)")
            existing_cols = {row["name"] for row in cursor.fetchall()}

            column_definitions = [
                ("is_selected", "INTEGER NOT NULL DEFAULT 0"),
                ("is_quarantined", "INTEGER NOT NULL DEFAULT 0"),
                ("contact_made", "INTEGER NOT NULL DEFAULT 0"),
                ("visit_scheduled", "INTEGER NOT NULL DEFAULT 0"),
                ("agency_name", "TEXT DEFAULT ''"),
                ("agency_phone", "TEXT DEFAULT ''"),
                ("followup_notes", "TEXT DEFAULT ''"),
                ("selected_at", "TIMESTAMP DEFAULT NULL"),
                ("quarantined_at", "TIMESTAMP DEFAULT NULL"),
            ]

            for col_name, col_def in column_definitions:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE listings ADD COLUMN {col_name} {col_def}")
            conn.commit()

        if INIT_SQL_PATH.exists():
            sql_script = INIT_SQL_PATH.read_text(encoding="utf-8")
            conn.executescript(sql_script)

        conn.commit()


def get_criteria() -> Dict[str, Any]:
    """Récupère les critères de recherche actuels (ou les valeurs par défaut)."""
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM search_criteria WHERE id = 1").fetchone()
        if not row:
            return {
                "max_budget": 280000,
                "min_surface": 80.0,
                "property_types": ["Maison", "Appartement"],
                "cities": ["Croix", "Wasquehal", "Roubaix", "Villeneuve-d'Ascq", "Marcq-en-Barœul"],
                "require_parking": True,
                "exclude_dpe_fg": True,
                "exclude_occupied": True,
                "exclude_heavy_works": True,
                "exclude_copro_issues": True,
                "custom_notes": "",
            }
        data = dict(row)
        data["property_types"] = [t.strip() for t in data["property_types"].split(",") if t.strip()]
        raw_cities = [c.strip() for c in data["cities"].split(",") if c.strip()]
        data["cities"] = [_canonical_city_name(c) for c in raw_cities]
        data["require_parking"] = bool(data["require_parking"])
        data["exclude_dpe_fg"] = bool(data["exclude_dpe_fg"])
        data["exclude_occupied"] = bool(data["exclude_occupied"])
        data["exclude_heavy_works"] = bool(data["exclude_heavy_works"])
        data["exclude_copro_issues"] = bool(data["exclude_copro_issues"])
        return data


def save_criteria(
    max_budget: int,
    min_surface: float,
    property_types: List[str],
    cities: List[str],
    require_parking: bool = True,
    exclude_dpe_fg: bool = True,
    exclude_occupied: bool = True,
    exclude_heavy_works: bool = True,
    exclude_copro_issues: bool = True,
    custom_notes: str = "",
) -> None:
    """Sauvegarde les critères modifiés depuis l'interface Streamlit."""
    init_db()
    normalized_cities = []
    for c in cities:
        canon = _canonical_city_name(c)
        if canon and canon not in normalized_cities:
            normalized_cities.append(canon)
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE search_criteria
            SET max_budget = ?,
                min_surface = ?,
                property_types = ?,
                cities = ?,
                require_parking = ?,
                exclude_dpe_fg = ?,
                exclude_occupied = ?,
                exclude_heavy_works = ?,
                exclude_copro_issues = ?,
                custom_notes = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = 1
            """,
            (
                int(max_budget),
                float(min_surface),
                ",".join(property_types),
                ",".join(normalized_cities),
                int(require_parking),
                int(exclude_dpe_fg),
                int(exclude_occupied),
                int(exclude_heavy_works),
                int(exclude_copro_issues),
                custom_notes.strip(),
            ),
        )
        conn.commit()


def upsert_listing(listing: Dict[str, Any]) -> bool:
    """
    Insère une nouvelle annonce si son external_id ou son url n'existe pas encore,
    ou enrichit/met à jour ses métadonnées scrapées (DPE, parking, description, ville, prix)
    si elle existe déjà en préservant le tri IA et le suivi CRM.
    Retourne True si l'annonce est nouvelle, False si elle existait déjà (et a été rafraîchie).
    """
    init_db()
    price = int(listing.get("price", 0))
    surface = float(listing.get("surface", 0.0))
    price_per_m2 = round(price / surface, 2) if surface > 0 else 0.0
    canon_city = _canonical_city_name(str(listing.get("city", "")))
    new_dpe = str(listing.get("dpe") or "Non renseigné").strip()
    new_desc = str(listing.get("description") or "")
    new_parking = None if listing.get("has_parking") is None else int(bool(listing.get("has_parking")))

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id, description, dpe, has_parking FROM listings WHERE external_id = ? OR url = ?",
            (listing["external_id"], listing["url"]),
        ).fetchone()
        if existing:
            old_desc = str(existing["description"] or "")
            best_desc = new_desc if len(new_desc) >= len(old_desc) else old_desc
            old_dpe = str(existing["dpe"] or "Non renseigné")
            best_dpe = new_dpe if new_dpe in ("A", "B", "C", "D", "E", "F", "G") else old_dpe
            best_parking = new_parking if new_parking is not None else existing["has_parking"]

            conn.execute(
                """
                UPDATE listings
                SET source = ?,
                    title = ?,
                    property_type = ?,
                    price = ?,
                    surface = ?,
                    rooms = ?,
                    city = ?,
                    postal_code = ?,
                    description = ?,
                    dpe = ?,
                    has_parking = ?,
                    price_per_m2 = ?,
                    raw_json = ?
                WHERE id = ?
                """,
                (
                    listing.get("source", "Manuel"),
                    listing["title"],
                    listing.get("property_type", "Autre"),
                    price,
                    surface,
                    int(listing.get("rooms", 0)),
                    canon_city,
                    listing.get("postal_code", ""),
                    best_desc,
                    best_dpe,
                    best_parking,
                    price_per_m2,
                    json.dumps(listing, ensure_ascii=False),
                    existing["id"],
                ),
            )
            conn.commit()
            return False

        conn.execute(
            """
            INSERT INTO listings (
                external_id, source, url, title, property_type,
                price, surface, rooms, city, postal_code,
                description, dpe, has_parking, price_per_m2, raw_json, status,
                is_selected, is_quarantined
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_analysis', 0, 0)
            """,
            (
                listing["external_id"],
                listing.get("source", "Manuel"),
                listing["url"],
                listing["title"],
                listing.get("property_type", "Autre"),
                price,
                surface,
                int(listing.get("rooms", 0)),
                canon_city,
                listing.get("postal_code", ""),
                new_desc,
                new_dpe,
                new_parking,
                price_per_m2,
                json.dumps(listing, ensure_ascii=False),
            ),
        )
        conn.commit()
        return True


def get_pending_listings() -> List[Dict[str, Any]]:
    """Récupère toutes les annonces en attente d'analyse IA (hors annonces en quarantaine)."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE status = 'pending_analysis' AND is_quarantined = 0
            ORDER BY scraped_at DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def reset_all_listings_to_pending() -> int:
    """Permet de relancer l'analyse IA sur toutes les annonces actives en base (hors quarantaine)."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute("UPDATE listings SET status = 'pending_analysis' WHERE is_quarantined = 0")
        conn.commit()
        return cur.rowcount


def update_listing_analysis(listing_id: int, analysis: Dict[str, Any]) -> None:
    """Met à jour une annonce avec le résultat structuré du modèle IA."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE listings
            SET status = ?,
                ai_score = ?,
                price_per_m2 = COALESCE(?, price_per_m2),
                dpe = COALESCE(?, dpe),
                has_parking = ?,
                ai_summary = ?,
                ai_rejection_reason = ?,
                ai_pros = ?,
                ai_cons = ?,
                ai_signals = ?,
                ai_analysis_json = ?,
                analyzed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                analysis.get("statut", "a_verifier"),
                int(analysis.get("score", 5)),
                analysis.get("prix_m2"),
                analysis.get("dpe_detecte"),
                None if analysis.get("parking_detecte") is None else int(bool(analysis.get("parking_detecte"))),
                analysis.get("resume", ""),
                analysis.get("raison_rejet"),
                json.dumps(analysis.get("points_forts", []), ensure_ascii=False),
                json.dumps(analysis.get("points_vigilance", []), ensure_ascii=False),
                json.dumps(analysis.get("signaux_faibles", []), ensure_ascii=False),
                json.dumps(analysis, ensure_ascii=False),
                listing_id,
            ),
        )
        conn.commit()


def _format_listing_rows(rows: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    results = []
    for r in rows:
        item = dict(r)
        for list_field in ("ai_pros", "ai_cons", "ai_signals"):
            try:
                item[list_field] = json.loads(item.get(list_field) or "[]")
            except Exception:
                item[list_field] = []
        item["is_selected"] = bool(item.get("is_selected", 0))
        item["is_quarantined"] = bool(item.get("is_quarantined", 0))
        item["contact_made"] = bool(item.get("contact_made", 0))
        item["visit_scheduled"] = bool(item.get("visit_scheduled", 0))
        results.append(item)
    return results


def get_all_listings(
    status_filter: Optional[List[str]] = None,
    include_quarantined: bool = False,
    only_selected: bool = False,
) -> List[Dict[str, Any]]:
    """Récupère les annonces filtrables pour affichage dans Streamlit."""
    init_db()
    with get_connection() as conn:
        conditions = []
        params: List[Any] = []

        if not include_quarantined:
            conditions.append("is_quarantined = 0")

        if only_selected:
            conditions.append("is_selected = 1")

        if status_filter:
            placeholders = ",".join("?" for _ in status_filter)
            conditions.append(f"status IN ({placeholders})")
            params.extend(status_filter)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""
            SELECT * FROM listings
            {where_clause}
            ORDER BY is_selected DESC, COALESCE(ai_score, -1) DESC, scraped_at DESC
        """
        rows = conn.execute(query, params).fetchall()
        return _format_listing_rows(rows)


def get_selected_listings() -> List[Dict[str, Any]]:
    """Récupère toutes les annonces sélectionnées pour le suivi (hors quarantaine)."""
    init_db()
    with get_connection() as conn:
        query = """
            SELECT * FROM listings
            WHERE is_selected = 1 AND is_quarantined = 0
            ORDER BY COALESCE(selected_at, scraped_at) DESC, COALESCE(ai_score, -1) DESC
        """
        rows = conn.execute(query).fetchall()
        return _format_listing_rows(rows)


def get_quarantined_listings() -> List[Dict[str, Any]]:
    """Récupère toutes les annonces mises en quarantaine."""
    init_db()
    with get_connection() as conn:
        query = """
            SELECT * FROM listings
            WHERE is_quarantined = 1
            ORDER BY COALESCE(quarantined_at, scraped_at) DESC
        """
        rows = conn.execute(query).fetchall()
        return _format_listing_rows(rows)


def set_listing_selection(
    listing_id: int,
    is_selected: bool,
    default_agency_name: str = "",
    default_phone: str = "",
) -> None:
    """Marque une annonce comme sélectionnée (ou la retire de la sélection)."""
    init_db()
    with get_connection() as conn:
        if is_selected:
            conn.execute(
                """
                UPDATE listings
                SET is_selected = 1,
                    is_quarantined = 0,
                    selected_at = CURRENT_TIMESTAMP,
                    agency_name = CASE
                        WHEN (agency_name IS NULL OR TRIM(agency_name) = '') THEN ?
                        ELSE agency_name
                    END,
                    agency_phone = CASE
                        WHEN (agency_phone IS NULL OR TRIM(agency_phone) = '') THEN ?
                        ELSE agency_phone
                    END
                WHERE id = ?
                """,
                (default_agency_name.strip(), default_phone.strip(), listing_id),
            )
        else:
            conn.execute(
                "UPDATE listings SET is_selected = 0 WHERE id = ?",
                (listing_id,),
            )
        conn.commit()


def set_listing_quarantine(listing_id: int, is_quarantined: bool) -> None:
    """Met une annonce en quarantaine (ou la restaure). Désélectionne si mise en quarantaine."""
    init_db()
    with get_connection() as conn:
        if is_quarantined:
            conn.execute(
                """
                UPDATE listings
                SET is_quarantined = 1,
                    is_selected = 0,
                    quarantined_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (listing_id,),
            )
        else:
            conn.execute(
                """
                UPDATE listings
                SET is_quarantined = 0
                WHERE id = ?
                """,
                (listing_id,),
            )
        conn.commit()


def update_listing_followup(
    listing_id: int,
    contact_made: bool,
    visit_scheduled: bool,
    agency_name: str,
    agency_phone: str,
    followup_notes: str = "",
) -> None:
    """Met à jour les informations de suivi CRM d'une annonce sélectionnée."""
    init_db()
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE listings
            SET contact_made = ?,
                visit_scheduled = ?,
                agency_name = ?,
                agency_phone = ?,
                followup_notes = ?
            WHERE id = ?
            """,
            (
                int(contact_made),
                int(visit_scheduled),
                agency_name.strip(),
                agency_phone.strip(),
                followup_notes.strip(),
                listing_id,
            ),
        )
        conn.commit()


def delete_listing_permanently(listing_id: int) -> None:
    """Supprime définitivement une annonce de la base SQLite."""
    init_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
        conn.commit()


def empty_quarantine() -> int:
    """Supprime définitivement toutes les annonces en quarantaine."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM listings WHERE is_quarantined = 1")
        conn.commit()
        return cur.rowcount


def restore_all_quarantine() -> int:
    """Restaure toutes les annonces de la quarantaine dans le flux actif."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute("UPDATE listings SET is_quarantined = 0 WHERE is_quarantined = 1")
        conn.commit()
        return cur.rowcount


def delete_all_listings() -> None:
    """Vide intégralement la table des annonces."""
    init_db()
    with get_connection() as conn:
        conn.execute("DELETE FROM listings")
        conn.commit()


def export_db_to_json() -> str:
    """Exporte les critères et toutes les annonces (avec suivi CRM et analyses IA) au format JSON."""
    init_db()
    with get_connection() as conn:
        criteria_row = conn.execute("SELECT * FROM search_criteria WHERE id = 1").fetchone()
        listings_rows = conn.execute("SELECT * FROM listings ORDER BY id ASC").fetchall()
        payload = {
            "search_criteria": dict(criteria_row) if criteria_row else {},
            "listings": [dict(r) for r in listings_rows],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


def import_db_from_json(json_str: str) -> int:
    """Restaure les critères, annonces et suivi IA/CRM depuis un export JSON en préservant les enrichissements des nouveaux scrapers."""
    init_db()
    data = json.loads(json_str)

    # Restauration éventuelle des critères avec normalisation orthographique des villes
    criteria = data.get("search_criteria")
    if criteria and isinstance(criteria, dict):
        raw_cities_val = criteria.get("cities")
        if isinstance(raw_cities_val, str):
            raw_list = [c.strip() for c in raw_cities_val.split(",") if c.strip()]
        elif isinstance(raw_cities_val, list):
            raw_list = [str(c).strip() for c in raw_cities_val if str(c).strip()]
        else:
            raw_list = []
        canon_cities_str = ",".join(_canonical_city_name(c) for c in raw_list) if raw_list else None

        with get_connection() as conn:
            conn.execute(
                """
                UPDATE search_criteria
                SET max_budget = COALESCE(?, max_budget),
                    min_surface = COALESCE(?, min_surface),
                    property_types = COALESCE(?, property_types),
                    cities = COALESCE(?, cities),
                    require_parking = COALESCE(?, require_parking),
                    exclude_dpe_fg = COALESCE(?, exclude_dpe_fg),
                    exclude_occupied = COALESCE(?, exclude_occupied),
                    exclude_heavy_works = COALESCE(?, exclude_heavy_works),
                    exclude_copro_issues = COALESCE(?, exclude_copro_issues),
                    custom_notes = COALESCE(?, custom_notes),
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """,
                (
                    criteria.get("max_budget"),
                    criteria.get("min_surface"),
                    criteria.get("property_types") if isinstance(criteria.get("property_types"), str) else ",".join(criteria.get("property_types", [])),
                    canon_cities_str,
                    criteria.get("require_parking"),
                    criteria.get("exclude_dpe_fg"),
                    criteria.get("exclude_occupied"),
                    criteria.get("exclude_heavy_works"),
                    criteria.get("exclude_copro_issues"),
                    criteria.get("custom_notes"),
                ),
            )
            conn.commit()

    listings = data.get("listings", [])
    restored = 0
    with get_connection() as conn:
        for item in listings:
            canon_city = _canonical_city_name(str(item.get("city", "")))
            existing = conn.execute(
                "SELECT description, dpe, has_parking FROM listings WHERE external_id = ?",
                (item["external_id"],),
            ).fetchone()

            imp_desc = str(item.get("description") or "")
            imp_dpe = str(item.get("dpe") or "Non renseigné")
            imp_parking = item.get("has_parking")

            if existing:
                ex_desc = str(existing["description"] or "")
                if len(ex_desc) > len(imp_desc):
                    imp_desc = ex_desc
                ex_dpe = str(existing["dpe"] or "Non renseigné")
                if imp_dpe not in ("A", "B", "C", "D", "E", "F", "G") and ex_dpe in ("A", "B", "C", "D", "E", "F", "G"):
                    imp_dpe = ex_dpe
                if imp_parking is None and existing["has_parking"] is not None:
                    imp_parking = existing["has_parking"]

            conn.execute(
                """
                INSERT OR REPLACE INTO listings (
                    external_id, source, url, title, property_type,
                    price, surface, rooms, city, postal_code,
                    description, dpe, has_parking, raw_json,
                    status, ai_score, price_per_m2, ai_summary,
                    ai_rejection_reason, ai_pros, ai_cons, ai_signals,
                    ai_analysis_json,
                    is_selected, is_quarantined, contact_made, visit_scheduled,
                    agency_name, agency_phone, followup_notes,
                    selected_at, quarantined_at, scraped_at, analyzed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["external_id"],
                    item.get("source", "Import"),
                    item["url"],
                    item["title"],
                    item.get("property_type", "Autre"),
                    int(item.get("price", 0)),
                    float(item.get("surface", 0.0)),
                    int(item.get("rooms", 0)),
                    canon_city,
                    item.get("postal_code", ""),
                    imp_desc,
                    imp_dpe,
                    imp_parking,
                    item.get("raw_json", "{}"),
                    item.get("status", "pending_analysis"),
                    item.get("ai_score"),
                    item.get("price_per_m2"),
                    item.get("ai_summary"),
                    item.get("ai_rejection_reason"),
                    item.get("ai_pros", "[]") if isinstance(item.get("ai_pros"), str) else json.dumps(item.get("ai_pros", []), ensure_ascii=False),
                    item.get("ai_cons", "[]") if isinstance(item.get("ai_cons"), str) else json.dumps(item.get("ai_cons", []), ensure_ascii=False),
                    item.get("ai_signals", "[]") if isinstance(item.get("ai_signals"), str) else json.dumps(item.get("ai_signals", []), ensure_ascii=False),
                    item.get("ai_analysis_json", "{}"),
                    int(bool(item.get("is_selected", 0))),
                    int(bool(item.get("is_quarantined", 0))),
                    int(bool(item.get("contact_made", 0))),
                    int(bool(item.get("visit_scheduled", 0))),
                    item.get("agency_name", "") or "",
                    item.get("agency_phone", "") or "",
                    item.get("followup_notes", "") or "",
                    item.get("selected_at"),
                    item.get("quarantined_at"),
                    item.get("scraped_at"),
                    item.get("analyzed_at"),
                ),
            )
            restored += 1
        conn.commit()
    return restored
