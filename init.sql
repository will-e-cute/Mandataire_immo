-- Schéma SQL d'initialisation pour le Mandataire Immobilier Virtuel (SQLite)

CREATE TABLE IF NOT EXISTS search_criteria (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    max_budget INTEGER NOT NULL DEFAULT 280000,
    min_surface REAL NOT NULL DEFAULT 80.0,
    property_types TEXT NOT NULL DEFAULT 'Maison,Appartement',
    cities TEXT NOT NULL DEFAULT 'Croix,Wasquehal,Roubaix,Villeneuve-d''Ascq,Marcq-en-Barœul',
    require_parking INTEGER NOT NULL DEFAULT 1,
    exclude_dpe_fg INTEGER NOT NULL DEFAULT 1,
    exclude_occupied INTEGER NOT NULL DEFAULT 1,
    exclude_heavy_works INTEGER NOT NULL DEFAULT 1,
    exclude_copro_issues INTEGER NOT NULL DEFAULT 1,
    custom_notes TEXT DEFAULT '',
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO search_criteria (
    id, max_budget, min_surface, property_types, cities,
    require_parking, exclude_dpe_fg, exclude_occupied, exclude_heavy_works, exclude_copro_issues
) VALUES (
    1, 280000, 80.0, 'Maison,Appartement',
    'Croix,Wasquehal,Roubaix,Villeneuve-d''Ascq,Marcq-en-Barœul',
    1, 1, 1, 1, 1
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    property_type TEXT DEFAULT 'Autre',
    price INTEGER NOT NULL,
    surface REAL NOT NULL,
    rooms INTEGER DEFAULT 0,
    city TEXT NOT NULL,
    postal_code TEXT DEFAULT '',
    description TEXT NOT NULL,
    dpe TEXT DEFAULT 'Non renseigné',
    has_parking INTEGER DEFAULT NULL,
    raw_json TEXT DEFAULT '{}',
    -- Statut du pipeline: 'pending_analysis', 'a_visiter', 'a_verifier', 'rejete'
    status TEXT NOT NULL DEFAULT 'pending_analysis',
    ai_score INTEGER DEFAULT NULL,
    price_per_m2 REAL DEFAULT NULL,
    ai_summary TEXT DEFAULT NULL,
    ai_rejection_reason TEXT DEFAULT NULL,
    ai_pros TEXT DEFAULT '[]',
    ai_cons TEXT DEFAULT '[]',
    ai_signals TEXT DEFAULT '[]',
    ai_analysis_json TEXT DEFAULT '{}',
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    analyzed_at TIMESTAMP DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_score ON listings(ai_score DESC);
CREATE INDEX IF NOT EXISTS idx_listings_city ON listings(city);
