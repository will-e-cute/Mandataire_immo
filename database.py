import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from scraper import _canonical_city_name

DB_PATH = Path(__file__).parent / "mandataire.db"
INIT_SQL_PATH = Path(__file__).parent / "init.sql"
_DB_INITIALIZED = False


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")
    return conn


def init_db(force: bool = False) -> None:
    """Initialise la base de données SQLite et applique les migrations de schéma une seule fois par processus."""
    global _DB_INITIALIZED
    if _DB_INITIALIZED and not force and DB_PATH.exists():
        return

    with get_connection() as conn:
        has_criteria = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='search_criteria'"
        ).fetchone()[0] > 0

        if has_criteria:
            cursor = conn.execute("PRAGMA table_info(search_criteria)")
            existing_crit_cols = {row["name"] for row in cursor.fetchall()}
            crit_column_definitions = [
                ("transaction_type", "TEXT NOT NULL DEFAULT 'buy'"),
                ("min_rooms", "INTEGER NOT NULL DEFAULT 0"),
                ("min_bedrooms", "INTEGER NOT NULL DEFAULT 0"),
            ]
            for col_name, col_def in crit_column_definitions:
                if col_name not in existing_crit_cols:
                    conn.execute(f"ALTER TABLE search_criteria ADD COLUMN {col_name} {col_def}")
            conn.commit()

        has_listings = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='listings'"
        ).fetchone()[0] > 0

        if has_listings:
            cursor = conn.execute("PRAGMA table_info(listings)")
            existing_cols = {row["name"] for row in cursor.fetchall()}

            column_definitions = [
                ("transaction_type", "TEXT DEFAULT 'buy'"),
                ("bedrooms", "INTEGER DEFAULT 0"),
                ("is_selected", "INTEGER NOT NULL DEFAULT 0"),
                ("is_quarantined", "INTEGER NOT NULL DEFAULT 0"),
                ("contact_made", "INTEGER NOT NULL DEFAULT 0"),
                ("visit_scheduled", "INTEGER NOT NULL DEFAULT 0"),
                ("agency_name", "TEXT DEFAULT ''"),
                ("agency_phone", "TEXT DEFAULT ''"),
                ("followup_notes", "TEXT DEFAULT ''"),
                ("selected_at", "TIMESTAMP DEFAULT NULL"),
                ("quarantined_at", "TIMESTAMP DEFAULT NULL"),
                ("is_available", "INTEGER NOT NULL DEFAULT 1"),
                ("unavailable_reason", "TEXT DEFAULT ''"),
                ("availability_checked_at", "TIMESTAMP DEFAULT NULL"),
            ]

            for col_name, col_def in column_definitions:
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE listings ADD COLUMN {col_name} {col_def}")
            conn.commit()

        if INIT_SQL_PATH.exists():
            sql_script = INIT_SQL_PATH.read_text(encoding="utf-8")
            conn.executescript(sql_script)

        conn.commit()
    _DB_INITIALIZED = True


def get_criteria() -> Dict[str, Any]:
    """Récupère les critères de recherche actuels (ou les valeurs par défaut)."""
    init_db()
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM search_criteria WHERE id = 1").fetchone()
        if not row:
            return {
                "transaction_type": "buy",
                "max_budget": 280000,
                "min_surface": 80.0,
                "min_rooms": 0,
                "min_bedrooms": 0,
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
        data["transaction_type"] = str(data.get("transaction_type") or "buy").strip().lower()
        if data["transaction_type"] not in ("buy", "rent"):
            data["transaction_type"] = "buy"
        data["min_rooms"] = int(data.get("min_rooms") or 0)
        data["min_bedrooms"] = int(data.get("min_bedrooms") or 0)
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
    transaction_type: str = "buy",
    min_rooms: int = 0,
    min_bedrooms: int = 0,
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
    tx_type = "rent" if str(transaction_type).strip().lower() in ("rent", "location") else "buy"
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE search_criteria
            SET transaction_type = ?,
                max_budget = ?,
                min_surface = ?,
                min_rooms = ?,
                min_bedrooms = ?,
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
                tx_type,
                int(max_budget),
                float(min_surface),
                int(min_rooms),
                int(min_bedrooms),
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


def _upsert_single_in_conn(conn: sqlite3.Connection, listing: Dict[str, Any]) -> bool:
    """Insère ou met à jour une annonce au sein d'une transaction SQLite ouverte."""
    price = int(listing.get("price", 0))
    surface = float(listing.get("surface", 0.0))
    price_per_m2 = round(price / surface, 2) if surface > 0 else 0.0
    canon_city = _canonical_city_name(str(listing.get("city", "")))
    new_dpe = str(listing.get("dpe") or "Non renseigné").strip()
    new_desc = str(listing.get("description") or "")
    new_parking = None if listing.get("has_parking") is None else int(bool(listing.get("has_parking")))
    tx_type = "rent" if str(listing.get("transaction_type", "buy")).strip().lower() in ("rent", "location") else "buy"
    rooms_val = int(listing.get("rooms", 0) or 0)
    bedrooms_val = int(listing.get("bedrooms", 0) or 0)
    is_avail = 1 if listing.get("is_available", True) else 0
    unavail_reason = "" if is_avail else str(listing.get("unavailable_reason") or "").strip()

    existing = conn.execute(
        "SELECT id, description, dpe, has_parking, bedrooms FROM listings WHERE external_id = ? OR url = ?",
        (listing["external_id"], listing["url"]),
    ).fetchone()

    if existing:
        old_desc = str(existing["description"] or "")
        best_desc = new_desc if len(new_desc) >= len(old_desc) else old_desc
        old_dpe = str(existing["dpe"] or "Non renseigné")
        best_dpe = new_dpe if new_dpe in ("A", "B", "C", "D", "E", "F", "G") else old_dpe
        best_parking = new_parking if new_parking is not None else existing["has_parking"]
        best_bedrooms = bedrooms_val if bedrooms_val > 0 else int(existing["bedrooms"] or 0)

        conn.execute(
            """
            UPDATE listings
            SET source = ?,
                title = ?,
                transaction_type = ?,
                property_type = ?,
                price = ?,
                surface = ?,
                rooms = ?,
                bedrooms = ?,
                city = ?,
                postal_code = ?,
                description = ?,
                dpe = ?,
                has_parking = ?,
                price_per_m2 = ?,
                raw_json = ?,
                is_available = ?,
                unavailable_reason = ?,
                availability_checked_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                listing.get("source", "Manuel"),
                listing["title"],
                tx_type,
                listing.get("property_type", "Autre"),
                price,
                surface,
                rooms_val,
                best_bedrooms,
                canon_city,
                listing.get("postal_code", ""),
                best_desc,
                best_dpe,
                best_parking,
                price_per_m2,
                json.dumps(listing, ensure_ascii=False),
                is_avail,
                unavail_reason,
                existing["id"],
            ),
        )
        return False

    conn.execute(
        """
        INSERT INTO listings (
            external_id, source, url, title, transaction_type, property_type,
            price, surface, rooms, bedrooms, city, postal_code,
            description, dpe, has_parking, price_per_m2, raw_json, status,
            is_selected, is_quarantined, is_available, unavailable_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_analysis', 0, 0, ?, ?)
        """,
        (
            listing["external_id"],
            listing.get("source", "Manuel"),
            listing["url"],
            listing["title"],
            tx_type,
            listing.get("property_type", "Autre"),
            price,
            surface,
            rooms_val,
            bedrooms_val,
            canon_city,
            listing.get("postal_code", ""),
            new_desc,
            new_dpe,
            new_parking,
            price_per_m2,
            json.dumps(listing, ensure_ascii=False),
            is_avail,
            unavail_reason,
        ),
    )
    return True


def upsert_listing(listing: Dict[str, Any]) -> bool:
    """Insère ou met à jour une annonce unitaire."""
    init_db()
    with get_connection() as conn:
        is_new = _upsert_single_in_conn(conn, listing)
        conn.commit()
        return is_new


def upsert_listings_batch(listings: List[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Insère ou met à jour un lot d'annonces en UNE SEULE transaction SQLite atomique (mode WAL).
    Retourne `(new_count, updated_count)`. Gain de vitesse ~50x sur de gros volumes.
    """
    init_db()
    if not listings:
        return 0, 0
    new_count = 0
    updated_count = 0
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for ad in listings:
                if _upsert_single_in_conn(conn, ad):
                    new_count += 1
                else:
                    updated_count += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return new_count, updated_count


def get_existing_orpi_descriptions() -> Dict[str, Tuple[str, str, str, Optional[int]]]:
    """
    Récupère un cache mémoire des annonces Orpi déjà présentes en base (`external_id -> (description, dpe, postal_code, has_parking)`)
    pour éviter de re-télécharger inutilement les fiches détails HTML déjà enrichies.
    """
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT external_id, description, dpe, postal_code, has_parking FROM listings WHERE external_id LIKE 'orpi-%'"
        ).fetchall()
        return {
            str(r["external_id"]): (
                str(r["description"] or ""),
                str(r["dpe"] or "Non renseigné"),
                str(r["postal_code"] or ""),
                r["has_parking"],
            )
            for r in rows
        }


def get_pending_listings() -> List[Dict[str, Any]]:
    """Récupère toutes les annonces disponibles en attente d'analyse IA (hors quarantaine)."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE status = 'pending_analysis' AND is_quarantined = 0 AND is_available = 1
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


def update_listings_analysis_batch(updates: List[Tuple[int, Dict[str, Any]]]) -> None:
    """Met à jour un lot d'analyses IA en une seule transaction SQLite."""
    init_db()
    if not updates:
        return
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
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
                [
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
                    )
                    for listing_id, analysis in updates
                ],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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
        raw_avail = item.get("is_available")
        item["is_available"] = True if raw_avail is None else bool(raw_avail)
        item["unavailable_reason"] = str(item.get("unavailable_reason") or "")
        results.append(item)
    return results


def get_all_listings(
    status_filter: Optional[List[str]] = None,
    include_quarantined: bool = False,
    only_selected: bool = False,
    include_unavailable: bool = False,
) -> List[Dict[str, Any]]:
    """Récupère les annonces filtrables pour affichage dans Streamlit."""
    init_db()
    with get_connection() as conn:
        conditions = []
        params: List[Any] = []

        if not include_quarantined:
            conditions.append("is_quarantined = 0")

        if not include_unavailable:
            conditions.append("is_available = 1")

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


def get_unavailable_listings() -> List[Dict[str, Any]]:
    """Récupère les annonces détectées comme n'étant plus disponibles (hors quarantaine)."""
    init_db()
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM listings
            WHERE is_available = 0 AND is_quarantined = 0
            ORDER BY COALESCE(availability_checked_at, scraped_at) DESC
            """
        ).fetchall()
        return _format_listing_rows(rows)


def mark_listings_availability_batch(updates: List[Tuple[int, bool, str]]) -> Tuple[int, int]:
    """
    Met à jour en une transaction le statut de disponibilité de plusieurs annonces.
    Chaque élément est `(listing_id, is_available, reason)`.
    Retourne `(marked_unavailable, confirmed_available)`.
    """
    init_db()
    if not updates:
        return 0, 0
    marked_unavailable = 0
    confirmed_available = 0
    params = []
    for listing_id, is_avail, reason in updates:
        avail_int = 1 if is_avail else 0
        reason_str = "" if avail_int else str(reason or "").strip()
        if avail_int:
            confirmed_available += 1
        else:
            marked_unavailable += 1
        params.append((avail_int, reason_str, int(listing_id)))

    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """
                UPDATE listings
                SET is_available = ?,
                    unavailable_reason = ?,
                    availability_checked_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                params,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return marked_unavailable, confirmed_available


def quarantine_unavailable_listings() -> int:
    """Place en quarantaine toutes les annonces actives marquées comme indisponibles."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE listings
            SET is_quarantined = 1,
                is_selected = 0,
                quarantined_at = CURRENT_TIMESTAMP
            WHERE is_available = 0 AND is_quarantined = 0
            """
        )
        conn.commit()
        return cur.rowcount


def delete_unavailable_listings() -> int:
    """Supprime définitivement toutes les annonces indisponibles non placées en quarantaine."""
    init_db()
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM listings WHERE is_available = 0 AND is_quarantined = 0")
        conn.commit()
        return cur.rowcount


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
    """Restaure les critères, annonces et suivi IA/CRM depuis un export JSON en une seule transaction."""
    init_db()
    data = json.loads(json_str)

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
                SET transaction_type = COALESCE(?, transaction_type),
                    max_budget = COALESCE(?, max_budget),
                    min_surface = COALESCE(?, min_surface),
                    min_rooms = COALESCE(?, min_rooms),
                    min_bedrooms = COALESCE(?, min_bedrooms),
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
                    criteria.get("transaction_type"),
                    criteria.get("max_budget"),
                    criteria.get("min_surface"),
                    criteria.get("min_rooms"),
                    criteria.get("min_bedrooms"),
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
        conn.execute("BEGIN IMMEDIATE")
        for item in listings:
            canon_city = _canonical_city_name(str(item.get("city", "")))
            existing = conn.execute(
                "SELECT description, dpe, has_parking, bedrooms FROM listings WHERE external_id = ?",
                (item["external_id"],),
            ).fetchone()

            imp_desc = str(item.get("description") or "")
            imp_dpe = str(item.get("dpe") or "Non renseigné")
            imp_parking = item.get("has_parking")
            imp_bedrooms = int(item.get("bedrooms") or 0)

            if existing:
                ex_desc = str(existing["description"] or "")
                if len(ex_desc) > len(imp_desc):
                    imp_desc = ex_desc
                ex_dpe = str(existing["dpe"] or "Non renseigné")
                if imp_dpe not in ("A", "B", "C", "D", "E", "F", "G") and ex_dpe in ("A", "B", "C", "D", "E", "F", "G"):
                    imp_dpe = ex_dpe
                if imp_parking is None and existing["has_parking"] is not None:
                    imp_parking = existing["has_parking"]
                if imp_bedrooms == 0 and existing["bedrooms"]:
                    imp_bedrooms = int(existing["bedrooms"])

            conn.execute(
                """
                INSERT OR REPLACE INTO listings (
                    external_id, source, url, title, transaction_type, property_type,
                    price, surface, rooms, bedrooms, city, postal_code,
                    description, dpe, has_parking, raw_json,
                    status, ai_score, price_per_m2, ai_summary,
                    ai_rejection_reason, ai_pros, ai_cons, ai_signals,
                    ai_analysis_json,
                    is_selected, is_quarantined, contact_made, visit_scheduled,
                    agency_name, agency_phone, followup_notes,
                    selected_at, quarantined_at, scraped_at, analyzed_at,
                    is_available, unavailable_reason, availability_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["external_id"],
                    item.get("source", "Import"),
                    item["url"],
                    item["title"],
                    item.get("transaction_type", "buy"),
                    item.get("property_type", "Autre"),
                    int(item.get("price", 0)),
                    float(item.get("surface", 0.0)),
                    int(item.get("rooms", 0)),
                    imp_bedrooms,
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
                    1 if item.get("is_available") is None else int(bool(item.get("is_available"))),
                    str(item.get("unavailable_reason") or ""),
                    item.get("availability_checked_at"),
                ),
            )
            restored += 1
        conn.commit()
    return restored
