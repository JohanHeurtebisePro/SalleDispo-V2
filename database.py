"""
database.py — Couche d'accès aux données SQLite pour SalleDispo
Sources de données : tout est en base, plus de config.json ni de dossier salleICS/ obligatoire.

Tables :
    users           — Utilisateurs (étudiants + admins)
    sites           — Sites (campus) rattachés à l'établissement [MULTI-SITES]
    salles          — Source de vérité unique pour les salles (remplace config.json + filesystem ICS)
    reservations    — Réservations étudiants / admin
    blocages        — Blocages admin
    reports         — Signalements
    salle_photos    — Photos par salle
"""

import sqlite3
import os
from datetime import datetime
from contextlib import contextmanager
from werkzeug.security import generate_password_hash

# ── Chiffrement des URLs ICS (Niveau 2) ──────────────────────────────────────
# Import optionnel : si ics_crypto n'est pas encore présent (premier démarrage),
# on se replie sur des fonctions no-op pour ne pas bloquer l'application.
try:
    from ics_crypto import encrypt_url as _encrypt_url, decrypt_url as _decrypt_url
    _CRYPTO_AVAILABLE = True
except ImportError:
    def _encrypt_url(v): return v   # type: ignore[misc]
    def _decrypt_url(v): return v   # type: ignore[misc]
    _CRYPTO_AVAILABLE = False


def _row_decrypt_ical(row_dict: dict) -> dict:
    """Déchiffre ical_url dans un dict salle avant de le renvoyer à l'appelant."""
    if row_dict and row_dict.get("ical_url"):
        row_dict = dict(row_dict)
        row_dict["ical_url"] = _decrypt_url(row_dict["ical_url"])
    return row_dict

# ── Chemin du fichier DB ──────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "salledispo.db")


# ══════════════════════════════════════════════════════════════════════════════
# CONNEXION
# ══════════════════════════════════════════════════════════════════════════════

@contextmanager
def get_conn():
    """Gestionnaire de contexte : ouvre une connexion, commit ou rollback, ferme."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# SCHÉMA
# ══════════════════════════════════════════════════════════════════════════════

SCHEMA = """

-- ── Sites (campus / antennes) ─────────────────────────────────────────────────
-- Un établissement peut avoir plusieurs sites géographiques.
-- site_id NULLable sur users : NULL = super-admin (tous sites), rempli = admin de site
-- jours_ouverture : liste CSV des jours ouverts (0=Lundi ... 6=Dimanche, cf. date.weekday()).
-- Par défaut : ouvert du lundi au vendredi.
CREATE TABLE IF NOT EXISTS sites (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    etablissement_id INTEGER NOT NULL DEFAULT 1,  -- pour usage futur multi-établissements
    nom              TEXT NOT NULL,
    adresse          TEXT NOT NULL DEFAULT '',
    couleur          TEXT NOT NULL DEFAULT '#0a5ad2',
    logo_url         TEXT,
    timezone         TEXT NOT NULL DEFAULT 'Europe/Paris',
    heure_ouverture  TEXT NOT NULL DEFAULT '08:00',
    heure_fermeture  TEXT NOT NULL DEFAULT '20:00',
    jours_ouverture  TEXT NOT NULL DEFAULT '0,1,2,3,4',
    actif            INTEGER NOT NULL DEFAULT 1,
    date_creation    TEXT NOT NULL,
    date_maj         TEXT NOT NULL
);

-- ── Utilisateurs ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    username         TEXT PRIMARY KEY,
    password_hash    TEXT NOT NULL,
    role             TEXT NOT NULL DEFAULT 'etudiant',   -- 'admin' | 'etudiant'
    nom_complet      TEXT NOT NULL DEFAULT '',
    email            TEXT NOT NULL DEFAULT '',
    numero_etudiant  TEXT,
    promotion        TEXT,
    actif            INTEGER NOT NULL DEFAULT 1
);

-- ── Salles (SOURCE DE VÉRITÉ UNIQUE) ─────────────────────────────────────────
-- ical_url  : URL distante chiffrée (Fernet) — prioritaire
-- ical_file : nom de fichier local fallback dans salleICS/
-- actif     : 0 = salle masquée sans être supprimée
-- places    : NULL = capacité inconnue
CREATE TABLE IF NOT EXISTS salles (
    nom              TEXT PRIMARY KEY,
    nom_complet      TEXT NOT NULL DEFAULT '',
    places           INTEGER,
    pc               INTEGER NOT NULL DEFAULT 0,
    projecteur       INTEGER NOT NULL DEFAULT 0,
    tableau          INTEGER NOT NULL DEFAULT 1,
    description      TEXT NOT NULL DEFAULT '',
    etage            TEXT NOT NULL DEFAULT '0',
    aile             TEXT NOT NULL DEFAULT 'centre',
    ical_url         TEXT,
    ical_file        TEXT,
    actif            INTEGER NOT NULL DEFAULT 1,
    date_creation    TEXT NOT NULL,
    date_maj         TEXT NOT NULL
);

-- ── Réservations ──────────────────────────────────────────────────────────────
-- statut      : 'confirmee' | 'annulee'
-- dates       : ISO 8601 avec timezone (ex: 2026-05-06T14:00:00+02:00)
-- nb_personnes: nombre total de personnes (organisateur + participants déclarés)
--               NULL = ancienne réservation sans info de capacité
CREATE TABLE IF NOT EXISTS reservations (
    id               TEXT PRIMARY KEY,
    salle            TEXT NOT NULL REFERENCES salles(nom) ON DELETE CASCADE,
    user             TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    motif            TEXT NOT NULL DEFAULT '',
    date_debut       TEXT NOT NULL,
    date_fin         TEXT NOT NULL,
    date_creation    TEXT NOT NULL,
    statut           TEXT NOT NULL DEFAULT 'confirmee',
    cree_par_admin   INTEGER NOT NULL DEFAULT 0,
    annulee_par      TEXT,
    date_annulation  TEXT,
    nb_personnes     INTEGER
);

-- ── Participants de réservation ───────────────────────────────────────────────
-- Lie des utilisateurs supplémentaires à une réservation
CREATE TABLE IF NOT EXISTS reservation_participants (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id   TEXT NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
    username         TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    UNIQUE(reservation_id, username)
);

-- ── Blocages admin ────────────────────────────────────────────────────────────
-- dates : ISO 8601 avec timezone
CREATE TABLE IF NOT EXISTS blocages (
    id               TEXT PRIMARY KEY,
    salle            TEXT NOT NULL REFERENCES salles(nom) ON DELETE CASCADE,
    date_debut       TEXT NOT NULL,
    date_fin         TEXT NOT NULL,
    motif            TEXT NOT NULL DEFAULT '',
    cree_par         TEXT NOT NULL,
    date_creation    TEXT NOT NULL
);

-- ── Signalements (tickets) ────────────────────────────────────────────────────
-- statut         : 'nouveau' | 'en_cours' | 'resolu'
-- priorite       : 'basse' | 'normale' | 'haute' | 'urgente'
-- site_id        : site de la salle concernée, calculé à la création (cache dénormalisé)
-- admin_assigne  : username de l'admin en charge du ticket (NULL = non affecté)
-- date_creation  : ISO 8601 (ex: 2026-05-06T22:12:00+02:00)
-- date_maj       : ISO 8601 ou NULL si jamais modifié
CREATE TABLE IF NOT EXISTS reports (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    salle            TEXT NOT NULL REFERENCES salles(nom) ON DELETE CASCADE,
    type             TEXT NOT NULL,
    description      TEXT NOT NULL,
    auteur           TEXT NOT NULL DEFAULT 'Anonyme',
    statut           TEXT NOT NULL DEFAULT 'nouveau',
    priorite         TEXT NOT NULL DEFAULT 'normale',
    site_id          INTEGER REFERENCES sites(id) ON DELETE SET NULL,
    admin_assigne    TEXT REFERENCES users(username) ON DELETE SET NULL,
    note_admin       TEXT,
    date_creation    TEXT NOT NULL,
    date_maj         TEXT
);

-- ── Historique / frise chronologique des tickets ──────────────────────────────
-- type_evenement : 'creation' | 'statut' | 'priorite' | 'assignation' | 'commentaire'
--                  | 'commentaire_modifie' | 'commentaire_supprime'
-- ancienne_valeur / nouvelle_valeur : libellés bruts (statut, priorité, username...)
-- detail         : texte libre complémentaire (ex : note laissée au changement de statut)
CREATE TABLE IF NOT EXISTS report_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    type_evenement   TEXT NOT NULL,
    ancienne_valeur  TEXT,
    nouvelle_valeur  TEXT,
    detail           TEXT,
    auteur           TEXT NOT NULL DEFAULT 'system',
    date_creation    TEXT NOT NULL
);

-- ── Suivi détaillé (commentaires internes admin) sur un ticket ────────────────
-- Modifiable/supprimable par l'admin qui gère le ticket (ou le super-admin).
CREATE TABLE IF NOT EXISTS report_comments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id        INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    auteur           TEXT NOT NULL,
    texte            TEXT NOT NULL,
    date_creation    TEXT NOT NULL,
    date_maj         TEXT
);

-- ── Photos de salles ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS salle_photos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    salle            TEXT NOT NULL REFERENCES salles(nom) ON DELETE CASCADE,
    filename         TEXT NOT NULL,
    legende          TEXT NOT NULL DEFAULT '',
    ordre            INTEGER NOT NULL DEFAULT 0,
    date_ajout       TEXT NOT NULL
);

-- ── Demandes de changement de mot de passe ────────────────────────────────────
-- statut : 'en_attente' | 'approuvee' | 'refusee'
CREATE TABLE IF NOT EXISTS pwd_requests (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    motif            TEXT NOT NULL DEFAULT '',
    statut           TEXT NOT NULL DEFAULT 'en_attente',
    traite_par       TEXT,
    date_creation    TEXT NOT NULL,
    date_traitement  TEXT
);

-- ── Annonces admin (e-mails de masse) ─────────────────────────────────────────
-- cible_type  : 'tous' | 'promotion' | 'utilisateurs'
-- cible_value : NULL (tous), nom de promo, ou liste de usernames séparés par ','
-- statut      : 'envoyee' | 'erreur'
CREATE TABLE IF NOT EXISTS annonces (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    sujet            TEXT NOT NULL,
    corps            TEXT NOT NULL,
    cible_type       TEXT NOT NULL DEFAULT 'tous',
    cible_value      TEXT,
    nb_destinataires INTEGER NOT NULL DEFAULT 0,
    envoye_par       TEXT NOT NULL,
    date_envoi       TEXT NOT NULL,
    statut           TEXT NOT NULL DEFAULT 'envoyee'
);

-- ── Tokens de réinitialisation de mot de passe ────────────────────────────────
-- token    : UUID v4 aléatoire, usage unique
-- expires  : ISO 8601, validité 30 minutes
-- used     : 1 dès que le token a été consommé
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    username         TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    token            TEXT NOT NULL UNIQUE,
    expires          TEXT NOT NULL,
    used             INTEGER NOT NULL DEFAULT 0,
    date_creation    TEXT NOT NULL
);

-- ── Notifications utilisateur ──────────────────────────────────────────────────
-- type   : 'reservation' | 'annulation' | 'invitation' | 'annonce' | 'reset_pwd' | 'system'
-- lu     : 0 = non lu, 1 = lu
-- lien   : URL optionnelle vers la ressource concernée
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL REFERENCES users(username) ON DELETE CASCADE,
    type        TEXT NOT NULL DEFAULT 'system',
    titre       TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    lien        TEXT,
    lu          INTEGER NOT NULL DEFAULT 0,
    date_envoi  TEXT NOT NULL
);

-- ── Paramètres globaux de l'application ──────────────────────────────────────
-- Stockage clé-valeur pour la configuration pilotée depuis l'UI admin.
-- cle      : identifiant unique du paramètre (ex: 'etablissement_nom')
-- valeur   : valeur sérialisée en texte (entiers, booléens, JSON inclus)
-- categorie: regroupe les paramètres par thème ('etablissement' | 'app' | 'horaires')
-- date_maj : horodatage ISO 8601 de la dernière modification
CREATE TABLE IF NOT EXISTS settings (
    cle         TEXT PRIMARY KEY,
    valeur      TEXT NOT NULL DEFAULT '',
    categorie   TEXT NOT NULL DEFAULT 'app',
    date_maj    TEXT NOT NULL
);

-- ── Promotions (référentiel) ──────────────────────────────────────────────────
-- Remplace la liste PROMOTIONS_DISPONIBLES codée en dur dans app.py.
-- actif : 0 = masquée des formulaires sans être supprimée
CREATE TABLE IF NOT EXISTS promotions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nom         TEXT NOT NULL UNIQUE,
    ordre       INTEGER NOT NULL DEFAULT 0,
    actif       INTEGER NOT NULL DEFAULT 1,
    date_creation TEXT NOT NULL
);
"""

# Migrations idempotentes appliquées à chaque démarrage
# Corrigent les données existantes sans toucher aux nouvelles
MIGRATIONS = """
DROP TABLE IF EXISTS salle_meta;
UPDATE reports SET note_admin = NULL WHERE note_admin = 'None';
UPDATE salles SET places = NULL WHERE places = '?';
"""

# Migration séparée pour les colonnes (ne peut pas être dans executescript car dépend d'une vérification)
COLUMN_MIGRATIONS = [
    ("reservations", "nb_personnes", "ALTER TABLE reservations ADD COLUMN nb_personnes INTEGER"),
    ("users", "must_change_password", "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"),
    # ── Multi-sites [nouveau] ──────────────────────────────────────────────────
    ("salles", "site_id", "ALTER TABLE salles ADD COLUMN site_id INTEGER REFERENCES sites(id) ON DELETE SET NULL"),
    ("users",  "site_id", "ALTER TABLE users  ADD COLUMN site_id INTEGER REFERENCES sites(id) ON DELETE SET NULL"),
    # ── Horaires d'ouverture / fermeture par site [nouveau] ────────────────────
    ("sites", "heure_ouverture", "ALTER TABLE sites ADD COLUMN heure_ouverture TEXT NOT NULL DEFAULT '08:00'"),
    ("sites", "heure_fermeture", "ALTER TABLE sites ADD COLUMN heure_fermeture TEXT NOT NULL DEFAULT '20:00'"),
    ("sites", "jours_ouverture", "ALTER TABLE sites ADD COLUMN jours_ouverture TEXT NOT NULL DEFAULT '0,1,2,3,4'"),
    # ── Tickets enrichis : affectation par site + priorité [nouveau] ───────────
    ("reports", "priorite",      "ALTER TABLE reports ADD COLUMN priorite TEXT NOT NULL DEFAULT 'normale'"),
    ("reports", "site_id",       "ALTER TABLE reports ADD COLUMN site_id INTEGER REFERENCES sites(id) ON DELETE SET NULL"),
    ("reports", "admin_assigne", "ALTER TABLE reports ADD COLUMN admin_assigne TEXT REFERENCES users(username) ON DELETE SET NULL"),
]

# Valeurs par défaut des paramètres de l'établissement injectées au premier démarrage
SETTINGS_DEFAULTS = [
    # ── Identité établissement ──
    ("etablissement_nom",       "IUT",                  "etablissement"),
    ("etablissement_sous_titre","Réservation de salles", "etablissement"),
    ("etablissement_adresse",   "",                      "etablissement"),
    ("etablissement_ville",     "",                      "etablissement"),
    ("etablissement_email",     "",                      "etablissement"),
    ("etablissement_telephone", "",                      "etablissement"),
    ("etablissement_site_web",  "",                      "etablissement"),
    ("etablissement_couleur",   "#0a5ad2",               "etablissement"),
    # ── Promotions seed (si table vide) ──
    # (gérées séparément via la table promotions)
]

PROMOTIONS_DEFAULTS = [
    ("BUT RT 1",   0),
    ("BUT RT 2",   1),
    ("BUT RT 3",   2),
    ("BUT INFO 1", 3),
    ("BUT INFO 2", 4),
    ("BUT INFO 3", 5),
    ("LP SISR",    6),
    ("LP ASUR",    7),
    ("DUT RT 1",   8),
    ("DUT RT 2",   9),
    ("DUT INFO 1", 10),
    ("DUT INFO 2", 11),
]

SEED_USERS = []
SEED_SALLES = []

def init_db():
    """Crée les tables et insère les données initiales si besoin. Applique les migrations idempotentes."""
    with get_conn() as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        # Migrations idempotentes : nettoyage données legacy, suppression tables obsolètes
        conn.executescript(MIGRATIONS)

        # Migrations de colonnes (idempotentes via vérification préalable)
        for table, col, sql in COLUMN_MIGRATIONS:
            existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
            if col not in existing:
                conn.execute(sql)

        # ── Backfill : renseigne reports.site_id pour les tickets déjà en base
        #    (créés avant l'ajout de la colonne) à partir du site de leur salle. ──
        conn.execute(
            """UPDATE reports
               SET site_id = (SELECT s.site_id FROM salles s WHERE s.nom = reports.salle)
               WHERE site_id IS NULL"""
        )

        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            conn.executemany(
                """INSERT INTO users
                   (username, password_hash, role, nom_complet, email, numero_etudiant, promotion, actif)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                SEED_USERS
            )

        count_salles = conn.execute("SELECT COUNT(*) FROM salles").fetchone()[0]
        if count_salles == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for s in SEED_SALLES:
                conn.execute(
                    """INSERT OR IGNORE INTO salles
                       (nom, nom_complet, places, pc, projecteur, tableau, description, etage, aile, ical_url, ical_file, actif, date_creation, date_maj)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (*s, now, now)
                )

        # ── Amorçage des paramètres par défaut ──────────────────────────────
        now = datetime.now().isoformat()
        for cle, valeur, categorie in SETTINGS_DEFAULTS:
            conn.execute(
                "INSERT OR IGNORE INTO settings (cle, valeur, categorie, date_maj) VALUES (?, ?, ?, ?)",
                (cle, valeur, categorie, now)
            )

        # ── Amorçage des promotions par défaut ──────────────────────────────
        count_promos = conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
        if count_promos == 0:
            for nom, ordre in PROMOTIONS_DEFAULTS:
                conn.execute(
                    "INSERT OR IGNORE INTO promotions (nom, ordre, actif, date_creation) VALUES (?, ?, 1, ?)",
                    (nom, ordre, now)
                )




# ══════════════════════════════════════════════════════════════════════════════
# SITES (multi-campus) [MULTI-SITES]
# ══════════════════════════════════════════════════════════════════════════════

def get_all_sites(include_inactive: bool = False) -> list[dict]:
    """Retourne tous les sites (ou actifs seulement)."""
    with get_conn() as conn:
        if include_inactive:
            rows = conn.execute("SELECT * FROM sites ORDER BY nom").fetchall()
        else:
            rows = conn.execute("SELECT * FROM sites WHERE actif = 1 ORDER BY nom").fetchall()
        return [dict(r) for r in rows]


def get_site(site_id: int) -> dict | None:
    """Retourne un site par son ID ou None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
        return dict(row) if row else None


def create_site(nom: str, adresse: str = "", couleur: str = "#0a5ad2",
                logo_url: str = None, timezone: str = "Europe/Paris",
                heure_ouverture: str = "08:00", heure_fermeture: str = "20:00",
                jours_ouverture: str = "0,1,2,3,4") -> int | None:
    """Crée un site. Retourne l'id créé ou None si erreur."""
    now = datetime.now().isoformat()
    try:
        with get_conn() as conn:
            cur = conn.execute(
                """INSERT INTO sites (nom, adresse, couleur, logo_url, timezone,
                                       heure_ouverture, heure_fermeture, jours_ouverture,
                                       actif, date_creation, date_maj)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (nom.strip(), adresse, couleur, logo_url, timezone,
                 heure_ouverture, heure_fermeture, jours_ouverture, now, now)
            )
            return cur.lastrowid
    except Exception:
        return None


def update_site(site_id: int, **kwargs) -> bool:
    """Met à jour les champs d'un site. Retourne False si introuvable."""
    allowed = {"nom", "adresse", "couleur", "logo_url", "timezone",
               "heure_ouverture", "heure_fermeture", "jours_ouverture", "actif"}
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if not data:
        return False
    now = datetime.now().isoformat()
    with get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in data)
        vals = list(data.values()) + [now, site_id]
        cur = conn.execute(f"UPDATE sites SET {sets}, date_maj = ? WHERE id = ?", vals)
        return cur.rowcount > 0


def delete_site(site_id: int) -> bool:
    """Supprime un site (met site_id = NULL sur salles/users rattachés)."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        return cur.rowcount > 0


def toggle_site_actif(site_id: int) -> bool | None:
    """Bascule actif/inactif d'un site."""
    with get_conn() as conn:
        row = conn.execute("SELECT actif FROM sites WHERE id = ?", (site_id,)).fetchone()
        if not row:
            return None
        new_val = 0 if row["actif"] else 1
        conn.execute("UPDATE sites SET actif = ?, date_maj = ? WHERE id = ?",
                     (new_val, datetime.now().isoformat(), site_id))
        return bool(new_val)


# ── Filtrage par site ──────────────────────────────────────────────────────────

def get_salles_by_site(site_id: int | None, include_inactive: bool = False) -> list[dict]:
    """
    Retourne les salles d'un site donné.
    Si site_id est None, retourne toutes les salles (comportement super-admin).
    """
    if site_id is None:
        return get_all_salles(include_inactive=include_inactive)
    with get_conn() as conn:
        if include_inactive:
            rows = conn.execute(
                "SELECT * FROM salles WHERE site_id = ? ORDER BY nom", (site_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM salles WHERE site_id = ? AND actif = 1 ORDER BY nom", (site_id,)
            ).fetchall()
        return [_row_decrypt_ical(dict(r)) for r in rows]


def get_reservations_by_site(site_id: int | None) -> list[dict]:
    """
    Retourne toutes les réservations filtrées par site.
    Si site_id est None, retourne toutes les réservations.
    """
    if site_id is None:
        return get_all_reservations()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.* FROM reservations r
               JOIN salles s ON s.nom = r.salle
               WHERE s.site_id = ?
               ORDER BY r.date_debut DESC""",
            (site_id,)
        ).fetchall()
        return [_row_to_resa(r) for r in rows]


def get_users_by_site(site_id: int | None) -> list[dict]:
    """
    Retourne les utilisateurs d'un site (role etudiant).
    Si site_id est None (super-admin), retourne tous les utilisateurs.
    Note : les étudiants n'ont pas de site_id, ils voient les salles du site sélectionné.
    Pour l'admin de site, retourne tous les étudiants actifs (ils peuvent tous réserver).
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY role DESC, username"
        ).fetchall()
        return [dict(r) for r in rows]


def assign_salle_to_site(nom_salle: str, site_id: int | None) -> bool:
    """Rattache (ou détache) une salle à un site."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE salles SET site_id = ?, date_maj = ? WHERE nom = ?",
            (site_id, datetime.now().isoformat(), nom_salle)
        )
        return cur.rowcount > 0


def assign_admin_to_site(username: str, site_id: int | None) -> bool:
    """
    Assigne un admin à un site (admin de site) ou le passe super-admin (site_id=None).
    Ne fonctionne que sur les utilisateurs de role='admin'.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET site_id = ? WHERE username = ? AND role = 'admin'",
            (site_id, username)
        )
        return cur.rowcount > 0

# ══════════════════════════════════════════════════════════════════════════════
# SALLES
# ══════════════════════════════════════════════════════════════════════════════

def get_all_salles(include_inactive=False) -> list[dict]:
    """Retourne toutes les salles actives (ou toutes si include_inactive=True)."""
    with get_conn() as conn:
        if include_inactive:
            rows = conn.execute("SELECT * FROM salles ORDER BY nom").fetchall()
        else:
            rows = conn.execute("SELECT * FROM salles WHERE actif = 1 ORDER BY nom").fetchall()
        return [_row_decrypt_ical(dict(r)) for r in rows]


def get_salle(nom: str) -> dict | None:
    """Retourne une salle par son identifiant ou None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM salles WHERE nom = ?", (nom,)).fetchone()
        return _row_decrypt_ical(dict(row)) if row else None


def create_salle(nom: str, nom_complet: str, places: str = None, pc: bool = False,
                 projecteur: bool = False, tableau: bool = True, description: str = "",
                 etage: str = "0", aile: str = "centre", ical_url: str = None,
                 ical_file: str = None) -> bool:
    """Crée une nouvelle salle. Retourne False si l'identifiant existe déjà."""
    now = datetime.now().isoformat()
    # Convertir places en entier ou NULL
    places_int = None
    if places and str(places).strip() not in ("", "?"):
        try:
            places_int = int(places)
        except ValueError:
            places_int = None
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO salles
                   (nom, nom_complet, places, pc, projecteur, tableau, description,
                    etage, aile, ical_url, ical_file, actif, date_creation, date_maj)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (nom.strip(), nom_complet.strip(), places_int, int(pc), int(projecteur), int(tableau),
                 description, etage, aile, _encrypt_url(ical_url) if ical_url else None,
                 ical_file or None, now, now)
            )
        return True
    except sqlite3.IntegrityError:
        return False  # nom déjà pris


def update_salle(nom: str, **kwargs) -> bool:
    """Met à jour les champs d'une salle. Retourne False si introuvable."""
    allowed = {"nom_complet", "places", "pc", "projecteur", "tableau",
               "description", "etage", "aile", "ical_url", "ical_file", "actif"}
    data = {k: v for k, v in kwargs.items() if k in allowed}
    if not data:
        return False
    # Chiffrer ical_url si fournie en clair
    if "ical_url" in data:
        data["ical_url"] = _encrypt_url(data["ical_url"]) if data["ical_url"] else None
    # Convertir places en entier ou NULL
    if "places" in data:
        p = data["places"]
        if p is None or str(p).strip() in ("", "?"):
            data["places"] = None
        else:
            try:
                data["places"] = int(p)
            except (ValueError, TypeError):
                data["places"] = None
    now = datetime.now().isoformat()
    with get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in data)
        vals = list(data.values()) + [now, nom]
        cur = conn.execute(f"UPDATE salles SET {sets}, date_maj = ? WHERE nom = ?", vals)
        return cur.rowcount > 0


def delete_salle(nom: str) -> bool:
    """Supprime définitivement une salle de la DB."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM salles WHERE nom = ?", (nom,))
        return cur.rowcount > 0


def toggle_salle_actif(nom: str) -> bool | None:
    """Bascule actif/inactif. Retourne le nouvel état ou None si introuvable."""
    with get_conn() as conn:
        row = conn.execute("SELECT actif FROM salles WHERE nom = ?", (nom,)).fetchone()
        if not row:
            return None
        new_val = 0 if row["actif"] else 1
        conn.execute("UPDATE salles SET actif = ?, date_maj = ? WHERE nom = ?",
                     (new_val, datetime.now().isoformat(), nom))
        return bool(new_val)


def salle_exists(nom: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM salles WHERE nom = ? AND actif = 1", (nom,)).fetchone()
        return row is not None


# ══════════════════════════════════════════════════════════════════════════════
# USERS
# ══════════════════════════════════════════════════════════════════════════════

def get_user(username: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_all_users() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY role DESC, username").fetchall()
        return [dict(r) for r in rows]


def search_users(q: str, exclude_username: str = None, limit: int = 8) -> list[dict]:
    """Recherche des utilisateurs actifs par nom, username, promo ou numéro étudiant."""
    pattern = f"%{q}%"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT username, nom_complet, email, promotion, numero_etudiant
               FROM users
               WHERE actif = 1
                 AND (username LIKE ? OR nom_complet LIKE ? OR promotion LIKE ? OR numero_etudiant LIKE ?)
                 AND (? IS NULL OR username != ?)
               ORDER BY nom_complet
               LIMIT ?""",
            (pattern, pattern, pattern, pattern, exclude_username, exclude_username, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def create_user(username, password, nom_complet, email="", numero_etudiant=None, promotion=None, role="etudiant", site_id=None) -> bool:
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO users
                   (username, password_hash, role, nom_complet, email, numero_etudiant, promotion, actif, site_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (username, generate_password_hash(password), role, nom_complet, email, numero_etudiant, promotion, site_id)
            )
        return True
    except sqlite3.IntegrityError:
        return False


def update_user(username, nom_complet=None, email=None, numero_etudiant=None, promotion=None, new_password=None):
    with get_conn() as conn:
        if nom_complet is not None:
            conn.execute("UPDATE users SET nom_complet = ? WHERE username = ?", (nom_complet, username))
        if email is not None:
            conn.execute("UPDATE users SET email = ? WHERE username = ?", (email, username))
        if numero_etudiant is not None:
            conn.execute("UPDATE users SET numero_etudiant = ? WHERE username = ?", (numero_etudiant, username))
        if promotion is not None:
            conn.execute("UPDATE users SET promotion = ? WHERE username = ?", (promotion, username))
        if new_password:
            conn.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                         (generate_password_hash(new_password), username))


def toggle_user_actif(username) -> bool | None:
    with get_conn() as conn:
        row = conn.execute("SELECT actif FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return None
        new_val = 0 if row["actif"] else 1
        conn.execute("UPDATE users SET actif = ? WHERE username = ?", (new_val, username))
        return bool(new_val)


def check_password(username, password) -> bool:
    from werkzeug.security import check_password_hash
    user = get_user(username)
    if not user:
        return False
    return check_password_hash(user["password_hash"], password)


# ══════════════════════════════════════════════════════════════════════════════
# RÉSERVATIONS
# ══════════════════════════════════════════════════════════════════════════════

def _row_to_resa(row) -> dict:
    d = dict(row)
    d["cree_par_admin"] = bool(d.get("cree_par_admin", 0))
    return d


def get_all_reservations() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM reservations ORDER BY date_debut DESC").fetchall()
        return [_row_to_resa(r) for r in rows]


def delete_reservations(reservation_ids: list[str]) -> int:
    """
    Supprime DÉFINITIVEMENT une liste de réservations (par id).
    Contrairement à cancel_reservation() qui fait un soft-delete (statut='annulee'),
    cette fonction retire la ligne de la base. Les participants liés (reservation_participants)
    sont supprimés en cascade (ON DELETE CASCADE).
    Retourne le nombre de lignes effectivement supprimées.
    """
    if not reservation_ids:
        return 0
    with get_conn() as conn:
        placeholders = ",".join("?" * len(reservation_ids))
        cur = conn.execute(
            f"DELETE FROM reservations WHERE id IN ({placeholders})",
            list(reservation_ids)
        )
        return cur.rowcount


def get_reservations_salle(nom_salle: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE salle = ? AND statut = 'confirmee' ORDER BY date_debut",
            (nom_salle,)
        ).fetchall()
        return [_row_to_resa(r) for r in rows]


def get_reservations_user(user_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE user = ? AND statut = 'confirmee' ORDER BY date_debut DESC",
            (user_id,)
        ).fetchall()
        return [_row_to_resa(r) for r in rows]


def get_all_reservations_user(user_id: str) -> list[dict]:
    """Retourne toutes les réservations d'un utilisateur (passées, futures, annulées)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reservations WHERE user = ? ORDER BY date_debut DESC",
            (user_id,)
        ).fetchall()
        return [_row_to_resa(r) for r in rows]


def get_reservations_as_participant(user_id: str) -> list[dict]:
    """
    Retourne les réservations confirmées (futures) où l'utilisateur est participant
    (ajouté par quelqu'un d'autre), sans les réservations qu'il a lui-même créées.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*
               FROM reservations r
               JOIN reservation_participants rp ON rp.reservation_id = r.id
               WHERE rp.username = ?
                 AND r.user != ?
                 AND r.statut = 'confirmee'
               ORDER BY r.date_debut DESC""",
            (user_id, user_id)
        ).fetchall()
        return [_row_to_resa(r) for r in rows]


def get_all_reservations_as_participant(user_id: str) -> list[dict]:
    """
    Retourne toutes les réservations (passées, futures, annulées) où l'utilisateur
    est participant (pas organisateur), pour l'historique du profil.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT r.*
               FROM reservations r
               JOIN reservation_participants rp ON rp.reservation_id = r.id
               WHERE rp.username = ?
                 AND r.user != ?
               ORDER BY r.date_debut DESC""",
            (user_id, user_id)
        ).fetchall()
        return [_row_to_resa(r) for r in rows]


def add_reservation(id_, salle, user, motif, date_debut, date_fin, date_creation, cree_par_admin=False, nb_personnes=None) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reservations
               (id, salle, user, motif, date_debut, date_fin, date_creation, statut, cree_par_admin, nb_personnes)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'confirmee', ?, ?)""",
            (id_, salle, user, motif, date_debut, date_fin, date_creation, int(cree_par_admin), nb_personnes)
        )


def get_reservation(reservation_id: str) -> dict | None:
    """Retourne une réservation par son ID (str), ou None si introuvable."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
        return _row_to_resa(row) if row else None


def cancel_reservation(reservation_id: str, user_id: str, is_admin: bool = False):
    with get_conn() as conn:
        if is_admin:
            cur = conn.execute(
                "UPDATE reservations SET statut='annulee', annulee_par=?, date_annulation=? WHERE id=?",
                (user_id, datetime.now().isoformat(), reservation_id)
            )
            if cur.rowcount > 0:
                return True, "Réservation annulée."
            return False, "Réservation introuvable."
        else:
            cur = conn.execute(
                "UPDATE reservations SET statut='annulee', annulee_par=?, date_annulation=? WHERE id=? AND user=?",
                (user_id, datetime.now().isoformat(), reservation_id, user_id)
            )
            if cur.rowcount > 0:
                return True, "Réservation annulée."
            return False, "Réservation introuvable ou non autorisée."


def add_participants(reservation_id: str, usernames: list[str]) -> int:
    """
    Ajoute une liste de participants à une réservation.
    Ignore silencieusement les doublons (UNIQUE constraint).
    Retourne le nombre de participants effectivement insérés.
    """
    if not usernames:
        return 0
    inserted = 0
    with get_conn() as conn:
        for username in usernames:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO reservation_participants
                       (reservation_id, username) VALUES (?, ?)""",
                    (reservation_id, username)
                )
                inserted += 1
            except Exception:
                pass
    return inserted


def get_participants(reservation_id: str) -> list[dict]:
    """
    Retourne la liste des participants d'une réservation
    (avec les infos utilisateur : nom, promo, etc.).
    N'inclut PAS le réservant principal (table users via reservation.user).
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT u.username, u.nom_complet, u.promotion, u.numero_etudiant
               FROM reservation_participants rp
               JOIN users u ON u.username = rp.username
               WHERE rp.reservation_id = ?
               ORDER BY u.nom_complet""",
            (reservation_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_places_occupees_creneau(nom_salle: str, dt_debut_iso: str, dt_fin_iso: str) -> int:
    """
    Calcule le nombre total de places occupées sur un créneau donné,
    en additionnant les nb_personnes de toutes les réservations confirmées qui se chevauchent.
    Une réservation sans nb_personnes (legacy) compte pour 1 personne.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT nb_personnes FROM reservations
               WHERE salle = ? AND statut = 'confirmee'
                 AND date_debut < ? AND date_fin > ?""",
            (nom_salle, dt_fin_iso, dt_debut_iso)
        ).fetchall()
    total = 0
    for r in rows:
        total += (r["nb_personnes"] or 1)
    return total


def get_reservations_creneau(nom_salle: str, dt_debut_iso: str, dt_fin_iso: str) -> list[dict]:
    """
    Retourne toutes les réservations confirmées qui se chevauchent avec le créneau donné.
    Utilisé pour calculer les places restantes et les groupes en présence.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM reservations
               WHERE salle = ? AND statut = 'confirmee'
                 AND date_debut < ? AND date_fin > ?
               ORDER BY date_debut""",
            (nom_salle, dt_fin_iso, dt_debut_iso)
        ).fetchall()
    return [_row_to_resa(r) for r in rows]
    """
    Recherche des utilisateurs actifs par nom complet, username ou numéro étudiant.
    Utilisé pour la recherche de participants lors d'une réservation.

    - query          : terme de recherche (minimum 2 caractères recommandé)
    - exclude_username : exclure cet utilisateur des résultats (ex: le réservant)
    - limit          : nombre max de résultats
    """
    q = f"%{query.strip()}%"
    with get_conn() as conn:
        if exclude_username:
            rows = conn.execute(
                """SELECT username, nom_complet, promotion, numero_etudiant
                   FROM users
                   WHERE actif = 1
                     AND role = 'etudiant'
                     AND username != ?
                     AND (
                         nom_complet      LIKE ? COLLATE NOCASE
                      OR username         LIKE ? COLLATE NOCASE
                      OR numero_etudiant  LIKE ? COLLATE NOCASE
                     )
                   ORDER BY nom_complet
                   LIMIT ?""",
                (exclude_username, q, q, q, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT username, nom_complet, promotion, numero_etudiant
                   FROM users
                   WHERE actif = 1
                     AND role = 'etudiant'
                     AND (
                         nom_complet      LIKE ? COLLATE NOCASE
                      OR username         LIKE ? COLLATE NOCASE
                      OR numero_etudiant  LIKE ? COLLATE NOCASE
                     )
                   ORDER BY nom_complet
                   LIMIT ?""",
                (q, q, q, limit)
            ).fetchall()
        return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# BLOCAGES
# ══════════════════════════════════════════════════════════════════════════════

def get_blocages_actifs(nom_salle: str | None = None) -> list[dict]:
    now_iso = datetime.now().astimezone().isoformat()
    with get_conn() as conn:
        if nom_salle:
            rows = conn.execute(
                "SELECT * FROM blocages WHERE date_fin > ? AND salle = ? ORDER BY date_debut",
                (now_iso, nom_salle)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM blocages WHERE date_fin > ? ORDER BY date_debut",
                (now_iso,)
            ).fetchall()
        return [dict(r) for r in rows]


def add_blocage(id_, salle, date_debut, date_fin, motif, cree_par, date_creation) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO blocages
               (id, salle, date_debut, date_fin, motif, cree_par, date_creation)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (id_, salle, date_debut, date_fin, motif, cree_par, date_creation)
        )


def delete_blocage(blocage_id) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM blocages WHERE id = ?", (blocage_id,))
        return cur.rowcount > 0


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS (signalements)
# ══════════════════════════════════════════════════════════════════════════════

STATUT_LABELS   = {"nouveau": "🔔 Nouveau", "en_cours": "🔧 En cours", "resolu": "✅ Résolu"}
PRIORITE_LABELS = {"basse": "🟢 Basse", "normale": "🔵 Normale", "haute": "🟠 Haute", "urgente": "🔴 Urgente"}


def _fmt_iso(raw: str | None) -> str | None:
    """Formate une date ISO 8601 en 'dd/mm à HH:MM' ; renvoie la valeur brute si non parsable."""
    if not raw:
        return raw
    try:
        return datetime.fromisoformat(raw).strftime("%d/%m à %H:%M")
    except (ValueError, TypeError):
        return raw


def _row_to_report(row) -> dict:
    d = dict(row)
    d["desc"] = d.get("description", "")
    # Formatage lisible de la date pour les templates
    # Gère les deux formats : ISO 8601 (nouveau) et "dd/mm à HH:MM" (legacy)
    d["date"] = _fmt_iso(d.get("date_creation", ""))
    if d.get("date_maj"):
        d["date_maj"] = _fmt_iso(d["date_maj"])
    d.setdefault("priorite", "normale")
    d["priorite_label"] = PRIORITE_LABELS.get(d["priorite"], d["priorite"])
    d["statut_label"]   = STATUT_LABELS.get(d.get("statut"), d.get("statut"))
    return d


def _enrich_report_names(d: dict) -> dict:
    """Ajoute site_nom / admin_assigne_nom à un dict de ticket déjà construit."""
    if d.get("site_id"):
        site = get_site(d["site_id"])
        d["site_nom"] = site["nom"] if site else None
    else:
        d["site_nom"] = None
    if d.get("admin_assigne"):
        u = get_user(d["admin_assigne"])
        d["admin_assigne_nom"] = u["nom_complet"] if u else d["admin_assigne"]
    else:
        d["admin_assigne_nom"] = None
    return d


def get_reports_salle(nom_salle: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE salle = ? ORDER BY id DESC",
            (nom_salle,)
        ).fetchall()
        return [_row_to_report(r) for r in rows]


def get_all_reports(site_id: int | None = None) -> dict:
    """
    Retourne les signalements groupés par salle.
    Si site_id est fourni (admin de site), ne renvoie que les tickets de ce site.
    """
    with get_conn() as conn:
        if site_id is None:
            rows = conn.execute("SELECT * FROM reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reports WHERE site_id = ? ORDER BY id DESC", (site_id,)
            ).fetchall()
    result = {}
    for r in rows:
        d = _row_to_report(r)
        result.setdefault(d["salle"], []).append(d)
    return result


def get_report(report_id) -> dict | None:
    """Retourne un ticket unique, enrichi (site, admin assigné), ou None."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if not row:
        return None
    d = _row_to_report(row)
    return _enrich_report_names(d)


def _log_report_event(conn, report_id, type_evenement, auteur, ancienne_valeur=None,
                       nouvelle_valeur=None, detail=None, date_iso=None) -> None:
    conn.execute(
        """INSERT INTO report_events
           (report_id, type_evenement, ancienne_valeur, nouvelle_valeur, detail, auteur, date_creation)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (report_id, type_evenement, ancienne_valeur, nouvelle_valeur, detail, auteur,
         date_iso or datetime.now().isoformat())
    )


def get_admins_for_site(site_id: int | None, only_active: bool = True) -> list[dict]:
    """Retourne les comptes admin rattachés à un site (admins de site). site_id=None → []."""
    if site_id is None:
        return []
    with get_conn() as conn:
        q = "SELECT * FROM users WHERE role = 'admin' AND site_id = ?"
        params = [site_id]
        if only_active:
            q += " AND actif = 1"
        rows = conn.execute(q + " ORDER BY nom_complet", params).fetchall()
        return [dict(r) for r in rows]


def add_report(nom_salle, type_pb, description, auteur="Anonyme", priorite="normale") -> int:
    """Crée un ticket, l'affecte automatiquement au site de la salle et,
    si un seul admin de site est actif pour ce site, l'assigne directement à lui.
    Retourne l'id du ticket créé."""
    if priorite not in PRIORITE_LABELS:
        priorite = "normale"
    date_iso = datetime.now().isoformat()
    salle_row = get_salle(nom_salle)
    site_id = salle_row.get("site_id") if salle_row else None

    admins_site = get_admins_for_site(site_id) if site_id else []
    admin_assigne = admins_site[0]["username"] if len(admins_site) == 1 else None

    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reports
               (salle, type, description, auteur, statut, priorite, site_id, admin_assigne, date_creation)
               VALUES (?, ?, ?, ?, 'nouveau', ?, ?, ?, ?)""",
            (nom_salle, type_pb, description[:500].strip(), auteur, priorite, site_id, admin_assigne, date_iso)
        )
        report_id = cur.lastrowid
        _log_report_event(conn, report_id, "creation", auteur,
                           nouvelle_valeur="nouveau", detail=f"Signalement créé ({type_pb})", date_iso=date_iso)
        if admin_assigne:
            _log_report_event(conn, report_id, "assignation", "system",
                               nouvelle_valeur=admin_assigne,
                               detail="Affectation automatique (unique admin du site)", date_iso=date_iso)

    if admin_assigne:
        create_notification(
            username=admin_assigne, type='ticket',
            titre=f"Nouveau ticket — Salle {nom_salle}",
            message=f"{type_pb} · {description[:80]}",
        )
    return report_id


def delete_report(nom_salle, report_id) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM reports WHERE id = ? AND salle = ?", (report_id, nom_salle)
        )
        return cur.rowcount > 0


def update_report_statut(report_id, nouveau_statut, note_admin="", acteur="system") -> bool:
    date_maj = datetime.now().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT statut FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            return False
        ancien_statut = row["statut"]
        cur = conn.execute(
            "UPDATE reports SET statut = ?, note_admin = ?, date_maj = ? WHERE id = ?",
            (nouveau_statut, note_admin or None, date_maj, report_id)
        )
        if cur.rowcount > 0 and ancien_statut != nouveau_statut:
            _log_report_event(conn, report_id, "statut", acteur,
                               ancienne_valeur=ancien_statut, nouvelle_valeur=nouveau_statut,
                               detail=note_admin or None, date_iso=date_maj)
        elif cur.rowcount > 0 and note_admin:
            _log_report_event(conn, report_id, "commentaire", acteur, detail=note_admin, date_iso=date_maj)
        return cur.rowcount > 0


def update_report_priorite(report_id, nouvelle_priorite, acteur="system") -> bool:
    if nouvelle_priorite not in PRIORITE_LABELS:
        return False
    date_maj = datetime.now().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT priorite FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            return False
        ancienne = row["priorite"]
        cur = conn.execute(
            "UPDATE reports SET priorite = ?, date_maj = ? WHERE id = ?",
            (nouvelle_priorite, date_maj, report_id)
        )
        if cur.rowcount > 0 and ancienne != nouvelle_priorite:
            _log_report_event(conn, report_id, "priorite", acteur,
                               ancienne_valeur=ancienne, nouvelle_valeur=nouvelle_priorite, date_iso=date_maj)
        return cur.rowcount > 0


def assign_report(report_id, admin_username: str | None, acteur="system") -> bool:
    """Affecte (ou désaffecte si None/'') un ticket à un admin. Notifie le nouvel admin."""
    admin_username = admin_username or None
    date_maj = datetime.now().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT admin_assigne, salle FROM reports WHERE id = ?", (report_id,)).fetchone()
        if not row:
            return False
        ancien = row["admin_assigne"]
        cur = conn.execute(
            "UPDATE reports SET admin_assigne = ?, date_maj = ? WHERE id = ?",
            (admin_username, date_maj, report_id)
        )
        if cur.rowcount > 0 and ancien != admin_username:
            _log_report_event(conn, report_id, "assignation", acteur,
                               ancienne_valeur=ancien, nouvelle_valeur=admin_username, date_iso=date_maj)
        salle = row["salle"]
    if cur.rowcount > 0 and admin_username and admin_username != ancien:
        create_notification(
            username=admin_username, type='ticket',
            titre=f"Ticket affecté — Salle {salle}",
            message=f"Ce signalement vous a été affecté par {acteur}.",
        )
    return cur.rowcount > 0


def get_report_events(report_id) -> list[dict]:
    """Frise chronologique complète d'un ticket, triée du plus ancien au plus récent."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM report_events WHERE report_id = ? ORDER BY id ASC", (report_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["date_fmt"] = _fmt_iso(d["date_creation"])
        out.append(d)
    return out


def add_report_comment(report_id, auteur, texte) -> int:
    texte = (texte or "").strip()[:2000]
    date_iso = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO report_comments (report_id, auteur, texte, date_creation)
               VALUES (?, ?, ?, ?)""",
            (report_id, auteur, texte, date_iso)
        )
        _log_report_event(conn, report_id, "commentaire", auteur, detail=texte[:120], date_iso=date_iso)
        return cur.lastrowid


def update_report_comment(comment_id, texte, acteur="system") -> bool:
    texte = (texte or "").strip()[:2000]
    date_maj = datetime.now().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT report_id FROM report_comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            return False
        cur = conn.execute(
            "UPDATE report_comments SET texte = ?, date_maj = ? WHERE id = ?",
            (texte, date_maj, comment_id)
        )
        if cur.rowcount > 0:
            _log_report_event(conn, row["report_id"], "commentaire_modifie", acteur,
                               detail=texte[:120], date_iso=date_maj)
        return cur.rowcount > 0


def delete_report_comment(comment_id, acteur="system") -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT report_id FROM report_comments WHERE id = ?", (comment_id,)).fetchone()
        if not row:
            return False
        cur = conn.execute("DELETE FROM report_comments WHERE id = ?", (comment_id,))
        if cur.rowcount > 0:
            _log_report_event(conn, row["report_id"], "commentaire_supprime", acteur)
        return cur.rowcount > 0


def get_report_comments(report_id) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM report_comments WHERE report_id = ? ORDER BY id ASC", (report_id,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["date_fmt"] = _fmt_iso(d["date_creation"])
        if d.get("date_maj"):
            d["date_maj_fmt"] = _fmt_iso(d["date_maj"])
        out.append(d)
    return out


def get_report_full(report_id) -> dict | None:
    """Ticket complet : infos + commentaires + frise chronologique + salles/admins assignables."""
    report = get_report(report_id)
    if not report:
        return None
    report["comments"] = get_report_comments(report_id)
    report["events"]   = get_report_events(report_id)
    report["admins_assignables"] = [
        {"username": a["username"], "nom_complet": a["nom_complet"], "site_id": a.get("site_id")}
        for a in get_admins_for_site(report.get("site_id"))
    ]
    return report


def get_reports_assignment_overview() -> list[dict]:
    """
    Vue d'ensemble par site pour le super-admin : pour chaque site, la liste des
    admins qui y sont rattachés avec le nombre de tickets ouverts/résolus qui
    leur sont affectés, ainsi que le nombre de tickets non affectés du site.
    """
    with get_conn() as conn:
        sites_rows = conn.execute("SELECT * FROM sites ORDER BY nom").fetchall()
        report_rows = conn.execute("SELECT * FROM reports").fetchall()
        admin_rows = conn.execute(
            "SELECT * FROM users WHERE role = 'admin' AND site_id IS NOT NULL ORDER BY nom_complet"
        ).fetchall()

    reports_by_site = {}
    for r in report_rows:
        reports_by_site.setdefault(r["site_id"], []).append(dict(r))

    admins_by_site = {}
    for a in admin_rows:
        admins_by_site.setdefault(a["site_id"], []).append(dict(a))

    overview = []
    for s in sites_rows:
        site = dict(s)
        site_reports = reports_by_site.get(site["id"], [])
        ouverts = [r for r in site_reports if r["statut"] != "resolu"]
        admins_list = []
        for a in admins_by_site.get(site["id"], []):
            uname = a["username"]
            assignes = [r for r in site_reports if r["admin_assigne"] == uname]
            admins_list.append({
                "username":    uname,
                "nom_complet": a["nom_complet"],
                "actif":       bool(a.get("actif", 1)),
                "nb_ouverts":  len([r for r in assignes if r["statut"] != "resolu"]),
                "nb_total":    len(assignes),
            })
        non_affectes = len([r for r in ouverts if not r["admin_assigne"]])
        overview.append({
            "site_id":       site["id"],
            "site_nom":      site["nom"],
            "couleur":       site.get("couleur", "#0a5ad2"),
            "admins":        admins_list,
            "nb_tickets_ouverts": len(ouverts),
            "nb_non_affectes":    non_affectes,
        })

    # Tickets orphelins (salle sans site) — affichés à part
    orphelins = reports_by_site.get(None, [])
    orphelins_ouverts = len([r for r in orphelins if r["statut"] != "resolu"])

    return {"sites": overview, "orphelins_ouverts": orphelins_ouverts}


# ══════════════════════════════════════════════════════════════════════════════
# PHOTOS DE SALLES
# ══════════════════════════════════════════════════════════════════════════════

def get_photos_salle(nom_salle: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM salle_photos WHERE salle = ? ORDER BY ordre ASC, id ASC",
            (nom_salle,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_photo_salle(nom_salle: str, filename: str, legende: str = "", ordre: int = 0) -> int:
    date_str = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO salle_photos (salle, filename, legende, ordre, date_ajout)
               VALUES (?, ?, ?, ?, ?)""",
            (nom_salle, filename, legende[:200], ordre, date_str)
        )
        return cur.lastrowid


def update_photo_salle(photo_id: int, legende: str = None, ordre: int = None) -> bool:
    with get_conn() as conn:
        if legende is not None:
            conn.execute("UPDATE salle_photos SET legende = ? WHERE id = ?", (legende[:200], photo_id))
        if ordre is not None:
            conn.execute("UPDATE salle_photos SET ordre = ? WHERE id = ?", (ordre, photo_id))
        return True


def delete_photo_salle(photo_id: int, nom_salle: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT filename FROM salle_photos WHERE id = ? AND salle = ?",
            (photo_id, nom_salle)
        ).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM salle_photos WHERE id = ?", (photo_id,))
        return row["filename"]


# ══════════════════════════════════════════════════════════════════════════════
# DEMANDES DE CHANGEMENT DE MOT DE PASSE
# ══════════════════════════════════════════════════════════════════════════════

def create_pwd_request(username: str, motif: str = "") -> bool:
    """Crée une demande de changement de MDP. Retourne False si une demande est déjà en attente."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM pwd_requests WHERE username = ? AND statut = 'en_attente'",
            (username,)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO pwd_requests (username, motif, statut, date_creation) VALUES (?, ?, 'en_attente', ?)",
            (username, motif[:300].strip(), datetime.now().isoformat())
        )
        return True


def get_pwd_requests(statut: str = None) -> list[dict]:
    """Retourne les demandes de MDP, filtrées par statut si fourni."""
    with get_conn() as conn:
        if statut:
            rows = conn.execute(
                "SELECT r.*, u.nom_complet, u.promotion FROM pwd_requests r "
                "JOIN users u ON u.username = r.username "
                "WHERE r.statut = ? ORDER BY r.date_creation DESC",
                (statut,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT r.*, u.nom_complet, u.promotion FROM pwd_requests r "
                "JOIN users u ON u.username = r.username "
                "ORDER BY r.date_creation DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_pwd_request_user(username: str) -> dict | None:
    """Retourne la demande en attente d'un utilisateur, ou None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pwd_requests WHERE username = ? AND statut = 'en_attente' ORDER BY date_creation DESC LIMIT 1",
            (username,)
        ).fetchone()
        return dict(row) if row else None


def process_pwd_request(request_id: int, statut: str, traite_par: str, new_password: str = None) -> bool:
    """Approuve ou refuse une demande. Si approuvée, met à jour le mot de passe."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        row = conn.execute("SELECT username FROM pwd_requests WHERE id = ?", (request_id,)).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE pwd_requests SET statut = ?, traite_par = ?, date_traitement = ? WHERE id = ?",
            (statut, traite_par, now, request_id)
        )
        if statut == "approuvee" and new_password:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (generate_password_hash(new_password), row["username"])
            )
    return True


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def create_notification(username: str, type: str, titre: str,
                        message: str = '', lien: str = None) -> int:
    """
    Crée une notification pour un utilisateur.
    Retourne l'id de la notification créée.
    Types : 'reservation' | 'annulation' | 'invitation' | 'annonce' | 'reset_pwd' | 'system'
    """
    now = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO notifications (username, type, titre, message, lien, lu, date_envoi)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (username, type, titre, message, lien, now)
        )
        return cur.lastrowid


def create_notifications_bulk(usernames: list[str], type: str, titre: str,
                               message: str = '', lien: str = None) -> int:
    """Crée la même notification pour une liste d'utilisateurs. Retourne le nombre créé."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.executemany(
            """INSERT INTO notifications (username, type, titre, message, lien, lu, date_envoi)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            [(u, type, titre, message, lien, now) for u in usernames]
        )
        return len(usernames)


def get_notifications(username: str, limit: int = 30) -> list[dict]:
    """Retourne les notifications d'un utilisateur (les plus récentes en premier)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM notifications WHERE username = ?
               ORDER BY date_envoi DESC LIMIT ?""",
            (username, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_unread_count(username: str) -> int:
    """Retourne le nombre de notifications non lues."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE username = ? AND lu = 0",
            (username,)
        ).fetchone()
        return row[0] if row else 0


def mark_notification_read(notif_id: int, username: str) -> bool:
    """Marque une notification comme lue (vérifie l'appartenance)."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notifications SET lu = 1 WHERE id = ? AND username = ?",
            (notif_id, username)
        )
        return cur.rowcount > 0


def mark_all_read(username: str) -> int:
    """Marque toutes les notifications d'un utilisateur comme lues."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE notifications SET lu = 1 WHERE username = ? AND lu = 0",
            (username,)
        )
        return cur.rowcount


def delete_old_notifications(days: int = 30) -> int:
    """Supprime les notifications lues de plus de N jours. Retourne le nombre supprimé."""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM notifications WHERE lu = 1 AND date_envoi < ?",
            (cutoff,)
        )
        return cur.rowcount


# ══════════════════════════════════════════════════════════════════════════════
# RÉINITIALISATION DE MOT DE PASSE PAR TOKEN
# ══════════════════════════════════════════════════════════════════════════════

def create_reset_token(username: str) -> str:
    """
    Génère un token unique de réinitialisation valable 30 minutes.
    Invalide les tokens précédents non utilisés pour cet utilisateur.
    Retourne le token (UUID4).
    """
    import uuid
    from datetime import timedelta
    now = datetime.now()
    expires = (now + timedelta(minutes=30)).isoformat()
    token = str(uuid.uuid4())
    with get_conn() as conn:
        # Invalider les anciens tokens en attente
        conn.execute(
            "UPDATE password_reset_tokens SET used = 1 WHERE username = ? AND used = 0",
            (username,)
        )
        conn.execute(
            """INSERT INTO password_reset_tokens (username, token, expires, used, date_creation)
               VALUES (?, ?, ?, 0, ?)""",
            (username, token, expires, now.isoformat())
        )
    return token


def get_reset_token(token: str) -> dict | None:
    """
    Vérifie et retourne un token valide (non utilisé, non expiré).
    Retourne None si invalide.
    """
    now = datetime.now().isoformat()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT * FROM password_reset_tokens
               WHERE token = ? AND used = 0 AND expires > ?""",
            (token, now)
        ).fetchone()
        return dict(row) if row else None


def consume_reset_token(token: str, new_password: str) -> bool:
    """
    Consomme un token valide : met à jour le mot de passe et marque le token utilisé.
    Retourne True si succès, False si token invalide/expiré.
    """
    row = get_reset_token(token)
    if not row:
        return False
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = ?",
            (generate_password_hash(new_password), row["username"])
        )
        conn.execute(
            "UPDATE password_reset_tokens SET used = 1 WHERE token = ?",
            (token,)
        )
    return True


def set_must_change_password(username: str, value: bool = True) -> bool:
    """Active ou désactive le flag de changement forcé au prochain login."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET must_change_password = ? WHERE username = ?",
            (int(value), username)
        )
        return cur.rowcount > 0


def admin_reset_password(username: str, new_password: str, force_change: bool = True) -> bool:
    """
    Réinitialise le mot de passe d'un utilisateur (action admin).
    Si force_change=True, l'utilisateur devra changer son mdp à sa prochaine connexion.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = ? WHERE username = ?",
            (generate_password_hash(new_password), int(force_change), username)
        )
        return cur.rowcount > 0


# ══════════════════════════════════════════════════════════════════════════════
# ANNONCES (e-mails de masse admin)
# ══════════════════════════════════════════════════════════════════════════════

def get_users_by_cible(cible_type: str, cible_value: str | None = None) -> list[dict]:
    """
    Retourne la liste des utilisateurs actifs correspondant à la cible choisie.

    cible_type   | cible_value
    -------------|--------------------------------------------
    'tous'       | None  → tous les étudiants actifs
    'promotion'  | "BUT RT 1"  → étudiants de cette promo
    'promotions' | "BUT RT 1,BUT RT 2"  → plusieurs promos (séparées par virgule)
    'utilisateurs'| "alice,bob"  → usernames spécifiques (séparés par virgule)
    """
    with get_conn() as conn:
        if cible_type == 'tous':
            rows = conn.execute(
                "SELECT * FROM users WHERE actif = 1 AND role = 'etudiant' ORDER BY nom_complet"
            ).fetchall()
        elif cible_type == 'promotion' and cible_value:
            rows = conn.execute(
                "SELECT * FROM users WHERE actif = 1 AND role = 'etudiant' AND promotion = ? ORDER BY nom_complet",
                (cible_value,)
            ).fetchall()
        elif cible_type == 'promotions' and cible_value:
            promos = [p.strip() for p in cible_value.split(',') if p.strip()]
            placeholders = ','.join('?' * len(promos))
            rows = conn.execute(
                f"SELECT * FROM users WHERE actif = 1 AND role = 'etudiant' AND promotion IN ({placeholders}) ORDER BY nom_complet",
                promos
            ).fetchall()
        elif cible_type == 'utilisateurs' and cible_value:
            usernames = [u.strip() for u in cible_value.split(',') if u.strip()]
            placeholders = ','.join('?' * len(usernames))
            rows = conn.execute(
                f"SELECT * FROM users WHERE actif = 1 AND username IN ({placeholders}) ORDER BY nom_complet",
                usernames
            ).fetchall()
        else:
            rows = []
        return [dict(r) for r in rows]


def save_annonce(sujet: str, corps: str, cible_type: str, cible_value: str | None,
                 nb_destinataires: int, envoye_par: str, statut: str = 'envoyee') -> int:
    """Enregistre une annonce envoyée dans l'historique. Retourne l'id inséré."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO annonces
               (sujet, corps, cible_type, cible_value, nb_destinataires, envoye_par, date_envoi, statut)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sujet, corps, cible_type, cible_value, nb_destinataires, envoye_par, now, statut)
        )
        return cur.lastrowid


def get_annonces(limit: int = 50) -> list[dict]:
    """Retourne l'historique des annonces envoyées (les plus récentes en premier)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM annonces ORDER BY date_envoi DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════════════════
# MIGRATION DEPUIS L'ANCIEN SYSTÈME
# ══════════════════════════════════════════════════════════════════════════════

def migrate_from_legacy(
    config_json_path="config.json",
    salleics_folder="salleICS/",
    reservations_path="reservations.json",
    blocages_path="blocages.json",
    reports_path="reports.json",
):
    """
    Migration complète depuis l'ancien système (fichiers ICS + config.json + JSON).

    Importe :
      1. Les salles depuis le dossier salleICS/ + config.json → table salles
      2. Les réservations JSON → table reservations
      3. Les blocages JSON → table blocages
      4. Les signalements JSON → table reports

    À lancer UNE SEULE FOIS :
        python database.py migrate
    """
    import json

    print("=== Migration legacy → SQLite ===")
    init_db()
    now = datetime.now().isoformat()

    # ── 1. Salles depuis dossier ICS + config.json ──
    config = {}
    if os.path.exists(config_json_path):
        try:
            with open(config_json_path, encoding="utf-8") as f:
                config = json.load(f)
            print(f"  📄 config.json chargé : {len(config)} entrées")
        except Exception as e:
            print(f"  ⚠ config.json illisible : {e}")

    salles_migrees = 0
    if os.path.exists(salleics_folder):
        for fname in os.listdir(salleics_folder):
            if not fname.lower().endswith('.ics'):
                continue
            nom = fname.replace('.ics', '').replace('.ICS', '')
            cfg = config.get(nom, {})
            # Convertir places en entier ou NULL
            places_raw = cfg.get("places")
            try:
                places_int = int(places_raw) if places_raw and str(places_raw) != "?" else None
            except (ValueError, TypeError):
                places_int = None
            try:
                with get_conn() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO salles
                           (nom, nom_complet, places, pc, projecteur, tableau, description,
                            etage, aile, ical_url, ical_file, actif, date_creation, date_maj)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                        (
                            nom,
                            cfg.get("nom_complet", f"Salle {nom}"),
                            places_int,
                            int(bool(cfg.get("pc", False))),
                            int(bool(cfg.get("projecteur", False))),
                            int(bool(cfg.get("tableau", True))),
                            cfg.get("description", ""),
                            str(cfg.get("etage", "0")),
                            cfg.get("aile", "centre"),
                            _encrypt_url(cfg["ical_url"]) if cfg.get("ical_url") else None,
                            fname,
                            now, now
                        )
                    )
                salles_migrees += 1
            except Exception as e:
                print(f"  ⚠ Salle {nom} ignorée : {e}")
    print(f"  ✅ {salles_migrees} salles importées depuis {salleics_folder}")

    # ── 2. Réservations ──
    if os.path.exists(reservations_path):
        with open(reservations_path, encoding="utf-8") as f:
            resas = json.load(f)
        count = 0
        for r in resas:
            try:
                with get_conn() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO reservations
                           (id, salle, user, motif, date_debut, date_fin, date_creation,
                            statut, cree_par_admin, annulee_par, date_annulation)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (r["id"], r["salle"], r["user"], r.get("motif", ""),
                         r["date_debut"], r["date_fin"], r["date_creation"],
                         r.get("statut", "confirmee"), int(r.get("cree_par_admin", False)),
                         r.get("annulee_par"), r.get("date_annulation"))
                    )
                count += 1
            except Exception as e:
                print(f"  ⚠ Réservation {r.get('id')} ignorée : {e}")
        print(f"  ✅ {count} réservations importées")

    # ── 3. Blocages ──
    if os.path.exists(blocages_path):
        with open(blocages_path, encoding="utf-8") as f:
            blocages = json.load(f)
        count = 0
        for b in blocages:
            try:
                with get_conn() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO blocages
                           (id, salle, date_debut, date_fin, motif, cree_par, date_creation)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (b["id"], b["salle"], b["date_debut"], b["date_fin"],
                         b.get("motif", ""), b.get("cree_par", "admin"), b.get("date_creation", ""))
                    )
                count += 1
            except Exception as e:
                print(f"  ⚠ Blocage {b.get('id')} ignoré : {e}")
        print(f"  ✅ {count} blocages importés")

    # ── 4. Reports ──
    if os.path.exists(reports_path):
        with open(reports_path, encoding="utf-8") as f:
            reports = json.load(f)
        current_year = datetime.now().year
        count = 0
        for salle, liste in reports.items():
            for rep in liste:
                try:
                    # Convertir les dates legacy "dd/mm à HH:MM" → ISO 8601
                    raw_date = rep.get("date", "")
                    try:
                        dt = datetime.strptime(f"{raw_date} {current_year}", "%d/%m à %H:%M %Y")
                        date_iso = dt.isoformat()
                    except (ValueError, TypeError):
                        date_iso = raw_date  # déjà ISO ou format inconnu

                    raw_maj = rep.get("date_maj")
                    date_maj_iso = None
                    if raw_maj:
                        try:
                            dt_maj = datetime.strptime(f"{raw_maj} {current_year}", "%d/%m à %H:%M %Y")
                            date_maj_iso = dt_maj.isoformat()
                        except (ValueError, TypeError):
                            date_maj_iso = raw_maj

                    note = rep.get("note_admin")
                    if note == "None":
                        note = None

                    with get_conn() as conn:
                        conn.execute(
                            """INSERT INTO reports
                               (salle, type, description, auteur, statut, note_admin, date_creation, date_maj)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (salle, rep.get("type", ""), rep.get("desc", ""),
                             rep.get("auteur", "Anonyme"), rep.get("statut", "nouveau"),
                             note, date_iso, date_maj_iso)
                        )
                    count += 1
                except Exception as e:
                    print(f"  ⚠ Report salle {salle} ignoré : {e}")
        print(f"  ✅ {count} signalements importés")

    print("=== Migration terminée ===")
    print(f"Base : {DB_PATH}")
    print("Vous pouvez maintenant supprimer config.json et le dossier salleICS/ si les URL iCal sont configurées.")


# ══════════════════════════════════════════════════════════════════════════════
# PARAMÈTRES GLOBAUX (table settings)
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(cle: str, default: str = "") -> str:
    """Retourne la valeur d'un paramètre par sa clé, ou default si absent."""
    with get_conn() as conn:
        row = conn.execute("SELECT valeur FROM settings WHERE cle = ?", (cle,)).fetchone()
        return row["valeur"] if row else default


def get_settings_by_categorie(categorie: str) -> dict:
    """Retourne tous les paramètres d'une catégorie sous forme {cle: valeur}."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT cle, valeur FROM settings WHERE categorie = ? ORDER BY cle",
            (categorie,)
        ).fetchall()
        return {r["cle"]: r["valeur"] for r in rows}


def get_all_settings() -> dict:
    """Retourne tous les paramètres sous forme {cle: {valeur, categorie, date_maj}}."""
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM settings ORDER BY categorie, cle").fetchall()
        return {r["cle"]: dict(r) for r in rows}


def set_setting(cle: str, valeur: str, categorie: str = "app") -> None:
    """Crée ou met à jour un paramètre."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO settings (cle, valeur, categorie, date_maj)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur, date_maj = excluded.date_maj""",
            (cle, str(valeur) if valeur is not None else "", categorie, now)
        )


def set_settings_bulk(settings_dict: dict, categorie: str = "app") -> None:
    """Met à jour plusieurs paramètres d'un coup. Dict {cle: valeur}."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        for cle, valeur in settings_dict.items():
            conn.execute(
                """INSERT INTO settings (cle, valeur, categorie, date_maj)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur, date_maj = excluded.date_maj""",
                (cle, str(valeur) if valeur is not None else "", categorie, now)
            )


# ══════════════════════════════════════════════════════════════════════════════
# PROMOTIONS (table promotions)
# ══════════════════════════════════════════════════════════════════════════════

def get_all_promotions(include_inactive: bool = False) -> list[dict]:
    """Retourne toutes les promotions triées par ordre puis nom."""
    with get_conn() as conn:
        if include_inactive:
            rows = conn.execute(
                "SELECT * FROM promotions ORDER BY ordre ASC, nom ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM promotions WHERE actif = 1 ORDER BY ordre ASC, nom ASC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_promotions_list(include_inactive: bool = False) -> list[str]:
    """Retourne la liste des noms de promotions actives (pour les formulaires)."""
    return [p["nom"] for p in get_all_promotions(include_inactive=include_inactive)]


def create_promotion(nom: str, ordre: int = None) -> bool:
    """Crée une promotion. Retourne False si le nom existe déjà."""
    now = datetime.now().isoformat()
    with get_conn() as conn:
        if ordre is None:
            row = conn.execute("SELECT MAX(ordre) FROM promotions").fetchone()
            ordre = (row[0] or 0) + 1
        try:
            conn.execute(
                "INSERT INTO promotions (nom, ordre, actif, date_creation) VALUES (?, ?, 1, ?)",
                (nom.strip(), ordre, now)
            )
            return True
        except Exception:
            return False  # nom déjà pris (UNIQUE)


def update_promotion(promo_id: int, nom: str = None, ordre: int = None, actif: int = None) -> bool:
    """Met à jour une promotion. Retourne False si introuvable."""
    with get_conn() as conn:
        if nom is not None:
            conn.execute("UPDATE promotions SET nom = ? WHERE id = ?", (nom.strip(), promo_id))
        if ordre is not None:
            conn.execute("UPDATE promotions SET ordre = ? WHERE id = ?", (ordre, promo_id))
        if actif is not None:
            conn.execute("UPDATE promotions SET actif = ? WHERE id = ?", (int(actif), promo_id))
        return True


def toggle_promotion_actif(promo_id: int) -> bool | None:
    """Bascule actif/inactif. Retourne le nouvel état ou None si introuvable."""
    with get_conn() as conn:
        row = conn.execute("SELECT actif FROM promotions WHERE id = ?", (promo_id,)).fetchone()
        if not row:
            return None
        new_val = 0 if row["actif"] else 1
        conn.execute("UPDATE promotions SET actif = ? WHERE id = ?", (new_val, promo_id))
        return bool(new_val)


def delete_promotion(promo_id: int) -> bool:
    """Supprime une promotion. Retourne False si introuvable."""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM promotions WHERE id = ?", (promo_id,))
        return cur.rowcount > 0


def reorder_promotions(ordered_ids: list[int]) -> None:
    """Met à jour l'ordre de toutes les promotions d'un coup (liste d'IDs ordonnés)."""
    with get_conn() as conn:
        for i, pid in enumerate(ordered_ids):
            conn.execute("UPDATE promotions SET ordre = ? WHERE id = ?", (i, pid))


# ── Point d'entrée CLI ────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        migrate_from_legacy()
    else:
        print(f"Base initialisée : {DB_PATH}")
        print("Pour migrer l'ancien système : python database.py migrate")