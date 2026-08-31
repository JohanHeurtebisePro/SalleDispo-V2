"""
PROJET : SalleDispo
DESCRIPTION :
    Application Flask de gestion de salles en temps réel.
    Les salles sont gérées 100% en base de données (table salles).
    Plus de config.json ni de dossier salleICS/ obligatoire.
    L'admin peut ajouter / modifier / supprimer des salles depuis l'interface.

CHANGELOG :
    - [REFACTOR] Migration complète vers DB : table salles = source de vérité unique
    - [FEATURE] Admin : ajout / suppression de salles depuis l'interface
    - [FEATURE] Admin : toggle actif/inactif d'une salle
    - [FEATURE] Rate limiting sur /login : 5 tentatives / 5 min par IP
    - Conservation de toutes les fonctionnalités existantes
"""
from dotenv import load_dotenv
load_dotenv("mail.env")     # Config SMTP non-sensible
load_dotenv("secrets.env")  # Secrets : SMTP_PASSWORD, FLASK_SECRET_KEY

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, abort, Response, session
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from urllib.parse import urlsplit
from icalendar import Calendar
from datetime import datetime, timedelta
from functools import wraps
import pytz
import os
import csv
import io
import locale
import re
import time
import uuid
import requests

import database as db
from ics_crypto import mask_url_for_display
from email_service import send_reservation_confirmation, send_annonce, send_password_reset, send_password_changed_by_admin

app = Flask(__name__)

app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'cle_par_defaut_dev_a_changer_en_prod')
DEBUG_MODE = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'


# =========================================================
# 🔒 HELPER : validation des redirections post-login
# =========================================================

def is_safe_url(target):
    """
    Vérifie qu une URL de redirection est interne à l application.
    Protège contre les open redirects du type /login?next=https://evil.com

    Règles :
      - None ou vide    → False (on utilisera la route par défaut)
      - scheme ou netloc présents → URL absolue externe → rejeté
      - chemin relatif ou absolu sans host → accepté
    """
    if not target:
        return False
    parsed = urlsplit(target)
    # Une URL sûre n a ni scheme (http/https) ni netloc (evil.com)
    return parsed.scheme == '' and parsed.netloc == ''


# ── Niveau 3 : Filtre des données ICS ────────────────────────────────────────
# ICS_SUMMARY_MODE=filtered → affiche le nom du cours uniquement, sans enseignant ni groupe (défaut)
# ICS_SUMMARY_MODE=none     → remplace tous les titres par "Cours" (confidentialité maximale)
# ICS_SUMMARY_MODE=full     → affiche le titre ICS complet tel quel (ex: "TD Réseaux – Dupont J.")
ICS_SUMMARY_MODE = os.getenv("ICS_SUMMARY_MODE", "filtered").lower()


def _filter_summary(raw: str) -> str:
    """
    Extrait uniquement le nom du cours depuis un titre ICS ENT.

    Les titres ENT suivent souvent le format :
        "TD Réseaux – Dupont J. (G1A)"
        "CM Mathématiques / Martin P."
        "TP Programmation - Durand (INFO1)"
        "Examen - Algo - Salle 101"

    On garde uniquement la première partie avant les séparateurs
    qui introduisent typiquement le nom de l'enseignant ou du groupe :
        –  (tiret long), —, /, \\ et  -  (tiret court entouré d'espaces)

    Le contenu entre parenthèses (groupe, salle) est également supprimé.
    """
    if not raw:
        return "Cours"

    # Supprimer le contenu entre parenthèses (groupes, salles, codes)
    cleaned = re.sub(r'\s*\([^)]*\)', '', raw)

    # Couper au premier séparateur fort (tiret long, slash, tiret court entouré d'espaces)
    # On ne coupe PAS sur un tiret collé (ex: "TP-INFO") → lookahead espace
    separators = r'\s*[–—]\s*|\s*/\s*|\s+-\s+'
    parts = re.split(separators, cleaned, maxsplit=1)
    titre = parts[0].strip()

    return titre if titre else "Cours"

with app.app_context():
    db.init_db()
    # ── Vérification configuration multi-worker ───────────────────────────────
    # Le cache ICS en mémoire (_ics_cache) n'est pas partagé entre workers.
    # WEB_CONCURRENCY est la variable standard lue par Gunicorn.
    _workers = int(os.getenv("WEB_CONCURRENCY", os.getenv("GUNICORN_WORKERS", "1")))
    if _workers > 1:
        import warnings
        warnings.warn(
            f"[SalleDispo] Gunicorn lancé avec {_workers} workers "
            f"et un cache ICS en mémoire de processus. "
            f"Les invalidations de cache seront incohérentes entre workers. "
            f"Utilisez --workers 1 --threads {_workers} ou migrez vers flask-caching + Redis.",
            RuntimeWarning, stacklevel=2,
        )

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'


@app.context_processor
def inject_globals():
    """Injecte les variables globales disponibles dans tous les templates."""
    try:
        etab = db.get_settings_by_categorie("etablissement")
    except Exception:
        etab = {}
    # ── Multi-sites : injecte tous les sites et le site courant ──────────────
    try:
        all_sites = db.get_all_sites(include_inactive=False)
    except Exception:
        all_sites = []
    current_site = get_current_site() if current_user.is_authenticated else None
    return dict(etablissement=etab, all_sites=all_sites, current_site=current_site)

PROMOTIONS_DISPONIBLES = [
    "BUT RT 1", "BUT RT 2", "BUT RT 3",
    "BUT INFO 1", "BUT INFO 2", "BUT INFO 3",
    "LP SISR", "LP ASUR",
    "DUT RT 1", "DUT RT 2",
    "DUT INFO 1", "DUT INFO 2",
]


def get_promotions_disponibles() -> list[str]:
    """
    Retourne la liste des promotions actives depuis la DB (table promotions).
    Fallback sur PROMOTIONS_DISPONIBLES si la table est vide (compatibilité montée en version).
    """
    try:
        promos = db.get_promotions_list(include_inactive=False)
        return promos if promos else PROMOTIONS_DISPONIBLES
    except Exception:
        return PROMOTIONS_DISPONIBLES


def get_etablissement_settings() -> dict:
    """
    Charge les paramètres de l'établissement depuis la DB.
    Retourne un dict avec des valeurs par défaut si la table n'existe pas encore.
    """
    try:
        return db.get_settings_by_categorie("etablissement")
    except Exception:
        return {}


class User(UserMixin):
    def __init__(self, user_row: dict):
        self.id          = user_row["username"]
        self.role        = user_row["role"]
        self.nom_complet = user_row["nom_complet"]
        # ── Multi-sites : site_id NULL = super-admin, rempli = admin de site ──
        self.site_id     = user_row.get("site_id")   # None ou int

    def is_admin(self):
        return self.role == "admin"

    def is_super_admin(self):
        """Super-admin : role admin ET aucun site rattaché (site_id IS NULL)."""
        return self.role == "admin" and self.site_id is None

    def is_site_admin(self):
        """Admin de site : role admin ET site_id renseigné."""
        return self.role == "admin" and self.site_id is not None

    def can_manage_site(self, site_id) -> bool:
        """Vrai si l'utilisateur peut gérer ce site (super-admin ou admin du site)."""
        if self.is_super_admin():
            return True
        return self.is_site_admin() and self.site_id == site_id


@login_manager.user_loader
def load_user(user_id):
    row = db.get_user(user_id)
    if row and row.get("actif", 1):
        return User(row)
    return None



# =========================================================
# 🏗️  MULTI-SITES — helpers [MULTI-SITES]
# =========================================================

def get_current_site_id() -> int | None:
    """
    Retourne le site actuellement sélectionné par l'utilisateur.
    Priorité : session['site_id'] > site_id forcé de l'admin de site.
    Super-admin sans sélection → None (voit tout).
    """
    if current_user.is_authenticated and current_user.is_site_admin():
        return current_user.site_id  # admin de site : limité à son site
    return session.get("site_id")    # super-admin ou user : session


def get_current_site() -> dict | None:
    """Retourne le dict du site courant ou None (vue globale)."""
    sid = get_current_site_id()
    if sid is None:
        return None
    return db.get_site(sid)


def site_admin_required(f):
    """
    Décorateur : accès réservé aux admins.
    Un admin de site ne peut accéder qu'aux ressources de son site.
    Le site_id de la ressource doit être passé en kwarg 'site_id' ou extrait de la salle.
    """
    from functools import wraps as _wraps
    @_wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            abort(403)
        # Admin de site : vérification que la ressource appartient à son site
        # (la vérification fine est faite dans chaque route)
        return f(*args, **kwargs)
    return decorated


def filter_salles_by_current_site(salles_list: list[dict]) -> list[dict]:
    """
    Filtre une liste de salles selon le site courant.
    Si site_id courant est None, retourne la liste complète.
    """
    sid = get_current_site_id()
    if sid is None:
        return salles_list
    return [s for s in salles_list if s.get("site_id") == sid]


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated


# =========================================================
# 🚦 RATE LIMITING /login
# =========================================================
#
# Stockage en mémoire : dict[ip] = {"count": int, "window_start": float, "lockout_until": float}
# Règles :
#   - MAX_LOGIN_ATTEMPTS tentatives échouées dans LOGIN_WINDOW secondes → blocage
#   - Durée du blocage : LOCKOUT_DURATION secondes (croissante selon le palier)
#   - Un login réussi remet le compteur à zéro pour cette IP
#   - Nettoyage automatique des entrées expirées à chaque requête

_login_attempts: dict = {}      # {ip: {"count": int, "window_start": float, "lockout_until": float}}

MAX_LOGIN_ATTEMPTS = 5          # tentatives échouées avant blocage
LOGIN_WINDOW       = 5 * 60     # fenêtre glissante en secondes (5 min)
LOCKOUT_BASE       = 5 * 60     # durée du 1er blocage (5 min)
LOCKOUT_MAX        = 60 * 60    # durée maximale d'un blocage (1 h)


def _get_client_ip() -> str:
    """
    Récupère l'IP du client en tenant compte d'un éventuel reverse proxy.
    Priorité : X-Forwarded-For (premier hop) > X-Real-IP > remote_addr.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    return request.remote_addr or "unknown"


def _purge_old_entries() -> None:
    """Supprime les entrées dont la fenêtre ET le blocage sont expirés (évite les fuites mémoire)."""
    now = time.time()
    expired = [
        ip for ip, data in _login_attempts.items()
        if now > data.get("lockout_until", 0)
        and now > data.get("window_start", 0) + LOGIN_WINDOW
    ]
    for ip in expired:
        del _login_attempts[ip]


def _is_rate_limited(ip: str) -> tuple[bool, int]:
    """
    Vérifie si l'IP est actuellement bloquée.
    Retourne (bloqué: bool, secondes_restantes: int).
    """
    now = time.time()
    data = _login_attempts.get(ip)
    if not data:
        return False, 0

    lockout_until = data.get("lockout_until", 0)
    if now < lockout_until:
        return True, int(lockout_until - now)

    return False, 0


def _record_failed_attempt(ip: str) -> tuple[bool, int]:
    """
    Enregistre une tentative échouée pour l'IP.
    Retourne (vient_d_être_bloqué: bool, secondes_de_blocage: int).
    """
    now = time.time()
    data = _login_attempts.setdefault(ip, {"count": 0, "window_start": now, "lockout_until": 0})

    # Fenêtre expirée → réinitialiser le compteur
    if now > data["window_start"] + LOGIN_WINDOW:
        data["count"]        = 0
        data["window_start"] = now
        data["lockout_until"] = 0

    data["count"] += 1

    if data["count"] >= MAX_LOGIN_ATTEMPTS:
        # Calcul de la durée de blocage (double à chaque dépassement, plafonné)
        nb_lockouts  = max(1, data["count"] - MAX_LOGIN_ATTEMPTS + 1)
        lockout_secs = min(LOCKOUT_BASE * nb_lockouts, LOCKOUT_MAX)
        data["lockout_until"] = now + lockout_secs
        app.logger.warning(
            f"[rate-limit] IP {ip} bloquée {lockout_secs}s après {data['count']} tentatives échouées."
        )
        return True, int(lockout_secs)

    return False, 0


def _reset_attempts(ip: str) -> None:
    """Réinitialise le compteur d'une IP après un login réussi."""
    _login_attempts.pop(ip, None)


# =========================================================
# 📁 CONFIGURATION DES CHEMINS
# =========================================================
DOSSIER_CIBLE   = "salleICS/"   # Fallback local uniquement (plus requis)
BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER   = os.path.join(BASE_DIR, "static", "uploads", "salles")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_PHOTO_SIZE  = 5 * 1024 * 1024  # 5 Mo

try:
    locale.setlocale(locale.LC_TIME, 'fr_FR.UTF-8')
except Exception:
    pass

# =========================================================
# ⚡ CACHE MÉMOIRE ICS
# =========================================================
# ⚠️  CONTRAINTE MULTI-WORKER : ce cache est un dict Python en mémoire de
# processus. Avec Gunicorn en mode multi-process (--workers > 1), chaque
# worker a son propre cache isolé → invalidations incohérentes possibles.
# → En production : lancer Gunicorn avec --workers 1 (et --threads N pour
#   la concurrence), ou migrer vers flask-caching + Redis.
_ics_cache = {}
ICS_CACHE_TTL = 60

ENT_AUTH_MODE      = os.getenv('ENT_AUTH_MODE', 'none')
ENT_SESSION_COOKIE = os.getenv('ENT_SESSION_COOKIE', '')
ENT_API_TOKEN      = os.getenv('ENT_API_TOKEN', '')
ICS_FETCH_TIMEOUT  = 5


def _build_ent_headers():
    headers = {"User-Agent": "SalleDispo/1.0"}
    if ENT_AUTH_MODE == 'cookie':
        headers["Cookie"] = f"PHPSESSID={ENT_SESSION_COOKIE}"
    elif ENT_AUTH_MODE == 'token':
        headers["Authorization"] = f"Bearer {ENT_API_TOKEN}"
    return headers


def _get_ics_events(nom_salle: str):
    """
    Récupère les événements ICS pour une salle.
    Priorité : URL iCal en DB > fichier local fallback (ical_file en DB ou nom_salle.ics)
    Cache TTL 60s.
    """
    cache_key = nom_salle
    now_ts = time.time()
    cached = _ics_cache.get(cache_key)
    if cached and (now_ts - cached["timestamp"]) < ICS_CACHE_TTL:
        return cached["data"]

    salle_row = db.get_salle(nom_salle)
    ical_url  = (salle_row or {}).get("ical_url")
    ical_file = (salle_row or {}).get("ical_file") or f"{nom_salle}.ics"

    ics_bytes = None

    # 1. URL distante
    if ical_url:
        try:
            resp = requests.get(ical_url, headers=_build_ent_headers(), timeout=ICS_FETCH_TIMEOUT)
            resp.raise_for_status()
            ics_bytes = resp.content
        except requests.exceptions.Timeout:
            app.logger.warning(f"Timeout ENT pour {nom_salle}")
        except requests.exceptions.RequestException as e:
            app.logger.warning(f"Erreur ICS distant {nom_salle} : {e}")

    # 2. Fichier local fallback
    if ics_bytes is None:
        chemin = os.path.join(DOSSIER_CIBLE, ical_file)
        try:
            with open(chemin, 'rb') as f:
                ics_bytes = f.read()
        except Exception:
            _ics_cache[cache_key] = {"data": [], "timestamp": now_ts}
            return []

    events = []
    try:
        cal = Calendar.from_ical(ics_bytes)
        events = [c for c in cal.walk() if c.name == "VEVENT"]
    except Exception as e:
        app.logger.warning(f"Erreur parsing ICS {nom_salle} : {e}")

    _ics_cache[cache_key] = {"data": events, "timestamp": now_ts}
    return events


def _invalidate_ics_cache(nom_salle: str):
    _ics_cache.pop(nom_salle, None)


# =========================================================
# 🧩 PARSING ICS
# =========================================================
TZ_PARIS = pytz.timezone('Europe/Paris')

# =========================================================
# 🕐 HORAIRES D'OUVERTURE / FERMETURE [MULTI-SITES]
# =========================================================
# Valeurs de secours si une salle n'est rattachée à aucun site
# (ou si le site n'a pas encore d'horaires renseignés).
DEFAULT_HEURE_OUVERTURE = "08:00"
DEFAULT_HEURE_FERMETURE = "20:00"
DEFAULT_JOURS_OUVERTURE = "0,1,2,3,4"  # Lundi → Vendredi (cf. date.weekday(), 0=Lundi)

NOMS_JOURS_SEMAINE = {0: "Lundi", 1: "Mardi", 2: "Mercredi", 3: "Jeudi",
                      4: "Vendredi", 5: "Samedi", 6: "Dimanche"}
NOMS_JOURS_ABBR = {0: "Lun", 1: "Mar", 2: "Mer", 3: "Jeu", 4: "Ven", 5: "Sam", 6: "Dim"}


def parse_jours_ouverture(valeur: str, defaut: str = DEFAULT_JOURS_OUVERTURE) -> list[int]:
    """
    Parse une chaîne CSV de jours ('0,1,2,3,4') en liste d'entiers 0-6 triée.
    Retourne les jours par défaut si la valeur est vide/invalide/hors-plage.
    """
    def _parse(chaine):
        jours = set()
        for part in (chaine or "").split(","):
            part = part.strip()
            if part.isdigit() and 0 <= int(part) <= 6:
                jours.add(int(part))
        return sorted(jours)

    jours = _parse(valeur)
    if jours:
        return jours
    return _parse(defaut) or [0, 1, 2, 3, 4]


def formater_jours_ouverture(jours: list[int], abbr: bool = False) -> str:
    """
    Formate une liste de jours (0-6) en texte lisible, avec regroupement des
    plages contiguës. Ex : [0,1,2,3,4] -> 'Lundi – Vendredi', [0,2,4] -> 'Lundi, Mercredi, Vendredi'.
    """
    noms = NOMS_JOURS_ABBR if abbr else NOMS_JOURS_SEMAINE
    jours = sorted(set(j for j in (jours or []) if 0 <= j <= 6))
    if not jours:
        return "Fermé"
    if len(jours) == 7:
        return "Tous les jours"

    groupes = []
    debut = prec = jours[0]
    for j in jours[1:]:
        if j == prec + 1:
            prec = j
            continue
        groupes.append((debut, prec))
        debut = prec = j
    groupes.append((debut, prec))

    parties = []
    for d, f in groupes:
        if d == f:
            parties.append(noms[d])
        elif f == d + 1:
            parties.append(f"{noms[d]}, {noms[f]}")
        else:
            parties.append(f"{noms[d]} – {noms[f]}")
    return ", ".join(parties)


def valider_jours_ouverture(jours_selectionnes: list[str]) -> tuple[bool, str | None, str | None]:
    """
    Valide une sélection de jours envoyée par le formulaire admin (liste de chaînes '0'..'6').
    Retourne (ok, message_erreur, chaine_csv_normalisee).
    """
    try:
        jours = sorted(set(int(j) for j in jours_selectionnes))
    except (ValueError, TypeError):
        return False, "Sélection de jours invalide.", None

    if not jours or any(j < 0 or j > 6 for j in jours):
        return False, "Vous devez sélectionner au moins un jour d'ouverture valide.", None

    return True, None, ",".join(str(j) for j in jours)


@app.template_filter('fmt_jours')
def jinja_fmt_jours(valeur, abbr=False):
    """Filtre Jinja : '0,1,2,3,4' -> 'Lundi – Vendredi' (ou liste déjà parsée)."""
    jours = valeur if isinstance(valeur, list) else parse_jours_ouverture(valeur)
    return formater_jours_ouverture(jours, abbr=abbr)


def parse_heure_hhmm(valeur: str, defaut: str = "08:00") -> tuple[int, int]:
    """Parse une chaîne 'HH:MM' en (heure, minute). Retourne le défaut si invalide."""
    try:
        h, m = map(int, (valeur or defaut).split(':'))
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    h, m = map(int, defaut.split(':'))
    return h, m


def get_horaires_salle(nom_salle: str) -> dict:
    """
    Retourne les horaires et jours d'ouverture applicables à une salle,
    déterminés par le site auquel elle est rattachée.
    Si la salle n'a pas de site (ou le site n'existe plus), retourne les
    valeurs par défaut de l'application.
    Clés retournées : heure_ouverture, heure_fermeture, jours_ouverture (list[int]), site (dict ou None).
    """
    site = None
    salle_row = db.get_salle(nom_salle)
    if salle_row and salle_row.get('site_id'):
        site = db.get_site(salle_row['site_id'])

    if site and site.get('heure_ouverture') and site.get('heure_fermeture'):
        return {
            "heure_ouverture": site['heure_ouverture'],
            "heure_fermeture": site['heure_fermeture'],
            "jours_ouverture": parse_jours_ouverture(site.get('jours_ouverture')),
            "site": site,
        }
    return {
        "heure_ouverture": DEFAULT_HEURE_OUVERTURE,
        "heure_fermeture": DEFAULT_HEURE_FERMETURE,
        "jours_ouverture": parse_jours_ouverture(DEFAULT_JOURS_OUVERTURE),
        "site": site,
    }


def valider_horaires(heure_ouverture: str, heure_fermeture: str) -> tuple[bool, str | None]:
    """
    Valide une paire d'horaires saisis dans le formulaire admin.
    Retourne (True, None) si valide, (False, message_erreur) sinon.
    Règles : format HH:MM valide, ouverture strictement avant fermeture,
    fermeture au plus tard à 23:59 (pas de créneau traversant minuit).
    """
    try:
        h_o, m_o = map(int, heure_ouverture.split(':'))
        h_f, m_f = map(int, heure_fermeture.split(':'))
    except (ValueError, AttributeError):
        return False, "Format d'horaire invalide (attendu HH:MM)."

    if not (0 <= h_o <= 23 and 0 <= m_o <= 59 and 0 <= h_f <= 23 and 0 <= m_f <= 59):
        return False, "Horaire hors plage (heures 0-23, minutes 0-59)."

    minutes_ouverture = h_o * 60 + m_o
    minutes_fermeture = h_f * 60 + m_f
    if minutes_fermeture <= minutes_ouverture:
        return False, "L'heure de fermeture doit être après l'heure d'ouverture."

    return True, None


def parse_events(nom_salle: str):
    raw_events = _get_ics_events(nom_salle)
    result = []

    for component in raw_events:
        dtstart_prop = component.get('dtstart')
        dtend_prop   = component.get('dtend')
        if not dtstart_prop:
            continue

        dtstart = dtstart_prop.dt
        dtend   = dtend_prop.dt if dtend_prop else dtstart
        summary = str(component.get('summary', '')).replace('\\,', ',')
        # ── Niveau 3 : filtre SUMMARY ──────────────────────────────────────
        # filtered (défaut) : nom du cours uniquement, sans enseignant ni groupe
        # none              : remplace tout par "Cours" (confidentialité max)
        # full              : titre ICS brut complet
        if ICS_SUMMARY_MODE == "none":
            summary = "Cours"
        elif ICS_SUMMARY_MODE == "full":
            pass  # on garde le summary brut tel quel
        else:  # "filtered" — comportement par défaut
            summary = _filter_summary(summary)


        if not isinstance(dtstart, datetime):
            dtstart = datetime.combine(dtstart, datetime.min.time())
            dtend   = datetime.combine(
                dtend if not isinstance(dtend, datetime) else dtend.date(),
                datetime.min.time()
            )
            dtstart = TZ_PARIS.localize(dtstart)
            dtend   = TZ_PARIS.localize(dtend)
        else:
            dtstart = dtstart.astimezone(TZ_PARIS) if dtstart.tzinfo else TZ_PARIS.localize(dtstart)
            dtend   = (
                dtend.astimezone(TZ_PARIS) if isinstance(dtend, datetime) and dtend.tzinfo
                else TZ_PARIS.localize(dtend) if isinstance(dtend, datetime)
                else TZ_PARIS.localize(datetime.combine(dtend, datetime.min.time()))
            )

        result.append({"dtstart": dtstart, "dtend": dtend, "summary": summary})

    return result


# =========================================================
# 🛠️ HELPERS
# =========================================================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# Signatures magic bytes pour les formats image autorisés.
# On lit les N premiers octets du fichier uploadé et on vérifie qu'ils
# correspondent à un vrai fichier image — pas juste une extension renommée.
_MAGIC_BYTES = [
    (bytes([0xFF, 0xD8, 0xFF]),                       "JPEG"),
    (bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]), "PNG"),
    (b"GIF87a",                                       "GIF"),
    (b"GIF89a",                                       "GIF"),
    (b"RIFF",                                         "WEBP"),
]
_MAGIC_READ_BYTES = 12   # on lit 12 octets, suffisant pour tous les formats ci-dessus


def validate_image_magic(file_storage) -> bool:
    """
    Vérifie que le contenu réel de FileStorage correspond à une image connue.

    Lit les premiers octets du fichier (sans le consommer entièrement),
    compare aux signatures magic bytes des formats autorisés,
    puis remet le curseur à 0 pour que file.save() fonctionne normalement.

    WebP : RIFF (4 octets) + taille (4 octets) + "WEBP" (4 octets).
    On vérifie donc RIFF en position 0 ET WEBP en position 8.

    Retourne True si le fichier est reconnu comme image valide, False sinon.
    """
    header = file_storage.read(_MAGIC_READ_BYTES)
    file_storage.seek(0)   # rembobinage impératif avant file.save()

    for magic, fmt in _MAGIC_BYTES:
        if header.startswith(magic):
            if fmt == "WEBP":
                # Contrôle supplémentaire : octets 8-11 doivent être b"WEBP"
                return header[8:12] == b'WEBP'
            return True

    return False


def get_infos_salle(nom_salle: str) -> dict:
    """
    Retourne les infos d'une salle depuis la table salles (DB).
    Valeurs par défaut si la salle n'existe pas (ne devrait pas arriver).
    """
    salle = db.get_salle(nom_salle)
    if salle:
        raw_url = salle.get("ical_url")  # déjà déchiffré par database.py
        places_raw = salle.get("places")
        places_display = str(places_raw) if places_raw is not None else "?"
        return {
            "nom_complet": salle["nom_complet"] or f"Salle {nom_salle}",
            "places":      places_display,
            "places_int":  int(places_raw) if places_raw not in (None, "", "?") else None,         # entier ou None, pour les comparaisons
            "pc":          bool(salle["pc"]),
            "projecteur":  bool(salle["projecteur"]),
            "tableau":     bool(salle["tableau"]),
            "description": salle["description"] or "Pas d'info.",
            "etage":       salle.get("etage", "0"),
            "aile":        salle.get("aile", "centre"),
            "ical_url":    raw_url,
            "ical_url_masked": mask_url_for_display(raw_url),
            "ical_file":   salle.get("ical_file"),
        }
    return {
        "nom_complet": f"Salle {nom_salle}", "places": "?", "places_int": None,
        "pc": False, "projecteur": False, "tableau": False,
        "description": "Pas d'info.", "etage": "0", "aile": "centre",
        "ical_url": None, "ical_url_masked": None, "ical_file": None,
    }


def detecter_etage_aile(nom_salle: str, infos: dict):
    """Détermine étage et aile depuis les infos DB ou le nom de la salle."""
    try:
        etage = int(infos.get("etage", 0))
    except (ValueError, TypeError):
        etage = int(nom_salle[0]) if nom_salle and nom_salle[0].isdigit() else 0

    aile = infos.get("aile", "centre")
    if not aile or aile == "centre":
        chiffres = re.findall(r'\d+', nom_salle)
        if chiffres:
            num = int(chiffres[0])
            aile = "droite" if num % 2 == 0 else "gauche"
        else:
            aile = "centre"

    return etage, aile


def get_reports(nom_salle):
    return db.get_reports_salle(nom_salle)


def add_report(nom_salle, type_pb, description):
    auteur = current_user.id if current_user.is_authenticated else "Anonyme"
    return db.add_report(nom_salle, type_pb, description, auteur)


def delete_report(nom_salle, report_id):
    return db.delete_report(nom_salle, report_id)


def update_report_statut(nom_salle, report_id, nouveau_statut, note_admin=""):
    return db.update_report_statut(report_id, nouveau_statut, note_admin, acteur=current_user.id)


def get_report_or_403(report_id):
    """
    Charge un ticket et vérifie les droits d'accès :
    - super-admin : accès à tous les tickets
    - admin de site : accès limité aux tickets de son propre site
    Retourne le ticket (dict) ou déclenche un abort(404/403).
    """
    report = db.get_report(report_id)
    if not report:
        abort(404)
    if current_user.is_site_admin() and report.get("site_id") != current_user.site_id:
        abort(403)
    return report


# =========================================================
# 🚫 BLOCAGES
# =========================================================

def get_blocages_actifs(nom_salle=None):
    return db.get_blocages_actifs(nom_salle)


def salle_est_bloquee(nom_salle, dt_debut=None, dt_fin=None):
    now = datetime.now(TZ_PARIS)
    for b in get_blocages_actifs(nom_salle):
        try:
            b_debut = datetime.fromisoformat(b["date_debut"]).astimezone(TZ_PARIS)
            b_fin   = datetime.fromisoformat(b["date_fin"]).astimezone(TZ_PARIS)
            if dt_debut and dt_fin:
                if b_debut < dt_fin and b_fin > dt_debut:
                    return True, b.get("motif", "Salle bloquée")
            else:
                if b_debut <= now <= b_fin:
                    return True, b.get("motif", "Salle bloquée")
        except Exception:
            pass
    return False, None


# =========================================================
# 📅 RÉSERVATIONS
# =========================================================

def get_reservations_salle(nom_salle):
    now = datetime.now(TZ_PARIS)
    return [
        r for r in db.get_reservations_salle(nom_salle)
        if datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now
    ]


def get_reservations_user(user_id):
    return db.get_reservations_user(user_id)


def reserver_salle(nom_salle, date_str, heure_debut, heure_fin, motif, user_id, force_admin=False, nb_personnes=1):
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        h_d, m_d = map(int, heure_debut.split(':'))
        h_f, m_f = map(int, heure_fin.split(':'))

        dt_debut = TZ_PARIS.localize(datetime.combine(date_obj, datetime.min.time().replace(hour=h_d, minute=m_d)))
        dt_fin   = TZ_PARIS.localize(datetime.combine(date_obj, datetime.min.time().replace(hour=h_f, minute=m_f)))

        if dt_fin <= dt_debut:
            return False, "L'heure de fin doit être après l'heure de début."

        if dt_debut < datetime.now(TZ_PARIS) and not force_admin:
            return False, "Impossible de réserver dans le passé."

        duree = (dt_fin - dt_debut).total_seconds() / 60
        if duree < 30:
            return False, "La réservation doit durer au moins 30 minutes."
        if duree > 480:
            return False, "La réservation ne peut pas dépasser 8 heures."

    except ValueError as e:
        return False, f"Format de date/heure invalide : {e}"

    # ── Horaires d'ouverture / fermeture du site [MULTI-SITES] ────────────
    if not force_admin:
        horaires = get_horaires_salle(nom_salle)

        if date_obj.weekday() not in horaires["jours_ouverture"]:
            return False, (
                f"Le site est fermé le {NOMS_JOURS_SEMAINE[date_obj.weekday()]}. "
                f"Jours d'ouverture : {formater_jours_ouverture(horaires['jours_ouverture'])}."
            )

        h_ouv_site, m_ouv_site = parse_heure_hhmm(horaires["heure_ouverture"], DEFAULT_HEURE_OUVERTURE)
        h_ferm_site, m_ferm_site = parse_heure_hhmm(horaires["heure_fermeture"], DEFAULT_HEURE_FERMETURE)
        borne_ouverture = dt_debut.replace(hour=h_ouv_site, minute=m_ouv_site, second=0, microsecond=0)
        borne_fermeture = dt_debut.replace(hour=h_ferm_site, minute=m_ferm_site, second=0, microsecond=0)
        if dt_debut < borne_ouverture or dt_fin > borne_fermeture:
            return False, (
                f"Réservation impossible en dehors des horaires d'ouverture "
                f"({horaires['heure_ouverture']} – {horaires['heure_fermeture']})."
            )

    if not force_admin:
        bloque, motif_blocage = salle_est_bloquee(nom_salle, dt_debut, dt_fin)
        if bloque:
            return False, f"La salle est bloquée sur ce créneau : {motif_blocage}"

    # Vérification ICS (cours)
    if not verifier_dispo_creneau(nom_salle, dt_debut, dt_fin):
        return False, "La salle est déjà occupée par un cours sur ce créneau."

    # Vérification de capacité (réservations simultanées)
    infos = get_infos_salle(nom_salle)
    places_totales = infos.get("places_int")  # None si inconnu

    if places_totales is not None:
        places_deja_occupees = db.get_places_occupees_creneau(
            nom_salle, dt_debut.isoformat(), dt_fin.isoformat()
        )
        places_restantes = places_totales - places_deja_occupees
        if nb_personnes > places_restantes:
            if places_restantes <= 0:
                return False, f"La salle est complète sur ce créneau (capacité : {places_totales} places)."
            return False, (
                f"Pas assez de places disponibles. Il reste {places_restantes} place{'s' if places_restantes > 1 else ''} "
                f"sur ce créneau (vous en demandez {nb_personnes})."
            )
    else:
        # Capacité inconnue : comportement legacy (1 seule réservation simultanée)
        for r in db.get_reservations_salle(nom_salle):
            r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
            if r_debut < dt_fin and r_fin > dt_debut:
                return False, f"La salle est déjà réservée par {r['user']} de {r_debut.strftime('%H:%M')} à {r_fin.strftime('%H:%M')}."

    resa_id = str(uuid.uuid4()).replace('-', '')[:16]  # 16 hex chars (était 8 — trop court)
    db.add_reservation(
        id_            = resa_id,
        salle          = nom_salle,
        user           = user_id,
        motif          = motif[:200].strip(),
        date_debut     = dt_debut.isoformat(),
        date_fin       = dt_fin.isoformat(),
        date_creation  = datetime.now(TZ_PARIS).isoformat(),
        cree_par_admin = force_admin,
        nb_personnes   = nb_personnes,
    )
    return True, resa_id


def annuler_reservation(reservation_id, user_id, is_admin=False):
    return db.cancel_reservation(reservation_id, user_id, is_admin)


# =========================================================
# 🧠 LOGIQUE ICS
# =========================================================

def verifier_dispo_creneau(nom_salle: str, start_req, end_req):
    if start_req.tzinfo is None:
        start_req = TZ_PARIS.localize(start_req)
    if end_req.tzinfo is None:
        end_req = TZ_PARIS.localize(end_req)
    try:
        for ev in parse_events(nom_salle):
            if ev["dtstart"] < end_req and ev["dtend"] > start_req:
                return False
        return True
    except Exception as e:
        app.logger.warning(f"Erreur vérification dispo {nom_salle} : {e}")
        return False


def get_salle_status(nom_salle: str):
    maintenant = datetime.now(TZ_PARIS)

    # Blocage admin
    bloque, motif_blocage = salle_est_bloquee(nom_salle)
    if bloque:
        return {
            "etat": "BLOQUÉ", "color": "dark",
            "msg": f"🔒 {motif_blocage}", "sub_msg": "Bloquée par l'administration",
            "progression": 100
        }

    # Réservations étudiants en cours — peut y en avoir plusieurs simultanément
    infos = get_infos_salle(nom_salle)
    places_totales = infos.get("places_int")  # None si inconnu

    resas_en_cours = []
    for r in get_reservations_salle(nom_salle):
        r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
        r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
        if r_debut <= maintenant <= r_fin:
            resas_en_cours.append((r, r_debut, r_fin))

    if resas_en_cours:
        # Calculer les places occupées en ce moment
        places_occupees = sum(r.get("nb_personnes") or 1 for r, _, _ in resas_en_cours)

        # Toutes les infos de progression basées sur la première réservation commencée
        r, r_debut, r_fin = resas_en_cours[0]
        total    = (r_fin - r_debut).total_seconds()
        ecoule   = (maintenant - r_debut).total_seconds()
        prog     = int((ecoule / total) * 100) if total > 0 else 100

        if places_totales is not None:
            places_restantes = max(0, places_totales - places_occupees)
            if places_restantes > 0:
                # Salle partiellement occupée — encore de la place
                first_r, first_debut, first_fin = resas_en_cours[0]
                user_info = db.get_user(first_r["user"]) or {}
                nom_org   = user_info.get("nom_complet") or first_r["user"]
                participants = db.get_participants(first_r["id"])
                # Fin la plus tardive parmi toutes les réservations en cours
                r_fin_max = max(rf for _, _, rf in resas_en_cours)
                return {
                    "etat": "PARTIEL", "color": "info",
                    "msg": f"{places_restantes} place{'s' if places_restantes > 1 else ''} disponible{'s' if places_restantes > 1 else ''}",
                    "sub_msg": f"Fin à {r_fin_max.strftime('%H:%M')} · {places_occupees}/{places_totales} places occupées",
                    "progression": prog,
                    "places_totales":   places_totales,
                    "places_occupees":  places_occupees,
                    "places_restantes": places_restantes,
                    "nb_resas":         len(resas_en_cours),
                    # Infos de la 1ère resa pour rétrocompat
                    "resa_id":       first_r["id"],
                    "resa_user":     first_r["user"],
                    "resa_nom_org":  nom_org,
                    "resa_motif":    first_r["motif"],
                    "resa_debut":    first_debut.strftime("%H:%M"),
                    "resa_fin":      r_fin_max.strftime("%H:%M"),
                    "resa_duree_min": int((r_fin_max - first_debut).total_seconds() / 60),
                    "resa_participants": participants,
                }

        # Salle complète (ou capacité inconnue avec une réservation)
        r, r_debut, r_fin = resas_en_cours[-1]  # la plus longue
        r_fin_max = max(rf for _, _, rf in resas_en_cours)
        user_info = db.get_user(r["user"]) or {}
        nom_org   = user_info.get("nom_complet") or r["user"]
        participants = db.get_participants(r["id"])
        duree_min = int((r_fin_max - r_debut).total_seconds() / 60)

        sub = f"Fin à {r_fin_max.strftime('%H:%M')} · {r['motif'][:30]}"
        if places_totales:
            sub = f"Fin à {r_fin_max.strftime('%H:%M')} · {places_occupees}/{places_totales} places"

        return {
            "etat": "RÉSERVÉ", "color": "warning",
            "msg": f"Réservé par {r['user']}",
            "sub_msg": sub,
            "progression": prog,
            "places_totales":   places_totales,
            "places_occupees":  places_occupees,
            "places_restantes": 0,
            "nb_resas":         len(resas_en_cours),
            "resa_id":       r["id"],
            "resa_user":     r["user"],
            "resa_nom_org":  nom_org,
            "resa_motif":    r["motif"],
            "resa_debut":    r_debut.strftime("%H:%M"),
            "resa_fin":      r_fin_max.strftime("%H:%M"),
            "resa_duree_min": duree_min,
            "resa_participants": participants,
        }

    # Cours ICS
    try:
        prochain_cours = None
        delta_min = float('inf')
        cours_trouve = False

        for ev in parse_events(nom_salle):
            dtstart = ev["dtstart"]
            dtend   = ev["dtend"]
            summary = ev["summary"]

            if dtstart <= maintenant <= dtend:
                fin_txt = dtend.strftime("%H:%M")
                total   = (dtend - dtstart).total_seconds()
                ecoule  = (maintenant - dtstart).total_seconds()
                prog    = int((ecoule / total) * 100) if total > 0 else 100
                return {
                    "etat": "OCCUPÉ", "color": "danger",
                    "msg": summary, "sub_msg": f"Fin à {fin_txt}",
                    "progression": prog
                }

            if dtstart > maintenant:
                cours_trouve = True
                delta = (dtstart - maintenant).total_seconds()
                if delta < delta_min:
                    delta_min = delta
                    if dtstart.date() == maintenant.date():
                        prochain_cours = f"à {dtstart.strftime('%Hh%M')}"
                    else:
                        prochain_cours = f"le {dtstart.strftime('%d/%m')} à {dtstart.strftime('%Hh%M')}"

        # Chercher la prochaine réservation future
        prochaine_resa = None
        for r in get_reservations_salle(nom_salle):
            r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
            if r_debut > maintenant:
                if prochaine_resa is None or r_debut < prochaine_resa["_debut_dt"]:
                    label_debut = (f"à {r_debut.strftime('%Hh%M')}" if r_debut.date() == maintenant.date()
                                   else f"le {r_debut.strftime('%d/%m')} à {r_debut.strftime('%Hh%M')}")
                    prochaine_resa = {
                        "_debut_dt":  r_debut,
                        "debut":      label_debut,
                        "fin":        r_fin.strftime("%Hh%M"),
                        "motif":      r["motif"][:40],
                        "nb_personnes": r.get("nb_personnes") or 1,
                    }

        if prochain_cours:
            return {"etat": "LIBRE", "color": "success", "msg": "Libre",
                    "sub_msg": f"Prochain cours {prochain_cours}", "progression": 0,
                    "prochaine_resa": prochaine_resa}
        elif cours_trouve:
            return {"etat": "LIBRE", "color": "success", "msg": "Libre",
                    "sub_msg": "Plus de cours auj.", "progression": 0,
                    "prochaine_resa": prochaine_resa}
        else:
            return {"etat": "LIBRE", "color": "success", "msg": "Libre",
                    "sub_msg": "Planning vide", "progression": 0,
                    "prochaine_resa": prochaine_resa}

    except Exception as e:
        app.logger.error(f"Erreur lecture ICS {nom_salle} : {e}")
        return {"etat": "ERREUR", "color": "secondary", "msg": "Erreur",
                "sub_msg": "Données ICS indisponibles", "progression": 0}


def get_planning_etendu(nom_salle: str):
    liste_evenements = []
    try:
        maintenant = datetime.now(TZ_PARIS)
        fin = maintenant + timedelta(days=15)

        for ev in parse_events(nom_salle):
            dtstart = ev["dtstart"]
            dtend   = ev["dtend"]
            if dtend > maintenant and dtstart < fin:
                liste_evenements.append({
                    "date_iso":  dtstart.strftime("%Y-%m-%d"),
                    "jour_joli": dtstart.strftime("%A %d %B").capitalize(),
                    "horaire":   f"{dtstart.strftime('%H:%M')} - {dtend.strftime('%H:%M')}",
                    "titre":     ev["summary"],
                    "type":      "cours",
                    "timestamp": dtstart.timestamp()
                })

        for r in db.get_reservations_salle(nom_salle):
            r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
            if r_fin > maintenant and r_debut < fin:
                duree_min    = int((r_fin - r_debut).total_seconds() / 60)
                user_info    = db.get_user(r["user"]) or {}
                nom_org      = user_info.get("nom_complet") or r["user"]
                participants = db.get_participants(r["id"])
                liste_evenements.append({
                    "date_iso":    r_debut.strftime("%Y-%m-%d"),
                    "jour_joli":   r_debut.strftime("%A %d %B").capitalize(),
                    "horaire":     f"{r_debut.strftime('%H:%M')} - {r_fin.strftime('%H:%M')}",
                    "titre":       f"📌 {r['motif'][:40]}",
                    "type":        "reservation",
                    "user":        r["user"],
                    "nom_org":     nom_org,
                    "motif":       r["motif"],
                    "duree_min":   duree_min,
                    "nb_personnes": r.get("nb_personnes") or 1,
                    "participants": participants,
                    "timestamp":   r_debut.timestamp()
                })

        liste_evenements.sort(key=lambda x: x['timestamp'])
        return liste_evenements

    except Exception as e:
        app.logger.error(f"Erreur planning {nom_salle} : {e}")
        return []


def get_planning_user(user_id: str, semaine_offset: int = 0):
    """
    Retourne tous les événements de la semaine en cours (ou d'une autre semaine
    via semaine_offset) pour un utilisateur donné :
      - ses propres réservations (role='organisateur')
      - les réservations où il est participant (role='participant')

    Format compatible avec le planning de detail.html :
    {
        date_iso, horaire, titre, type, user, nom_org, motif,
        duree_min, nb_personnes, participants, role, id, salle, timestamp
    }
    """
    now = datetime.now(TZ_PARIS)
    # Lundi de la semaine ciblée
    lundi = (now.date() - timedelta(days=now.weekday())) + timedelta(weeks=semaine_offset)
    debut_semaine = TZ_PARIS.localize(datetime.combine(lundi, datetime.min.time()))
    fin_semaine   = TZ_PARIS.localize(datetime.combine(lundi + timedelta(days=6), datetime.max.time()))

    events = []

    # ── Réservations de l'utilisateur (organisateur) ──
    try:
        all_resas = db.get_all_reservations_user(user_id)
    except Exception:
        all_resas = db.get_reservations_user(user_id)

    for r in all_resas:
        if r.get("statut") != "confirmee":
            continue
        try:
            r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
        except Exception:
            continue
        if r_debut > fin_semaine or r_fin < debut_semaine:
            continue
        duree_min    = int((r_fin - r_debut).total_seconds() / 60)
        participants = []
        try:
            participants = db.get_participants(r["id"])
        except Exception:
            pass
        events.append({
            "id":          r["id"],
            "date_iso":    r_debut.strftime("%Y-%m-%d"),
            "horaire":     f"{r_debut.strftime('%H:%M')}-{r_fin.strftime('%H:%M')}",
            "titre":       f"📌 {r.get('motif', 'Réservation')[:40]}",
            "type":        "reservation",
            "user":        r["user"],
            "nom_org":     user_id,
            "motif":       r.get("motif", ""),
            "duree_min":   duree_min,
            "nb_personnes": r.get("nb_personnes") or 1,
            "participants": participants,
            "role":        "organisateur",
            "salle":       r.get("salle", ""),
            "timestamp":   r_debut.timestamp(),
        })

    # ── Réservations où l'utilisateur est participant ──
    try:
        resas_participant = db.get_all_reservations_as_participant(user_id)
    except Exception:
        resas_participant = db.get_reservations_as_participant(user_id)

    seen_ids = {e["id"] for e in events}
    for r in resas_participant:
        if r.get("statut") != "confirmee":
            continue
        if r["id"] in seen_ids:
            continue
        try:
            r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
        except Exception:
            continue
        if r_debut > fin_semaine or r_fin < debut_semaine:
            continue
        duree_min = int((r_fin - r_debut).total_seconds() / 60)
        org_info  = db.get_user(r["user"]) or {}
        nom_org   = org_info.get("nom_complet") or r["user"]
        participants = []
        try:
            participants = db.get_participants(r["id"])
        except Exception:
            pass
        events.append({
            "id":          r["id"],
            "date_iso":    r_debut.strftime("%Y-%m-%d"),
            "horaire":     f"{r_debut.strftime('%H:%M')}-{r_fin.strftime('%H:%M')}",
            "titre":       f"👥 {r.get('motif', 'Invitation')[:40]}",
            "type":        "reservation",
            "user":        r["user"],
            "nom_org":     nom_org,
            "motif":       r.get("motif", ""),
            "duree_min":   duree_min,
            "nb_personnes": r.get("nb_personnes") or 1,
            "participants": participants,
            "role":        "participant",
            "salle":       r.get("salle", ""),
            "timestamp":   r_debut.timestamp(),
        })

    events.sort(key=lambda x: x["timestamp"])
    return {
        "events":       events,
        "lundi":        lundi,
        "fin_semaine":  (lundi + timedelta(days=6)),
        "semaine_offset": semaine_offset,
    }


def get_creneaux_libres(nom_salle: str, date_str: str, nb_personnes: int = 1):
    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return []

    maintenant = datetime.now(TZ_PARIS)
    reservations_salle = db.get_reservations_salle(nom_salle)
    infos = get_infos_salle(nom_salle)
    places_totales = infos.get("places_int")  # None si inconnu
    creneaux = []

    # ── Horaires d'ouverture / fermeture du site [MULTI-SITES] ────────────
    horaires = get_horaires_salle(nom_salle)

    # Jour fermé : aucun créneau, inutile de calculer les bornes horaires
    if date_obj.weekday() not in horaires["jours_ouverture"]:
        return []

    h_ouverture, m_ouverture = parse_heure_hhmm(horaires["heure_ouverture"], DEFAULT_HEURE_OUVERTURE)
    h_fermeture, m_fermeture = parse_heure_hhmm(horaires["heure_fermeture"], DEFAULT_HEURE_FERMETURE)
    borne_ouverture = TZ_PARIS.localize(
        datetime.combine(date_obj, datetime.min.time().replace(hour=h_ouverture, minute=m_ouverture))
    )
    borne_fermeture = TZ_PARIS.localize(
        datetime.combine(date_obj, datetime.min.time().replace(hour=h_fermeture, minute=m_fermeture))
    )

    debut = borne_ouverture
    while debut + timedelta(minutes=30) <= borne_fermeture:
        fin = debut + timedelta(minutes=30)

        if debut < maintenant:
            debut += timedelta(minutes=30)
            continue

        bloque, _ = salle_est_bloquee(nom_salle, debut, fin)
        if bloque:
            creneaux.append({"heure": debut.strftime("%H:%M"), "libre": False, "type": "bloque"})
            debut += timedelta(minutes=30)
            continue

        if not verifier_dispo_creneau(nom_salle, debut, fin):
            creneaux.append({"heure": debut.strftime("%H:%M"), "libre": False, "type": "cours"})
            debut += timedelta(minutes=30)
            continue

        # Vérification capacité
        if places_totales is not None:
            places_occupees = db.get_places_occupees_creneau(
                nom_salle, debut.isoformat(), fin.isoformat()
            )
            places_restantes = places_totales - places_occupees
            if places_restantes <= 0:
                creneaux.append({
                    "heure": debut.strftime("%H:%M"), "libre": False, "type": "reserve",
                    "places_restantes": 0, "places_totales": places_totales
                })
            elif places_restantes < nb_personnes:
                # Pas assez de place pour la demande en cours
                creneaux.append({
                    "heure": debut.strftime("%H:%M"), "libre": False, "type": "insuffisant",
                    "places_restantes": places_restantes, "places_totales": places_totales
                })
            else:
                creneaux.append({
                    "heure": debut.strftime("%H:%M"), "libre": True, "type": "libre",
                    "places_restantes": places_restantes, "places_totales": places_totales
                })
        else:
            # Capacité inconnue : comportement legacy
            reserve = False
            for r in reservations_salle:
                r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
                r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
                if r_debut < fin and r_fin > debut:
                    reserve = True
                    break
            if reserve:
                creneaux.append({"heure": debut.strftime("%H:%M"), "libre": False, "type": "reserve"})
            else:
                creneaux.append({"heure": debut.strftime("%H:%M"), "libre": True, "type": "libre"})

        debut += timedelta(minutes=30)

    return creneaux


# =========================================================
# 📊 STATISTIQUES ADMIN
# =========================================================

def get_stats_avancees(site_id=None):
    from collections import Counter, defaultdict

    all_reservations = db.get_reservations_by_site(site_id)
    now = datetime.now(TZ_PARIS)

    salle_counter = Counter(r["salle"] for r in all_reservations if r["statut"] == "confirmee")
    top_salles = salle_counter.most_common(5)
    max_count  = top_salles[0][1] if top_salles else 1
    top_salles_avec_pct = [(s, c, int(c / max_count * 100)) for s, c in top_salles]

    jours = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
    resas_par_jour = [0] * 7
    for r in all_reservations:
        if r["statut"] == "confirmee":
            try:
                d = datetime.fromisoformat(r["date_debut"])
                resas_par_jour[d.weekday()] += 1
            except Exception:
                pass

    total    = len(all_reservations)
    annulees = len([r for r in all_reservations if r["statut"] == "annulee"])
    taux_annulation = int(annulees / total * 100) if total > 0 else 0

    semaine_passee = now - timedelta(days=7)
    resas_semaine = [
        r for r in all_reservations
        if r["statut"] == "confirmee"
        and datetime.fromisoformat(r["date_creation"]).astimezone(TZ_PARIS) > semaine_passee
    ]

    all_users = {u["username"]: u for u in db.get_all_users()}
    resas_par_promo = defaultdict(int)
    for r in all_reservations:
        if r["statut"] == "confirmee":
            promo = all_users.get(r["user"], {}).get("promotion", "Inconnue") or "Inconnue"
            resas_par_promo[promo] += 1
    top_promos = sorted(resas_par_promo.items(), key=lambda x: x[1], reverse=True)[:5]

    durees = []
    for r in all_reservations:
        if r["statut"] == "confirmee":
            try:
                d = (datetime.fromisoformat(r["date_fin"]) - datetime.fromisoformat(r["date_debut"])).total_seconds() / 60
                durees.append(d)
            except Exception:
                pass
    duree_moyenne = int(sum(durees) / len(durees)) if durees else 0

    # Métriques collectives : total étudiants servis (organisateurs + participants)
    total_etudiants_servis = sum(
        (r.get("nb_personnes") or 1)
        for r in all_reservations
        if r["statut"] == "confirmee"
    )

    # Taux de remplissage moyen par salle
    remplissage_data = []
    for r in all_reservations:
        if r["statut"] == "confirmee":
            salle_info = db.get_salle(r["salle"])
            if salle_info:
                try:
                    places_totales = int(salle_info.get("places") or 0)
                    if places_totales > 0:
                        nb_p = r.get("nb_personnes") or 1
                        remplissage_data.append(nb_p / places_totales * 100)
                except (ValueError, TypeError):
                    pass
    taux_remplissage_moyen = int(sum(remplissage_data) / len(remplissage_data)) if remplissage_data else 0

    # Nb moyen de participants par résa (hors solo)
    nb_participants_list = [
        r.get("nb_personnes") or 1
        for r in all_reservations
        if r["statut"] == "confirmee"
    ]
    nb_moyen_participants = round(sum(nb_participants_list) / len(nb_participants_list), 1) if nb_participants_list else 0

    # ── Évolution hebdomadaire : nb de réservations par semaine sur les 8 dernières semaines ──
    NB_SEMAINES = 8
    lundi_courant = now.date() - timedelta(days=now.weekday())
    semaines_bornes = []  # [(lundi, dimanche_fin_journee), ...] de la plus ancienne à la plus récente
    for i in range(NB_SEMAINES - 1, -1, -1):
        lundi_s = lundi_courant - timedelta(weeks=i)
        semaines_bornes.append(lundi_s)

    evolution_counts = [0] * NB_SEMAINES
    for r in all_reservations:
        if r["statut"] != "confirmee":
            continue
        try:
            d = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS).date()
        except Exception:
            continue
        lundi_r = d - timedelta(days=d.weekday())
        for idx, lundi_s in enumerate(semaines_bornes):
            if lundi_r == lundi_s:
                evolution_counts[idx] += 1
                break

    evolution_hebdo = [
        {"label": lundi_s.strftime("%d/%m"), "count": evolution_counts[idx]}
        for idx, lundi_s in enumerate(semaines_bornes)
    ]

    # ── Heatmap horaire : occupation par jour de semaine x tranche horaire (7h-20h) ──
    HEURE_MIN, HEURE_MAX = 7, 20  # bornes inclusives des tranches affichées
    heatmap_grid = [[0] * (HEURE_MAX - HEURE_MIN + 1) for _ in range(7)]
    for r in all_reservations:
        if r["statut"] != "confirmee":
            continue
        try:
            d_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            d_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
        except Exception:
            continue
        # On compte chaque tranche horaire couverte par la réservation, sur son jour de départ
        jour_idx = d_debut.weekday()
        h = max(d_debut.hour, HEURE_MIN)
        h_fin = d_fin.hour if d_fin.date() == d_debut.date() else HEURE_MAX
        h_fin = min(h_fin, HEURE_MAX)
        for heure in range(h, h_fin + 1):
            if HEURE_MIN <= heure <= HEURE_MAX:
                heatmap_grid[jour_idx][heure - HEURE_MIN] += 1

    heatmap_max = max((max(row) for row in heatmap_grid), default=0)
    heatmap_heures = list(range(HEURE_MIN, HEURE_MAX + 1))

    # ── Répartition par site (uniquement pertinent en vue "tous les sites") ──
    repartition_sites = []
    if site_id is None:
        tous_sites = db.get_all_sites(include_inactive=True)
        if len(tous_sites) > 1:
            salles_site_map = {s["nom"]: s.get("site_id") for s in db.get_all_salles(include_inactive=True)}
            compte_par_site = defaultdict(int)
            for r in all_reservations:
                if r["statut"] == "confirmee":
                    sid = salles_site_map.get(r["salle"])
                    compte_par_site[sid] += 1
            total_avec_site = sum(compte_par_site.values()) or 1
            for s in tous_sites:
                c = compte_par_site.get(s["id"], 0)
                repartition_sites.append({
                    "nom":     s["nom"],
                    "couleur": s.get("couleur") or "#0a5ad2",
                    "count":   c,
                    "pct":     round(c / total_avec_site * 100, 1),
                })
            repartition_sites.sort(key=lambda x: x["count"], reverse=True)

    return {
        "top_salles":               top_salles_avec_pct,
        "resas_par_jour":           list(zip(jours, resas_par_jour)),
        "taux_annulation":          taux_annulation,
        "total_reservations":       total,
        "resas_semaine":            len(resas_semaine),
        "top_promos":               top_promos,
        "duree_moyenne":            duree_moyenne,
        "total_etudiants_servis":   total_etudiants_servis,
        "taux_remplissage_moyen":   taux_remplissage_moyen,
        "nb_moyen_participants":    nb_moyen_participants,
        "evolution_hebdo":          evolution_hebdo,
        "heatmap_grid":             heatmap_grid,
        "heatmap_heures":           heatmap_heures,
        "heatmap_max":              heatmap_max,
        "repartition_sites":        repartition_sites,
    }


def get_calendrier_semaine(date_lundi=None):
    now = datetime.now(TZ_PARIS)
    if date_lundi is None:
        date_lundi = now.date() - timedelta(days=now.weekday())

    jours = [date_lundi + timedelta(days=i) for i in range(7)]
    debut_semaine = TZ_PARIS.localize(datetime.combine(jours[0], datetime.min.time()))
    fin_semaine   = TZ_PARIS.localize(datetime.combine(jours[6], datetime.max.time()))

    calendrier = {j.strftime("%Y-%m-%d"): [] for j in jours}

    # ── Réservations étudiants ──
    for r in db.get_all_reservations():
        if r["statut"] != "confirmee":
            continue
        try:
            r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
            r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
            if r_debut <= fin_semaine and r_fin >= debut_semaine:
                jour_key = r_debut.strftime("%Y-%m-%d")
                if jour_key in calendrier:
                    calendrier[jour_key].append({
                        **r,
                        "type":            "reservation",
                        "heure_debut_fmt": r_debut.strftime("%H:%M"),
                        "heure_fin_fmt":   r_fin.strftime("%H:%M"),
                    })
        except Exception:
            pass

    # ── Cours ICS de toutes les salles actives ──
    try:
        salles_actives = [s["nom"] for s in db.get_all_salles(include_inactive=False)]
        for nom_salle in salles_actives:
            try:
                for ev in parse_events(nom_salle):
                    dtstart = ev["dtstart"]
                    dtend   = ev["dtend"]
                    if dtstart <= fin_semaine and dtend >= debut_semaine:
                        jour_key = dtstart.strftime("%Y-%m-%d")
                        if jour_key in calendrier:
                            calendrier[jour_key].append({
                                "type":            "cours",
                                "salle":           nom_salle,
                                "summary":         ev["summary"],
                                "heure_debut_fmt": dtstart.strftime("%H:%M"),
                                "heure_fin_fmt":   dtend.strftime("%H:%M"),
                            })
            except Exception as e:
                app.logger.warning(f"Calendrier semaine : erreur ICS salle {nom_salle} : {e}")
    except Exception as e:
        app.logger.warning(f"Calendrier semaine : erreur chargement cours ICS : {e}")

    return jours, calendrier


# =========================================================
# 🚦 ROUTES FLASK
# =========================================================



# =========================================================
# 🌐 SÉLECTION DE SITE [MULTI-SITES]
# =========================================================

@app.route('/choisir-site', methods=['GET', 'POST'])
@login_required
def choisir_site():
    """
    Affiche et traite le sélecteur de site.
    - Super-admin : peut choisir n'importe quel site ou "tous les sites"
    - Admin de site : redirigé directement (son site est fixe)
    - Utilisateur : choisit son site de travail pour filtrer les salles
    """
    if current_user.is_site_admin():
        # Admin de site : son site est imposé, pas de choix
        return redirect(url_for('index'))

    if request.method == 'POST':
        site_id_raw = request.form.get('site_id', '')
        if site_id_raw == '' or site_id_raw == 'all':
            session.pop('site_id', None)   # vue globale
        else:
            try:
                session['site_id'] = int(site_id_raw)
            except (ValueError, TypeError):
                session.pop('site_id', None)
        next_url = request.form.get('next') or url_for('index')
        return redirect(next_url)

    sites = db.get_all_sites(include_inactive=False)
    return render_template('choisir_site.html', sites=sites,
                           current_site_id=get_current_site_id())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = _get_client_ip()
        _purge_old_entries()

        # ── Vérification du blocage en cours ──────────────────────────────
        blocked, wait_secs = _is_rate_limited(ip)
        if blocked:
            minutes = wait_secs // 60
            secondes = wait_secs % 60
            if minutes > 0:
                duree_msg = f"{minutes} min {secondes:02d} s"
            else:
                duree_msg = f"{secondes} secondes"
            app.logger.warning(f"[rate-limit] Tentative bloquée depuis {ip} ({wait_secs}s restantes).")
            return render_template(
                'login.html',
                error=f"Trop de tentatives échouées. Réessayez dans {duree_msg}."
            )

        # ── Vérification des identifiants ──────────────────────────────────
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        user_row = db.get_user(username)

        if user_row and user_row.get("actif", 1) and check_password_hash(user_row["password_hash"], password):
            # Succès → réinitialiser le compteur
            _reset_attempts(ip)
            user = User(user_row)
            login_user(user)
            # Forcer le changement de mot de passe si demandé
            if user_row.get("must_change_password", 0):
                return redirect(url_for('force_change_password'))
            # ── Redirection post-login sécurisée (protection open redirect) ──
            # On valide que next est un chemin interne avant de l'utiliser.
            # Un next du type https://evil.com est rejeté → redirection par défaut.
            next_url = request.args.get('next')
            if is_safe_url(next_url):
                return redirect(next_url)
            if user.is_admin():
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('index'))
        else:
            # Échec → incrémenter le compteur
            just_locked, lockout_secs = _record_failed_attempt(ip)
            if just_locked:
                minutes = lockout_secs // 60
                secondes = lockout_secs % 60
                if minutes > 0:
                    duree_msg = f"{minutes} min {secondes:02d} s"
                else:
                    duree_msg = f"{secondes} secondes"
                return render_template(
                    'login.html',
                    error=f"Trop de tentatives échouées. Compte temporairement bloqué pour {duree_msg}."
                )

            data = _login_attempts.get(ip, {})
            restantes = MAX_LOGIN_ATTEMPTS - data.get("count", 0)
            restantes = max(0, restantes)
            if restantes > 0:
                return render_template(
                    'login.html',
                    error=f"Identifiants incorrects ou compte désactivé. ({restantes} tentative{'s' if restantes > 1 else ''} restante{'s' if restantes > 1 else ''})"
                )
            return render_template('login.html', error="Identifiants incorrects ou compte désactivé.")

    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


# =========================================================
# 🔑 RÉINITIALISATION MOT DE PASSE
# =========================================================

@app.route('/mot-de-passe-oublie', methods=['GET', 'POST'])
def forgot_password():
    """Formulaire 'Mot de passe oublié' — envoie un lien de réinitialisation."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == 'POST':
        # ── Rate limiting ─────────────────────────────────────────────────────
        # Réutilise le mécanisme du login : MAX_LOGIN_ATTEMPTS requêtes par
        # LOGIN_WINDOW secondes par IP. Protège contre le spam SMTP et
        # l'énumération de comptes par timing.
        ip = _get_client_ip()
        _purge_old_entries()
        blocked, wait_secs = _is_rate_limited(ip)
        if blocked:
            minutes  = wait_secs // 60
            secondes = wait_secs % 60
            duree_msg = f"{minutes} min {secondes:02d} s" if minutes > 0 else f"{secondes} secondes"
            return render_template(
                'forgot_password.html',
                error=f"Trop de demandes. Réessayez dans {duree_msg}."
            )
        _record_failed_attempt(ip)

        username = request.form.get('username', '').strip()
        user_row = db.get_user(username)

        # On répond toujours la même chose pour ne pas révéler si un compte existe
        success_msg = ("Si cet identifiant correspond à un compte actif avec une adresse e-mail, "
                       "un lien de réinitialisation vient d'être envoyé.")

        if user_row and user_row.get('actif', 1) and user_row.get('email'):
            token = db.create_reset_token(username)
            reset_url = url_for('reset_password', token=token, _external=True)
            sent = send_password_reset(
                username=username,
                nom_complet=user_row.get('nom_complet') or username,
                email=user_row['email'],
                reset_url=reset_url,
            )
            if not sent:
                app.logger.error(f"forgot_password: échec envoi e-mail pour {username}")

        return render_template('forgot_password.html', success=success_msg)

    return render_template('forgot_password.html')


@app.route('/reinitialiser/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Page de saisie du nouveau mot de passe via un token valide."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    token_row = db.get_reset_token(token)
    if not token_row:
        return render_template('reset_password.html', error_token=True)

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm      = request.form.get('confirm_password', '')

        if len(new_password) < 8:
            return render_template('reset_password.html', token=token,
                                   error="Le mot de passe doit contenir au moins 8 caractères.")
        if new_password != confirm:
            return render_template('reset_password.html', token=token,
                                   error="Les deux mots de passe ne correspondent pas.")

        ok = db.consume_reset_token(token, new_password)
        if not ok:
            return render_template('reset_password.html', error_token=True)

        return render_template('reset_password.html', success=True)

    return render_template('reset_password.html', token=token)


@app.route('/changer-mon-mot-de-passe', methods=['GET', 'POST'])
@login_required
def force_change_password():
    """Changement de mot de passe forcé après reset admin."""
    # Si le flag n'est pas activé, pas besoin d'être ici
    user_row = db.get_user(current_user.id)
    if not user_row or not user_row.get('must_change_password', 0):
        return redirect(url_for('admin_dashboard') if current_user.is_admin() else url_for('index'))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm      = request.form.get('confirm_password', '')

        if len(new_password) < 8:
            return render_template('force_change_password.html',
                                   error="Le mot de passe doit contenir au moins 8 caractères.")
        if new_password != confirm:
            return render_template('force_change_password.html',
                                   error="Les deux mots de passe ne correspondent pas.")

        db.admin_reset_password(current_user.id, new_password, force_change=False)
        flash("✅ Mot de passe mis à jour avec succès.", "success")
        if current_user.is_admin():
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('index'))

    return render_template('force_change_password.html')


# =========================================================
# 👤 PROFIL UTILISATEUR
# =========================================================

@app.route('/profil', methods=['GET', 'POST'])
@login_required
def profil():
    """
    Page de profil : identité, infos, changement de mot de passe, déconnexion.
    """
    user_row = db.get_user(current_user.id)
    if not user_row:
        abort(404)

    error   = None
    success = None

    if request.method == 'POST':
        action = request.form.get('action', '')

        if action == 'change_password':
            current_pwd = request.form.get('current_password', '')
            new_pwd     = request.form.get('new_password', '').strip()
            confirm_pwd = request.form.get('confirm_password', '').strip()

            if not check_password_hash(user_row['password_hash'], current_pwd):
                error = "Mot de passe actuel incorrect."
            elif len(new_pwd) < 8:
                error = "Le nouveau mot de passe doit contenir au moins 8 caractères."
            elif new_pwd != confirm_pwd:
                error = "Les deux nouveaux mots de passe ne correspondent pas."
            elif check_password_hash(user_row['password_hash'], new_pwd):
                error = "Le nouveau mot de passe doit être différent de l'ancien."
            else:
                db.admin_reset_password(current_user.id, new_pwd, force_change=False)
                success = "✅ Mot de passe mis à jour avec succès."

    # Compteur léger pour l'encart "réservations à venir" dans le profil
    now = datetime.now(TZ_PARIS)
    resas_futures = [
        r for r in db.get_reservations_user(current_user.id)
        if r["statut"] == "confirmee"
        and datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now
    ]

    return render_template(
        'profil.html',
        user=user_row,
        error=error,
        success=success,
        resas_futures=resas_futures,
    )


@app.route('/mes-reservations')
@login_required
def mes_reservations():
    """
    Page dédiée à la gestion des réservations de l'utilisateur connecté :
    à venir (avec bannière + timeline + annulation), passées (avec filtre),
    annulées. Séparée du profil pour une UX distincte.
    """
    user_row = db.get_user(current_user.id)
    if not user_row:
        abort(404)

    all_resas             = db.get_all_reservations_user(current_user.id)
    all_resas_participant = db.get_all_reservations_as_participant(current_user.id)
    now = datetime.now(TZ_PARIS)

    def _fmt_resa(r, role="organisateur"):
        r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
        r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
        r = dict(r)
        r["date_debut_fmt"] = r_debut.strftime("%d/%m/%Y %H:%M")
        r["date_fin_fmt"]   = r_fin.strftime("%H:%M")
        r["duree_min"]      = int((r_fin - r_debut).total_seconds() / 60)
        r["role"]           = role   # "organisateur" | "participant"
        r["r_debut"]        = r_debut
        r["r_fin"]          = r_fin
        # Organisateur (nom complet)
        org_info = db.get_user(r["user"]) or {}
        r["nom_org"] = org_info.get("nom_complet") or r["user"]
        # Participants
        r["participants"] = db.get_participants(r["id"])
        return r

    resas_futures  = []
    resas_passees  = []
    resas_annulees = []

    for r in all_resas:
        r = _fmt_resa(r, role="organisateur")
        if r["statut"] == "annulee":
            resas_annulees.append(r)
        elif r["r_fin"] > now:
            resas_futures.append(r)
        else:
            resas_passees.append(r)

    for r in all_resas_participant:
        r = _fmt_resa(r, role="participant")
        if r["statut"] == "annulee":
            resas_annulees.append(r)
        elif r["r_fin"] > now:
            resas_futures.append(r)
        else:
            resas_passees.append(r)

    resas_futures.sort(key=lambda x: x["r_debut"])
    resas_passees.sort(key=lambda x: x["r_debut"], reverse=True)
    resas_annulees.sort(key=lambda x: x["r_debut"], reverse=True)

    # ── Vue calendrier hebdomadaire ──
    try:
        semaine_offset = int(request.args.get('semaine', 0))
    except (ValueError, TypeError):
        semaine_offset = 0
    planning_semaine = get_planning_user(current_user.id, semaine_offset)

    return render_template(
        'affichage_resa.html',
        user=user_row,
        resas_futures=resas_futures,
        resas_passees=resas_passees,
        resas_annulees=resas_annulees,
        planning_semaine=planning_semaine,
        semaine_offset=semaine_offset,
    )


@app.route('/')
@login_required
def index():
    if current_user.is_admin():
        return redirect(url_for('admin_dashboard'))

    # ── Multi-sites : filtrage par site courant ──────────────────────────────
    current_sid = get_current_site_id()
    salles_db = db.get_salles_by_site(current_sid) if current_sid is not None else db.get_all_salles()

    q             = request.args.get('q')
    f_pc          = request.args.get('pc')
    f_proj        = request.args.get('proj')
    f_tableau     = request.args.get('tableau')
    f_etage       = request.args.get('etage')
    f_aile        = request.args.get('aile')
    f_duree       = request.args.get('duree_min')
    f_heure_debut = request.args.get('heure_debut')
    f_heure_fin   = request.args.get('heure_fin')
    f_cap         = request.args.get('cap')  # filtre capacité (côté client, passé au template pour reset)

    req_start, req_end = None, None
    now = datetime.now(TZ_PARIS)

    if f_heure_debut and f_heure_fin:
        try:
            h_d, m_d = map(int, f_heure_debut.split(':'))
            h_f, m_f = map(int, f_heure_fin.split(':'))
            req_start = now.replace(hour=h_d, minute=m_d, second=0, microsecond=0)
            req_end   = now.replace(hour=h_f, minute=m_f, second=0, microsecond=0)
            if req_end < req_start:
                req_end += timedelta(days=1)
        except ValueError:
            pass
    elif f_duree and f_duree.isdigit():
        req_start = now
        req_end   = now + timedelta(minutes=int(f_duree))

    liste_salles = []

    for salle_row in salles_db:
        nom_salle = salle_row["nom"]
        infos = get_infos_salle(nom_salle)
        etage, aile = detecter_etage_aile(nom_salle, infos)

        keep = True
        if q and q.lower() not in nom_salle.lower() and q.lower() not in infos.get("nom_complet", "").lower():
            keep = False
        if f_pc and not infos.get('pc'):
            keep = False
        if f_proj and not infos.get('projecteur'):
            keep = False
        if f_tableau and not infos.get('tableau'):
            keep = False
        if f_etage and str(etage) != f_etage:
            keep = False
        if f_aile and aile != f_aile:
            keep = False
        if keep and req_start and req_end:
            if not verifier_dispo_creneau(nom_salle, req_start, req_end):
                keep = False

        if keep:
            status    = get_salle_status(nom_salle)
            incidents = get_reports(nom_salle)
            infos['has_issue']  = any(inc.get('statut') != 'resolu' for inc in incidents)
            infos['etage_calc'] = etage
            infos['aile_calc']  = aile
            # ── Multi-sites : enrichir avec infos du site ──────────────────
            site_id_salle = salle_row.get('site_id')
            site_info = None
            if site_id_salle is not None:
                site_info = db.get_site(site_id_salle)
            liste_salles.append({
                'nom':    nom_salle,
                'fichier': nom_salle,  # rétrocompat template
                'status': status,
                'infos':  infos,
                'site_id': site_id_salle,
                'site':    site_info,
            })

    liste_salles.sort(key=lambda x: (x.get('site_id') or 0, x['nom']))

    mes_reservations_raw = [
        r for r in get_reservations_user(current_user.id)
        if r["statut"] == "confirmee"
        and datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now
    ]
    mes_reservations = []
    for r in mes_reservations_raw:
        r = dict(r)
        r_debut = datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS)
        r_fin   = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
        r["duree_min"]    = int((r_fin - r_debut).total_seconds() / 60)
        r["participants"] = db.get_participants(r["id"])
        mes_reservations.append(r)

    # Réservations où l'user est participant (invité par quelqu'un d'autre)
    resas_invitee_raw = [
        r for r in db.get_reservations_as_participant(current_user.id)
        if datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now
    ]
    resas_invitee = []
    for r in resas_invitee_raw:
        r = dict(r)
        org_info = db.get_user(r["user"]) or {}
        r["nom_org"] = org_info.get("nom_complet") or r["user"]
        resas_invitee.append(r)

    return render_template('index.html', salles=liste_salles,
                           q=q, f_pc=f_pc, f_proj=f_proj, f_tableau=f_tableau,
                           f_etage=f_etage, f_aile=f_aile,
                           f_duree=f_duree, f_heure_debut=f_heure_debut, f_heure_fin=f_heure_fin,
                           f_cap=f_cap,
                           mes_reservations=mes_reservations,
                           resas_invitee=resas_invitee,
                           current_site_id=current_sid)


@app.route('/salle/<nom_salle>')
@login_required
def detail(nom_salle):
    if not db.salle_exists(nom_salle):
        return "Salle introuvable", 404

    etat        = get_salle_status(nom_salle)
    infos       = get_infos_salle(nom_salle)
    planning    = get_planning_etendu(nom_salle)
    etage, aile = detecter_etage_aile(nom_salle, infos)
    incidents   = get_reports(nom_salle)
    photos      = db.get_photos_salle(nom_salle)

    ma_reservation = None
    now = datetime.now(TZ_PARIS)
    for r in get_reservations_user(current_user.id):
        if r["salle"] == nom_salle and r["statut"] == "confirmee":
            r_fin = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
            if r_fin > now:
                ma_reservation = r
                break

    # Réservations sur cette salle où l'user est invité (participant)
    resas_invite = []
    for r in db.get_reservations_as_participant(current_user.id):
        if r["salle"] == nom_salle and r["statut"] == "confirmee":
            r_fin = datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS)
            if r_fin > now:
                org_info = db.get_user(r["user"]) or {}
                r = dict(r)
                r["nom_org"] = org_info.get("nom_complet") or r["user"]
                resas_invite.append(r)

    return render_template('detail.html',
                           nom=nom_salle, etat=etat, infos=infos,
                           planning=planning, etage_courant=etage, aile=aile,
                           incidents=incidents, ma_reservation=ma_reservation,
                           resas_invite=resas_invite,
                           horaires_salle=get_horaires_salle(nom_salle),
                           nom_fichier=nom_salle,
                           photos=photos)


@app.route('/signaler/<nom_salle>', methods=['POST'])
@login_required
def signaler(nom_salle):
    if not db.salle_exists(nom_salle):
        abort(404)
    type_pb     = request.form.get('type_probleme', '').strip()
    description = request.form.get('description', '').strip()

    if type_pb and description:
        add_report(nom_salle, type_pb, description)
        flash("✅ Signalement enregistré. Merci !", "success")
    else:
        flash("⚠️ Formulaire incomplet.", "warning")

    return redirect(url_for('detail', nom_salle=nom_salle))


# =========================================================
# 📅 ROUTES RÉSERVATION
# =========================================================

@app.route('/reserver/<nom_salle>', methods=['GET', 'POST'])
@login_required
def reserver(nom_salle):
    if current_user.is_admin():
        abort(403)

    if not db.salle_exists(nom_salle):
        return "Salle introuvable", 404

    infos = get_infos_salle(nom_salle)

    if request.method == 'POST':
        date_str    = request.form.get('date', '')
        heure_debut = request.form.get('heure_debut', '')
        heure_fin   = request.form.get('heure_fin', '')
        motif       = request.form.get('motif', '').strip()

        # Nombre de personnes : calculé depuis participants + 1 (organisateur),
        # ou saisi manuellement si aucun participant nommé
        participants_raw = request.form.getlist('participants[]')
        try:
            nb_personnes_manuel = int(request.form.get('nb_personnes', 1) or 1)
        except (ValueError, TypeError):
            nb_personnes_manuel = 1

        if participants_raw:
            nb_personnes = len(participants_raw) + 1  # participants + organisateur
        else:
            nb_personnes = max(1, nb_personnes_manuel)

        if not motif:
            flash("⚠️ Veuillez indiquer un motif.", "warning")
        else:
            ok, result = reserver_salle(nom_salle, date_str, heure_debut, heure_fin, motif,
                                        current_user.id, nb_personnes=nb_personnes)
            if ok:
                # Sauvegarde des participants ajoutés
                if participants_raw:
                    db.add_participants(result, participants_raw)
                    nb = len(participants_raw)
                    flash(f"✅ Réservation confirmée (#{result}) ! La salle {infos['nom_complet']} vous est réservée. {nb} participant{'s' if nb > 1 else ''} ajouté{'s' if nb > 1 else ''}.", "success")
                else:
                    flash(f"✅ Réservation confirmée (#{result}) ! La salle {infos['nom_complet']} vous est réservée.", "success")

                # ── Notifications ──────────────────────────────────────────────
                lien_salle = url_for('detail', nom_salle=nom_salle)
                # Notification à l'organisateur
                db.create_notification(
                    username=current_user.id,
                    type='reservation',
                    titre=f"Réservation confirmée — {infos.get('nom_complet', nom_salle)}",
                    message=f"{date_str} · {heure_debut}–{heure_fin} · {motif[:60]}",
                    lien=lien_salle,
                )
                # Notifications aux participants invités
                if participants_raw:
                    db.create_notifications_bulk(
                        usernames=participants_raw,
                        type='invitation',
                        titre=f"Invitation — {infos.get('nom_complet', nom_salle)}",
                        message=f"Invité(e) par {current_user.id} · {date_str} · {heure_debut}–{heure_fin}",
                        lien=lien_salle,
                    )
                # ──────────────────────────────────────────────────────────────

                # ── Envoi des e-mails de confirmation ─────────────────────────
                try:
                    date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                    h_d, m_d = map(int, heure_debut.split(':'))
                    h_f, m_f = map(int, heure_fin.split(':'))
                    dt_debut_mail = TZ_PARIS.localize(
                        datetime.combine(date_obj, datetime.min.time().replace(hour=h_d, minute=m_d))
                    )
                    dt_fin_mail = TZ_PARIS.localize(
                        datetime.combine(date_obj, datetime.min.time().replace(hour=h_f, minute=m_f))
                    )
                    org_row = db.get_user(current_user.id) or {}
                    send_reservation_confirmation(
                        resa_id=result,
                        salle_nom=nom_salle,
                        salle_infos=infos,
                        dt_debut=dt_debut_mail,
                        dt_fin=dt_fin_mail,
                        motif=motif,
                        organisateur_user_row=org_row,
                        participants_usernames=participants_raw,
                        db_module=db,
                    )
                except Exception as e:
                    app.logger.error(f"Erreur envoi e-mail confirmation resa #{result} : {e}")
                # ──────────────────────────────────────────────────────────────

                return redirect(url_for('detail', nom_salle=nom_salle))
            else:
                flash(f"❌ {result}", "danger")

    date_defaut = datetime.now(TZ_PARIS).strftime("%Y-%m-%d")
    date_min    = date_defaut
    date_max    = (datetime.now(TZ_PARIS) + timedelta(days=30)).strftime("%Y-%m-%d")

    # ── Multi-sites : passer le site de la salle au template ─────────────
    salle_row_r = db.get_salle(nom_salle)
    site_salle = None
    if salle_row_r and salle_row_r.get('site_id'):
        site_salle = db.get_site(salle_row_r['site_id'])

    return render_template('reserver.html',
                           nom_salle=nom_salle, infos=infos,
                           date_defaut=date_defaut, date_min=date_min, date_max=date_max,
                           nom_fichier_ics=nom_salle,  # rétrocompat template
                           site_salle=site_salle,
                           horaires_salle=get_horaires_salle(nom_salle))


@app.route('/api/users/search')
@login_required
def api_users_search():
    """
    Recherche d'utilisateurs pour l'ajout de participants.
    Paramètre GET : q (terme de recherche, min 2 caractères)
    Retourne jusqu'à 8 étudiants actifs (JSON).
    L'utilisateur connecté est automatiquement exclu des résultats.
    """
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify([])

    results = db.search_users(q, exclude_username=current_user.id, limit=8)
    # Ne renvoyer que les champs utiles (pas de hash mot de passe, etc.)
    safe_results = [
        {
            "username":         r["username"],
            "nom_complet":      r["nom_complet"],
            "promotion":        r.get("promotion") or "",
            "numero_etudiant":  r.get("numero_etudiant") or "",
        }
        for r in results
    ]
    return jsonify(safe_results)


@app.route('/api/creneaux/<nom_salle>')
@login_required
def api_creneaux(nom_salle):
    if not db.salle_exists(nom_salle):
        return jsonify({"ouvert": True, "motif_fermeture": None, "creneaux": []})

    date_str     = request.args.get('date', datetime.now(TZ_PARIS).strftime("%Y-%m-%d"))
    nb_personnes = int(request.args.get('nb_personnes', 1) or 1)

    try:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"ouvert": True, "motif_fermeture": None, "creneaux": []})

    horaires = get_horaires_salle(nom_salle)
    ouvert   = date_obj.weekday() in horaires["jours_ouverture"]
    creneaux = get_creneaux_libres(nom_salle, date_str, nb_personnes) if ouvert else []

    return jsonify({
        "ouvert": ouvert,
        "motif_fermeture": None if ouvert else (
            f"Fermé le {NOMS_JOURS_SEMAINE[date_obj.weekday()]} "
            f"(ouvert {formater_jours_ouverture(horaires['jours_ouverture'])})"
        ),
        "creneaux": creneaux,
    })


@app.route('/ics/<nom_salle>.ics')
@login_required
def proxy_ics(nom_salle):
    """
    Proxy ICS : sert le calendrier d'une salle sans exposer l'URL ENT réelle.
    L'URL iCal (chiffrée en base) est déchiffrée côté serveur uniquement.
    Le navigateur / l'appli agenda ne voit que /ics/<salle>.ics — jamais l'URL ENT.
    """
    if not db.salle_exists(nom_salle):
        abort(404)

    salle_row = db.get_salle(nom_salle)  # ical_url déjà déchiffrée par database.py
    ical_url  = (salle_row or {}).get("ical_url")
    ical_file = (salle_row or {}).get("ical_file") or f"{nom_salle}.ics"

    ics_bytes = None

    # 1. Tentative via URL distante (prioritaire)
    if ical_url:
        try:
            resp = requests.get(ical_url, headers=_build_ent_headers(), timeout=ICS_FETCH_TIMEOUT)
            resp.raise_for_status()
            ics_bytes = resp.content
        except requests.exceptions.RequestException as e:
            app.logger.warning(f"Proxy ICS : erreur fetch {nom_salle} : {e}")

    # 2. Fallback fichier local
    if ics_bytes is None:
        chemin = os.path.join(DOSSIER_CIBLE, ical_file)
        try:
            with open(chemin, 'rb') as f:
                ics_bytes = f.read()
        except Exception:
            abort(404)

    return Response(
        ics_bytes,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f"inline; filename={nom_salle}.ics",
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "X-Content-Type-Options": "nosniff",
        }
    )


@app.route('/annuler/<reservation_id>', methods=['POST'])
@login_required
def annuler(reservation_id):
    # Récupérer infos avant annulation pour la notif
    resa = db.get_reservation(reservation_id)
    ok, msg = annuler_reservation(reservation_id, current_user.id, is_admin=current_user.is_admin())
    if ok:
        flash(f"✅ {msg}", "success")
        if resa:
            db.create_notification(
                username=current_user.id,
                type='annulation',
                titre=f"Réservation annulée — {resa.get('salle', '')}",
                message=f"{resa.get('date_debut','')[:10]} · {resa.get('motif','')[:60]}",
                lien=url_for('detail', nom_salle=resa.get('salle', '')),
            )
    else:
        flash(f"❌ {msg}", "danger")
    next_url = request.form.get('next', url_for('index'))
    return redirect(next_url)


# =========================================================
# 🛡️ ROUTES ADMIN
# =========================================================

@app.route('/admin')
@login_required
@admin_required
def admin_dashboard():
    now = datetime.now(TZ_PARIS)
    # ── Multi-sites : admin de site → limité à son site, super-admin → site session ou tout ──
    # get_current_site_id() gère les deux cas : retourne site_id forcé pour admin de site,
    # ou session['site_id'] pour le super-admin (None = vue globale).
    admin_site_id = get_current_site_id()
    all_reservations = db.get_reservations_by_site(admin_site_id)
    all_reports_dict = db.get_all_reports(site_id=admin_site_id)

    # Filtrer les salles par site courant (None = toutes les salles)
    salles_db = db.get_salles_by_site(admin_site_id, include_inactive=True)
    total_salles = len([s for s in salles_db if s["actif"]])

    reservations_actives = [
        r for r in all_reservations
        if r["statut"] == "confirmee"
        and datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now
    ]

    reservations_passees = [
        r for r in all_reservations
        if datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) <= now
        or r["statut"] == "annulee"
    ]
    reservations_passees.sort(key=lambda x: x["date_debut"], reverse=True)

    en_cours = [
        r for r in reservations_actives
        if datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS) <= now
        and datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now
    ]

    a_venir = [
        r for r in reservations_actives
        if datetime.fromisoformat(r["date_debut"]).astimezone(TZ_PARIS) > now
    ]
    a_venir.sort(key=lambda x: x["date_debut"])

    tous_signalements = []
    for salle, incidents in all_reports_dict.items():
        for inc in incidents:
            tous_signalements.append({**inc, "salle": salle})
    tous_signalements.sort(key=lambda x: x.get("date", ""), reverse=True)

    # ── Affectation par site : enrichissement des tickets pour l'affichage ────
    _sites_map  = {s["id"]: s for s in db.get_all_sites(include_inactive=True)}
    _users_map  = {u["username"]: u for u in db.get_all_users()}
    for sig in tous_signalements:
        sig["site_nom"] = _sites_map.get(sig.get("site_id"), {}).get("nom")
        assignee = _users_map.get(sig.get("admin_assigne"))
        sig["admin_assigne_nom"] = assignee["nom_complet"] if assignee else None

    # Admins assignables pour le formulaire d'affectation :
    # - admin de site : uniquement les admins de son propre site
    # - super-admin   : tous les admins (regroupés par site) pour pouvoir affecter n'importe qui
    if current_user.is_super_admin():
        admins_assignables = [u for u in db.get_all_users() if u["role"] == "admin"]
    else:
        admins_assignables = db.get_admins_for_site(admin_site_id)

    # Vue d'ensemble des affectations (onglet dédié, super-admin uniquement)
    report_assignment_overview = db.get_reports_assignment_overview() if current_user.is_super_admin() else None

    all_users_list = db.get_all_users()
    stats_users    = {}
    for u in all_users_list:
        if u["role"] != "etudiant":
            continue
        uid = u["username"]
        u_reserv = [r for r in all_reservations if r["user"] == uid]
        # Réservations en tant que participant
        try:
            participant_resas = db.get_all_reservations_as_participant(uid)
            nb_participant = len(participant_resas)
        except Exception:
            nb_participant = 0
        stats_users[uid] = {
            "nom_complet":      u["nom_complet"],
            "email":            u.get("email", ""),
            "numero_etudiant":  u.get("numero_etudiant", ""),
            "promotion":        u.get("promotion", ""),
            "actif":            bool(u.get("actif", 1)),
            "total":            len(u_reserv),
            "actives":          len([r for r in u_reserv if r["statut"] == "confirmee" and datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) > now]),
            "annulees":         len([r for r in u_reserv if r["statut"] == "annulee"]),
            "nb_participant":   nb_participant,
        }

    # Carte participants pour les réservations en cours et à venir
    participants_map = {}
    for r in en_cours + a_venir:
        try:
            participants_map[r["id"]] = db.get_participants(r["id"])
        except Exception:
            participants_map[r["id"]] = []

    semaine_param = request.args.get('semaine')
    if semaine_param:
        try:
            date_lundi = datetime.strptime(semaine_param, "%Y-%m-%d").date()
        except Exception:
            date_lundi = None
    else:
        date_lundi = None
    jours_semaine, calendrier_semaine = get_calendrier_semaine(date_lundi)

    lundi_actuel    = jours_semaine[0]
    lundi_precedent = (lundi_actuel - timedelta(weeks=1)).strftime("%Y-%m-%d")
    lundi_suivant   = (lundi_actuel + timedelta(weeks=1)).strftime("%Y-%m-%d")

    stats = get_stats_avancees(admin_site_id)
    blocages_actifs = get_blocages_actifs()

    # Filtres historique
    hist_q_salle  = request.args.get('hist_salle', '')
    hist_q_user   = request.args.get('hist_user', '')
    hist_q_statut = request.args.get('hist_statut', '')
    hist_page     = int(request.args.get('hist_page', 1))
    hist_per_page = 20

    hist_filtrees = reservations_passees
    if hist_q_salle:
        hist_filtrees = [r for r in hist_filtrees if hist_q_salle.lower() in r["salle"].lower()]
    if hist_q_user:
        hist_filtrees = [r for r in hist_filtrees if hist_q_user.lower() in r["user"].lower()]
    if hist_q_statut:
        hist_filtrees = [r for r in hist_filtrees if r["statut"] == hist_q_statut]

    hist_total  = len(hist_filtrees)
    hist_pages  = max(1, (hist_total + hist_per_page - 1) // hist_per_page)
    hist_page   = max(1, min(hist_page, hist_pages))
    hist_offset = (hist_page - 1) * hist_per_page
    hist_page_data = hist_filtrees[hist_offset:hist_offset + hist_per_page]

    # Onglet salles — liste enrichie
    salles_disponibles = [s["nom"] for s in salles_db if s["actif"]]
    utilisateurs_etudiants = [u["username"] for u in all_users_list if u["role"] == "etudiant"]

    salles_list        = []
    salles_avec_status = []
    for salle_row in salles_db:
        nom = salle_row["nom"]
        infos_s   = get_infos_salle(nom)
        nb_photos = len(db.get_photos_salle(nom))
        etage_s, aile_s = detecter_etage_aile(nom, infos_s)
        infos_s['etage_calc'] = etage_s
        infos_s['aile_calc']  = aile_s
        salles_list.append({
            'nom':            nom,
            'infos':          infos_s,
            'nb_photos':      nb_photos,
            'actif':          bool(salle_row["actif"]),
            'has_meta':       True,
            # Niveau 1 : on ne passe JAMAIS l'URL en clair dans le contexte template
            # admin_dashboard ; seule la version masquée y est exposée.
            'ical_url_masked': infos_s.get("ical_url_masked"),
            'ical_configured': bool(salle_row.get("ical_url")),
            'ical_file':      salle_row.get("ical_file"),
        })
        if salle_row["actif"]:
            status_s = get_salle_status(nom)
            salles_avec_status.append({
                'nom':    nom,
                'infos':  infos_s,
                'status': status_s,
            })

    # Demandes de changement de mot de passe en attente
    pwd_requests = db.get_pwd_requests(statut='en_attente')

    # Historique des annonces
    annonces_historique = db.get_annonces(limit=30)

    # Liste des promotions disponibles (pour le formulaire d'annonce)
    promotions_existantes = sorted(set(
        u["promotion"] for u in all_users_list
        if u.get("promotion") and u["role"] == "etudiant"
    ))

    # ── Paramètres ──
    etab_settings   = db.get_settings_by_categorie("etablissement")
    all_promotions  = db.get_all_promotions(include_inactive=True)
    admin_user_row  = db.get_user(current_user.id) or {}
    # ── Multi-sites : liste des sites pour le panneau admin ──────────────────
    all_sites_admin = db.get_all_sites(include_inactive=True)
    # admin_site_id est déjà défini en haut de la route (get_current_site_id())

    # ── Liste des comptes admin (pour le panneau Sites du super-admin) ────────
    all_admin_users = [u for u in db.get_all_users() if u["role"] == "admin"]

    # ── Architecture système : nombre de salles par site (onglet "Architecture") ──
    # On recharge TOUTES les salles indépendamment du site actuellement sélectionné
    # par le super-admin, pour avoir une vision globale correcte du système.
    salles_count_by_site = {}
    if current_user.is_super_admin():
        for salle_row in db.get_all_salles(include_inactive=True):
            sid = salle_row.get("site_id")
            salles_count_by_site[sid] = salles_count_by_site.get(sid, 0) + 1
    salles_sans_site = salles_count_by_site.get(None, 0)

    # Salles partiellement occupées (état PARTIEL) — calculé après salles_avec_status
    salles_partielles = [s for s in salles_avec_status if s["status"].get("etat") == "PARTIEL"]

    return render_template('admin.html',
                           reservations_actives=reservations_actives,
                           reservations_passees=hist_page_data,
                           en_cours=en_cours,
                           a_venir=a_venir,
                           tous_signalements=tous_signalements,
                           admins_assignables=admins_assignables,
                           report_assignment_overview=report_assignment_overview,
                           stats_users=stats_users,
                           stats=stats,
                           total_salles=total_salles,
                           now=now,
                           jours_semaine=jours_semaine,
                           calendrier_semaine=calendrier_semaine,
                           lundi_precedent=lundi_precedent,
                           lundi_suivant=lundi_suivant,
                           lundi_actuel=lundi_actuel,
                           hist_page=hist_page,
                           hist_pages=hist_pages,
                           hist_total=hist_total,
                           hist_q_salle=hist_q_salle,
                           hist_q_user=hist_q_user,
                           hist_q_statut=hist_q_statut,
                           salles_disponibles=salles_disponibles,
                           utilisateurs_etudiants=utilisateurs_etudiants,
                           promotions=get_promotions_disponibles(),
                           blocages_actifs=blocages_actifs,
                           salles_list=salles_list,
                           salles_avec_status=salles_avec_status,
                           salles_partielles=salles_partielles,
                           participants_map=participants_map,
                           pwd_requests=pwd_requests,
                           annonces_historique=annonces_historique,
                           promotions_existantes=promotions_existantes,
                           etab_settings=etab_settings,
                           all_promotions=all_promotions,
                           admin_user_row=admin_user_row,
                           all_sites_admin=all_sites_admin,
                           all_admin_users=all_admin_users,
                           admin_site_id=admin_site_id,
                           salles_count_by_site=salles_count_by_site,
                           salles_sans_site=salles_sans_site)


# ── Signalements ──

@app.route('/admin/pwd_request/<int:req_id>/traiter', methods=['POST'])
@login_required
@admin_required
def admin_traiter_pwd_request(req_id):
    """Approuve ou refuse une demande de changement de mot de passe."""
    action      = request.form.get('action', '')
    new_password = request.form.get('new_password', '').strip()

    if action == 'approuver':
        if len(new_password) < 8:
            flash("❌ Le nouveau mot de passe doit faire au moins 8 caractères.", "danger")
            return redirect(url_for('admin_dashboard') + '#tabUtilisateurs')
        ok = db.process_pwd_request(req_id, 'approuvee', current_user.id, new_password)
        if ok:
            flash("✅ Demande approuvée et mot de passe mis à jour.", "success")
        else:
            flash("❌ Demande introuvable.", "danger")

    elif action == 'refuser':
        ok = db.process_pwd_request(req_id, 'refusee', current_user.id)
        if ok:
            flash("✅ Demande refusée.", "success")
        else:
            flash("❌ Demande introuvable.", "danger")

    return redirect(url_for('admin_dashboard') + '#tabUtilisateurs')

@app.route('/admin/supprimer_signalement/<nom_salle>/<int:report_id>', methods=['POST'])
@login_required
@admin_required
def admin_supprimer_signalement(nom_salle, report_id):
    get_report_or_403(report_id)  # 403 si le ticket n'appartient pas au site de l'admin
    if delete_report(nom_salle, report_id):
        flash("✅ Signalement supprimé.", "success")
    else:
        flash("❌ Signalement introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#signalements')


@app.route('/admin/signalement_statut/<nom_salle>/<int:report_id>', methods=['POST'])
@login_required
@admin_required
def admin_signalement_statut(nom_salle, report_id):
    get_report_or_403(report_id)
    nouveau_statut = request.form.get('statut', '')
    note_admin     = request.form.get('note_admin', '').strip()
    if nouveau_statut not in ('nouveau', 'en_cours', 'resolu'):
        flash("❌ Statut invalide.", "danger")
    elif update_report_statut(nom_salle, report_id, nouveau_statut, note_admin):
        flash("✅ Signalement mis à jour.", "success")
    else:
        flash("❌ Signalement introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#signalements')


@app.route('/admin/api/signalement/<int:report_id>')
@login_required
@admin_required
def admin_api_signalement(report_id):
    """Détail complet d'un ticket (infos + commentaires + frise chronologique) en JSON,
    utilisé par la modale de gestion pour un chargement dynamique."""
    get_report_or_403(report_id)
    report = db.get_report_full(report_id)
    if not report:
        return jsonify({"error": "introuvable"}), 404
    # Liste des admins assignables selon le rôle de l'utilisateur courant
    if current_user.is_super_admin():
        report["admins_assignables"] = [
            {"username": u["username"], "nom_complet": u["nom_complet"], "site_id": u.get("site_id")}
            for u in db.get_all_users() if u["role"] == "admin"
        ]
    return jsonify(report)


@app.route('/admin/signalement/<int:report_id>/affecter', methods=['POST'])
@login_required
@admin_required
def admin_affecter_signalement(report_id):
    report = get_report_or_403(report_id)
    admin_username = request.form.get('admin_username', '').strip() or None

    # Un admin de site ne peut affecter qu'à un admin de son propre site.
    if admin_username and current_user.is_site_admin():
        cible = db.get_user(admin_username)
        if not cible or cible.get("role") != "admin" or cible.get("site_id") != current_user.site_id:
            flash("❌ Vous ne pouvez affecter ce ticket qu'à un admin de votre site.", "danger")
            return redirect(url_for('admin_dashboard') + '#signalements')

    if db.assign_report(report_id, admin_username, acteur=current_user.id):
        if admin_username:
            flash("✅ Ticket affecté.", "success")
        else:
            flash("✅ Ticket désaffecté.", "success")
    else:
        flash("❌ Signalement introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#signalements')


@app.route('/admin/signalement/<int:report_id>/priorite', methods=['POST'])
@login_required
@admin_required
def admin_priorite_signalement(report_id):
    get_report_or_403(report_id)
    nouvelle_priorite = request.form.get('priorite', '').strip()
    if db.update_report_priorite(report_id, nouvelle_priorite, acteur=current_user.id):
        flash("✅ Priorité mise à jour.", "success")
    else:
        flash("❌ Priorité invalide ou ticket introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#signalements')


@app.route('/admin/signalement/<int:report_id>/commentaire', methods=['POST'])
@login_required
@admin_required
def admin_ajouter_commentaire_signalement(report_id):
    get_report_or_403(report_id)
    texte = request.form.get('texte', '').strip()
    if texte:
        db.add_report_comment(report_id, current_user.id, texte)
        flash("✅ Commentaire ajouté.", "success")
    else:
        flash("⚠️ Le commentaire est vide.", "warning")
    return redirect(url_for('admin_dashboard') + '#signalements')


@app.route('/admin/signalement/commentaire/<int:comment_id>/modifier', methods=['POST'])
@login_required
@admin_required
def admin_modifier_commentaire_signalement(comment_id):
    texte = request.form.get('texte', '').strip()
    if not texte:
        flash("⚠️ Le commentaire est vide.", "warning")
        return redirect(url_for('admin_dashboard') + '#signalements')
    if db.update_report_comment(comment_id, texte, acteur=current_user.id):
        flash("✅ Commentaire modifié.", "success")
    else:
        flash("❌ Commentaire introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#signalements')


@app.route('/admin/signalement/commentaire/<int:comment_id>/supprimer', methods=['POST'])
@login_required
@admin_required
def admin_supprimer_commentaire_signalement(comment_id):
    if db.delete_report_comment(comment_id, acteur=current_user.id):
        flash("✅ Commentaire supprimé.", "success")
    else:
        flash("❌ Commentaire introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#signalements')


# ── Réservations ──

@app.route('/admin/annuler_reservation/<reservation_id>', methods=['POST'])
@login_required
@admin_required
def admin_annuler_reservation(reservation_id):
    resa = db.get_reservation(reservation_id)
    ok, msg = annuler_reservation(reservation_id, current_user.id, is_admin=True)
    if ok:
        flash(f"✅ {msg}", "success")
        if resa:
            # Notifier l'organisateur
            db.create_notification(
                username=resa['user_id'],
                type='annulation',
                titre=f"Réservation annulée par l'admin — {resa.get('salle', '')}",
                message=f"{resa.get('date_debut','')[:10]} · {resa.get('motif','')[:60]}",
                lien=url_for('detail', nom_salle=resa.get('salle', '')),
            )
            # Notifier les participants
            participants = db.get_participants(resa['id'])
            if participants:
                db.create_notifications_bulk(
                    usernames=[p['username'] for p in participants],
                    type='annulation',
                    titre=f"Réservation annulée — {resa.get('salle', '')}",
                    message=f"{resa.get('date_debut','')[:10]} · {resa.get('motif','')[:60]}",
                    lien=url_for('detail', nom_salle=resa.get('salle', '')),
                )
    else:
        flash(f"❌ {msg}", "danger")
    return redirect(url_for('admin_dashboard') + '#reservations')


@app.route('/admin/reserver_manuel', methods=['POST'])
@login_required
@admin_required
def admin_reserver_manuel():
    nom_salle   = request.form.get('salle', '').strip()
    user_cible  = request.form.get('user_cible', '').strip()
    date_str    = request.form.get('date', '')
    heure_debut = request.form.get('heure_debut', '')
    heure_fin   = request.form.get('heure_fin', '')
    motif       = request.form.get('motif', '').strip()
    try:
        nb_personnes = int(request.form.get('nb_personnes', 1) or 1)
        nb_personnes = max(1, nb_personnes)
    except (ValueError, TypeError):
        nb_personnes = 1

    if not all([nom_salle, user_cible, date_str, heure_debut, heure_fin, motif]):
        flash("❌ Formulaire incomplet.", "danger")
        return redirect(url_for('admin_dashboard') + '#reservations')

    if not db.get_user(user_cible):
        flash("❌ Utilisateur inconnu.", "danger")
        return redirect(url_for('admin_dashboard') + '#reservations')

    ok, result = reserver_salle(nom_salle, date_str, heure_debut, heure_fin,
                                motif, user_cible, force_admin=True,
                                nb_personnes=nb_personnes)
    if ok:
        flash(f"✅ Réservation #{result} créée pour {user_cible} sur la salle {nom_salle}.", "success")
    else:
        flash(f"❌ {result}", "danger")

    return redirect(url_for('admin_dashboard') + '#reservations')


@app.route('/admin/export_reservations')
@login_required
@admin_required
def admin_export_reservations():
    all_reservations = db.get_all_reservations()
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["ID", "Salle", "Utilisateur", "Date début", "Date fin", "Motif", "Statut", "Annulée par", "Date création"])
    for r in all_reservations:
        writer.writerow([
            r.get("id", ""), r.get("salle", ""), r.get("user", ""),
            r.get("date_debut", ""), r.get("date_fin", ""), r.get("motif", ""),
            r.get("statut", ""), r.get("annulee_par", ""), r.get("date_creation", ""),
        ])
    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename=reservations_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"}
    )


@app.route('/admin/reservations/purger', methods=['POST'])
@login_required
@admin_required
def admin_purger_reservations():
    """
    Supprime définitivement une partie ou la totalité de l'historique des réservations
    (réservations terminées et/ou annulées). Action irréversible.

    Deux modes :
    - mode='selection' : supprime uniquement les IDs cochés par l'admin (champ reservation_ids[]).
    - mode='filtre'    : supprime TOUTES les réservations de l'historique correspondant aux
                          filtres actifs (hist_salle/hist_user/hist_statut), pas seulement la
                          page affichée.

    Dans les deux cas, la suppression est strictement cantonnée à l'historique (réservations
    déjà terminées ou annulées) et au site géré par l'admin courant (un admin de site ne peut
    jamais supprimer les réservations d'un autre site, même en forgeant les IDs).
    """
    admin_site_id = get_current_site_id()
    mode          = request.form.get('mode', 'selection')
    confirmation  = request.form.get('confirmation', '').strip().upper()

    if confirmation != 'SUPPRIMER':
        flash("❌ Confirmation invalide : vous devez taper SUPPRIMER pour valider.", "danger")
        return redirect(url_for('admin_dashboard') + '#analyse')

    # ── Périmètre autorisé : historique (terminées/annulées) du site géré ──────
    now = datetime.now(TZ_PARIS)
    all_reservations = db.get_reservations_by_site(admin_site_id)
    reservations_passees = [
        r for r in all_reservations
        if datetime.fromisoformat(r["date_fin"]).astimezone(TZ_PARIS) <= now
        or r["statut"] == "annulee"
    ]

    if mode == 'selection':
        passees_ids     = {r["id"] for r in reservations_passees}
        ids_demandes    = request.form.getlist('reservation_ids')
        ids_a_supprimer = [rid for rid in ids_demandes if rid in passees_ids]

        if not ids_a_supprimer:
            flash("⚠️ Aucune réservation valide n'était sélectionnée.", "warning")
            return redirect(url_for('admin_dashboard') + '#analyse')

    else:  # mode == 'filtre' : purge tout l'historique correspondant aux filtres actifs
        hist_q_salle  = request.form.get('hist_salle', '').strip()
        hist_q_user   = request.form.get('hist_user', '').strip()
        hist_q_statut = request.form.get('hist_statut', '').strip()

        filtrees = reservations_passees
        if hist_q_salle:
            filtrees = [r for r in filtrees if hist_q_salle.lower() in r["salle"].lower()]
        if hist_q_user:
            filtrees = [r for r in filtrees if hist_q_user.lower() in r["user"].lower()]
        if hist_q_statut:
            filtrees = [r for r in filtrees if r["statut"] == hist_q_statut]

        ids_a_supprimer = [r["id"] for r in filtrees]

        if not ids_a_supprimer:
            flash("⚠️ Aucune réservation ne correspond à ces filtres.", "warning")
            return redirect(url_for('admin_dashboard') + '#analyse')

    nb_supprimees = db.delete_reservations(ids_a_supprimer)
    flash(
        f"🗑️ {nb_supprimees} réservation{'s' if nb_supprimees > 1 else ''} "
        f"supprimée{'s' if nb_supprimees > 1 else ''} définitivement de l'historique.",
        "success"
    )
    return redirect(url_for('admin_dashboard') + '#analyse')


# ── Utilisateurs ──

@app.route('/admin/utilisateur/ajouter', methods=['POST'])
@login_required
@admin_required
def admin_ajouter_utilisateur():
    uid             = request.form.get('username', '').strip().lower()
    password        = request.form.get('password', '').strip()
    nom_complet     = request.form.get('nom_complet', '').strip()
    email           = request.form.get('email', '').strip()
    numero_etudiant = request.form.get('numero_etudiant', '').strip()
    promotion       = request.form.get('promotion', '').strip()

    if not uid or not password or not nom_complet:
        flash("❌ Identifiant, mot de passe et nom complet sont obligatoires.", "danger")
    elif len(password) < 8:
        flash("❌ Le mot de passe doit faire au moins 8 caractères.", "danger")
    elif not db.create_user(uid, password, nom_complet, email, numero_etudiant or None, promotion or None):
        flash(f"❌ L'identifiant '{uid}' existe déjà.", "danger")
    else:
        flash(f"✅ Utilisateur '{uid}' créé avec succès.", "success")

    return redirect(url_for('admin_dashboard') + '#utilisateurs')


@app.route('/admin/admin-compte/creer', methods=['POST'])
@login_required
@admin_required
def admin_creer_compte_admin():
    """
    Crée un nouveau compte administrateur (role='admin').
    Réservé au super-admin uniquement.
    Le compte peut être rattaché à un site (admin de site) ou non (super-admin).
    """
    if not current_user.is_super_admin():
        abort(403)

    uid         = request.form.get('username', '').strip().lower()
    password    = request.form.get('password', '').strip()
    nom_complet = request.form.get('nom_complet', '').strip()
    email       = request.form.get('email', '').strip()
    site_id_raw = request.form.get('site_id', '').strip()
    site_id     = int(site_id_raw) if site_id_raw.isdigit() else None

    if not uid or not password or not nom_complet:
        flash("❌ Identifiant, mot de passe et nom complet sont obligatoires.", "danger")
        return redirect(url_for('admin_dashboard') + '#sites')

    if len(password) < 8:
        flash("❌ Le mot de passe doit faire au moins 8 caractères.", "danger")
        return redirect(url_for('admin_dashboard') + '#sites')

    import re as _re
    if not _re.match(r'^[a-z0-9._\-]+$', uid):
        flash("❌ L'identifiant ne doit contenir que des minuscules, chiffres, points, tirets ou underscores.", "danger")
        return redirect(url_for('admin_dashboard') + '#sites')

    ok = db.create_user(uid, password, nom_complet, email,
                        numero_etudiant=None, promotion=None,
                        role='admin', site_id=site_id)
    if not ok:
        flash(f"❌ L'identifiant '{uid}' existe déjà.", "danger")
    else:
        label = f"rattaché au site #{site_id}" if site_id else "super-admin"
        flash(f"✅ Compte admin '{uid}' créé avec succès ({label}).", "success")

    return redirect(url_for('admin_dashboard') + '#sites')


@app.route('/admin/utilisateur/modifier/<uid>', methods=['POST'])
@login_required
@admin_required
def admin_modifier_utilisateur(uid):
    if not db.get_user(uid):
        flash("❌ Utilisateur introuvable.", "danger")
        return redirect(url_for('admin_dashboard') + '#utilisateurs')
    if uid == 'admin':
        flash("❌ Impossible de modifier le compte administrateur principal.", "danger")
        return redirect(url_for('admin_dashboard') + '#utilisateurs')

    nom_complet     = request.form.get('nom_complet', '').strip()
    email           = request.form.get('email', '').strip()
    numero_etudiant = request.form.get('numero_etudiant', '').strip()
    promotion       = request.form.get('promotion', '').strip()
    new_password    = request.form.get('new_password', '').strip()

    if new_password and len(new_password) < 8:
        flash("❌ Le nouveau mot de passe doit faire au moins 8 caractères.", "danger")
        return redirect(url_for('admin_dashboard') + '#utilisateurs')

    db.update_user(uid, nom_complet=nom_complet or None, email=email,
                   numero_etudiant=numero_etudiant, promotion=promotion,
                   new_password=new_password or None)
    flash(f"✅ Profil de '{uid}' mis à jour.", "success")
    return redirect(url_for('admin_dashboard') + '#utilisateurs')


@app.route('/admin/utilisateur/toggle/<uid>', methods=['POST'])
@login_required
@admin_required
def admin_toggle_utilisateur(uid):
    if uid == 'admin':
        flash("❌ Impossible de désactiver le compte administrateur.", "danger")
        return redirect(url_for('admin_dashboard') + '#utilisateurs')
    nouvel_etat = db.toggle_user_actif(uid)
    if nouvel_etat is None:
        flash("❌ Utilisateur introuvable.", "danger")
    else:
        etat = "activé" if nouvel_etat else "désactivé"
        flash(f"✅ Compte '{uid}' {etat}.", "success")
    return redirect(url_for('admin_dashboard') + '#utilisateurs')


# ── Blocages de salles ──

@app.route('/admin/bloquer_salle', methods=['POST'])
@login_required
@admin_required
def admin_bloquer_salle():
    nom_salle   = request.form.get('salle', '').strip()
    date_debut  = request.form.get('date_debut', '').strip()
    heure_debut = request.form.get('heure_debut', '').strip()
    date_fin    = request.form.get('date_fin', '').strip()
    heure_fin   = request.form.get('heure_fin', '').strip()
    motif       = request.form.get('motif', '').strip()

    if not all([nom_salle, date_debut, heure_debut, date_fin, heure_fin, motif]):
        flash("❌ Tous les champs sont obligatoires.", "danger")
        return redirect(url_for('admin_dashboard') + '#blocages')

    try:
        dt_debut = TZ_PARIS.localize(datetime.strptime(f"{date_debut} {heure_debut}", "%Y-%m-%d %H:%M"))
        dt_fin   = TZ_PARIS.localize(datetime.strptime(f"{date_fin} {heure_fin}", "%Y-%m-%d %H:%M"))
        if dt_fin <= dt_debut:
            flash("❌ La date de fin doit être après la date de début.", "danger")
            return redirect(url_for('admin_dashboard') + '#blocages')
    except ValueError:
        flash("❌ Format de date invalide.", "danger")
        return redirect(url_for('admin_dashboard') + '#blocages')

    db.add_blocage(
        id_           = str(uuid.uuid4())[:8],
        salle         = nom_salle,
        date_debut    = dt_debut.isoformat(),
        date_fin      = dt_fin.isoformat(),
        motif         = motif[:200],
        cree_par      = current_user.id,
        date_creation = datetime.now(TZ_PARIS).isoformat(),
    )
    flash(f"✅ Salle {nom_salle} bloquée du {date_debut} {heure_debut} au {date_fin} {heure_fin}.", "success")
    return redirect(url_for('admin_dashboard') + '#blocages')


@app.route('/admin/debloquer_salle/<blocage_id>', methods=['POST'])
@login_required
@admin_required
def admin_debloquer_salle(blocage_id):
    if db.delete_blocage(blocage_id):
        flash("✅ Blocage supprimé.", "success")
    else:
        flash("❌ Blocage introuvable.", "danger")
    return redirect(url_for('admin_dashboard') + '#blocages')


# ═══════════════════════════════════════════════════════════
# 🏫 GESTION DES SALLES (CRUD) — NOUVELLES ROUTES
# ═══════════════════════════════════════════════════════════

@app.route('/admin/salle/ajouter', methods=['POST'])
@login_required
@admin_required
def admin_ajouter_salle():
    """Crée une nouvelle salle en base."""
    nom         = request.form.get('nom', '').strip().upper().replace(' ', '-')
    nom_complet = request.form.get('nom_complet', '').strip()
    places      = request.form.get('places', '').strip() or None
    description = request.form.get('description', '').strip()
    etage       = request.form.get('etage', '0').strip()
    aile        = request.form.get('aile', 'centre').strip()
    ical_url    = request.form.get('ical_url', '').strip() or None
    ical_file   = request.form.get('ical_file', '').strip() or None
    pc          = bool(request.form.get('pc'))
    projecteur  = bool(request.form.get('projecteur'))
    tableau     = bool(request.form.get('tableau'))

    # ── Multi-sites : rattacher la salle au site sélectionné ──────────────
    site_id_raw = request.form.get('site_id', '').strip()
    new_site_id = int(site_id_raw) if site_id_raw.isdigit() else None
    # Un admin de site ne peut rattacher qu'à son propre site
    if current_user.is_site_admin():
        new_site_id = current_user.site_id
    # Si aucun site choisi par le super-admin, utiliser le site courant de la session
    if new_site_id is None and current_user.is_super_admin():
        new_site_id = get_current_site_id()

    if not nom or not nom_complet:
        flash("❌ L'identifiant et le nom complet sont obligatoires.", "danger")
        return redirect(url_for('admin_dashboard') + '#salles')

    if not re.match(r'^[A-Z0-9\-_]+$', nom):
        flash("❌ L'identifiant ne doit contenir que des lettres majuscules, chiffres, tirets ou underscores.", "danger")
        return redirect(url_for('admin_dashboard') + '#salles')

    ok = db.create_salle(
        nom=nom, nom_complet=nom_complet, places=places,
        pc=pc, projecteur=projecteur, tableau=tableau,
        description=description, etage=etage, aile=aile,
        ical_url=ical_url, ical_file=ical_file,
    )
    if ok:
        # Rattacher au site si défini
        if new_site_id is not None:
            db.assign_salle_to_site(nom, new_site_id)
        flash(f"✅ Salle '{nom}' ({nom_complet}) ajoutée avec succès.", "success")
    else:
        flash(f"❌ L'identifiant '{nom}' existe déjà.", "danger")

    return redirect(url_for('admin_dashboard') + '#salles')


@app.route('/admin/salle/<nom_salle>/edit', methods=['GET', 'POST'])
@login_required
@admin_required
def admin_edit_salle(nom_salle):
    """Page dédiée à l'édition complète d'une salle."""
    salle_row = db.get_salle(nom_salle)
    if not salle_row:
        flash(f"❌ Salle '{nom_salle}' introuvable.", "danger")
        return redirect(url_for('admin_dashboard') + '#salles')

    if request.method == 'POST':
        action = request.form.get('action', 'update_infos')

        if action == 'update_infos':
            nom_complet = request.form.get('nom_complet', '').strip()
            places      = request.form.get('places', '').strip()
            description = request.form.get('description', '').strip()
            etage       = request.form.get('etage', '').strip()
            aile        = request.form.get('aile', '').strip()
            ical_url_raw = request.form.get('ical_url', '').strip()
            ical_file   = request.form.get('ical_file', '').strip() or None
            pc          = 1 if request.form.get('pc') else 0
            projecteur  = 1 if request.form.get('projecteur') else 0
            tableau     = 1 if request.form.get('tableau') else 0

            # ── Multi-sites : mise à jour du site de rattachement ──────────
            site_id_raw = request.form.get('site_id', '')
            if current_user.is_site_admin():
                new_site_id = current_user.site_id
                site_changed = False
            elif site_id_raw == '__NONE__':
                new_site_id = None
                site_changed = True
            elif site_id_raw.strip().isdigit():
                new_site_id = int(site_id_raw)
                site_changed = (new_site_id != salle_row.get('site_id'))
            else:
                new_site_id = salle_row.get('site_id')
                site_changed = False

            # Logique URL iCal :
            # - '__CLEAR__' → suppression explicite (bouton "Supprimer l'URL")
            # - ''          → champ non modifié, on conserve la valeur en base
            # - toute autre valeur → nouvelle URL à chiffrer et sauvegarder
            update_kwargs = dict(
                nom_complet=nom_complet or salle_row["nom_complet"],
                places=places if places else salle_row["places"],
                description=description,
                etage=etage or "0",
                aile=aile or "centre",
                ical_file=ical_file,
                pc=pc, projecteur=projecteur, tableau=tableau,
            )

            if ical_url_raw == '__CLEAR__':
                update_kwargs['ical_url'] = None          # suppression
            elif ical_url_raw:
                update_kwargs['ical_url'] = ical_url_raw  # nouvelle valeur
            # else : vide → on n'inclut pas ical_url dans kwargs → pas de modification

            db.update_salle(nom_salle, **update_kwargs)
            if site_changed:
                db.assign_salle_to_site(nom_salle, new_site_id)
            _invalidate_ics_cache(nom_salle)
            flash(f"✅ Informations de '{nom_salle}' mises à jour.", "success")

        return redirect(url_for('admin_edit_salle', nom_salle=nom_salle))

    infos  = get_infos_salle(nom_salle)
    photos = db.get_photos_salle(nom_salle)
    all_sites_list = db.get_all_sites(include_inactive=False)

    return render_template('admin_salle_edit.html',
                           nom_salle=nom_salle, infos=infos,
                           salle=salle_row, photos=photos, meta=salle_row,
                           all_sites=all_sites_list)


@app.route('/admin/salle/<nom_salle>/refresh-ics', methods=['POST'])
@login_required
@admin_required
def admin_refresh_ics(nom_salle):
    """Force l'invalidation du cache ICS pour une salle."""
    _invalidate_ics_cache(nom_salle)
    flash(f"✅ Cache ICS de la salle {nom_salle} vidé. Le prochain accès rechargera les données.", "success")
    return redirect(url_for('admin_dashboard') + '#salles')


@app.route('/admin/salle/<nom_salle>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_salle(nom_salle):
    """Active ou désactive une salle (masquage sans suppression)."""
    nouvel_etat = db.toggle_salle_actif(nom_salle)
    if nouvel_etat is None:
        flash("❌ Salle introuvable.", "danger")
    else:
        etat = "activée" if nouvel_etat else "désactivée (masquée)"
        flash(f"✅ Salle '{nom_salle}' {etat}.", "success")
    return redirect(url_for('admin_dashboard') + '#salles')


@app.route('/admin/salle/<nom_salle>/supprimer', methods=['POST'])
@login_required
@admin_required
def admin_supprimer_salle(nom_salle):
    """Supprime définitivement une salle et toutes ses photos."""
    # Suppression des photos du disque
    photos = db.get_photos_salle(nom_salle)
    for photo in photos:
        filepath = os.path.join(UPLOAD_FOLDER, photo["filename"])
        try:
            os.remove(filepath)
        except OSError:
            pass

    ok = db.delete_salle(nom_salle)
    if ok:
        _invalidate_ics_cache(nom_salle)
        flash(f"✅ Salle '{nom_salle}' supprimée définitivement.", "success")
    else:
        flash("❌ Salle introuvable.", "danger")

    return redirect(url_for('admin_dashboard') + '#salles')


# ── Photos ──

@app.route('/admin/salle/<nom_salle>/photo/upload', methods=['POST'])
@login_required
@admin_required
def admin_upload_photo(nom_salle):
    if not db.get_salle(nom_salle):
        abort(404)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)

    files   = request.files.getlist('photos')
    legende = request.form.get('legende', '').strip()
    added   = 0

    for file in files:
        if not file or file.filename == '':
            continue

        # ── 1. Vérification de l'extension (filtre rapide) ──────────────────
        if not allowed_file(file.filename):
            flash(f"❌ Fichier '{file.filename}' : format non supporté (PNG, JPG, GIF, WebP).", "warning")
            continue

        # ── 2. Vérification de la taille ─────────────────────────────────────
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > MAX_PHOTO_SIZE:
            flash(f"❌ Fichier '{file.filename}' trop lourd (max 5 Mo).", "warning")
            continue

        # ── 3. Validation magic bytes (contenu réel du fichier) ───────────────
        # Protège contre les fichiers malveillants renommés en .jpg/.png.
        # Ex : exploit.php renommé exploit.jpg → rejeté ici car magic bytes invalides.
        if not validate_image_magic(file):
            app.logger.warning(
                f"[upload] Fichier rejeté (magic bytes invalides) : '{file.filename}' "
                f"depuis {_get_client_ip()}"
            )
            flash(f"❌ Fichier '{file.filename}' : contenu invalide ou corrompu.", "warning")
            continue

        ext      = file.filename.rsplit('.', 1)[1].lower()
        ts       = str(int(time.time() * 1000))
        filename = f"{nom_salle}_{ts}_{secure_filename(file.filename)}"
        if len(filename) > 120:
            filename = f"{nom_salle}_{ts}.{ext}"

        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)

        existing = db.get_photos_salle(nom_salle)
        ordre    = (max((p["ordre"] for p in existing), default=-1) + 1)
        db.add_photo_salle(nom_salle, filename, legende, ordre)
        added += 1

    if added > 0:
        flash(f"✅ {added} photo(s) ajoutée(s).", "success")
    return redirect(url_for('admin_edit_salle', nom_salle=nom_salle))


@app.route('/admin/salle/<nom_salle>/photo/<int:photo_id>/delete', methods=['POST'])
@login_required
@admin_required
def admin_delete_photo(nom_salle, photo_id):
    filename = db.delete_photo_salle(photo_id, nom_salle)
    if filename:
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        try:
            os.remove(filepath)
        except OSError:
            pass
        flash("✅ Photo supprimée.", "success")
    else:
        flash("❌ Photo introuvable.", "danger")
    return redirect(url_for('admin_edit_salle', nom_salle=nom_salle))


@app.route('/admin/salle/<nom_salle>/photo/<int:photo_id>/edit', methods=['POST'])
@login_required
@admin_required
def admin_edit_photo(nom_salle, photo_id):
    legende = request.form.get('legende', '').strip()
    ordre_s = request.form.get('ordre', '').strip()
    ordre   = int(ordre_s) if ordre_s.isdigit() else None
    db.update_photo_salle(photo_id, legende=legende, ordre=ordre)
    flash("✅ Photo mise à jour.", "success")
    return redirect(url_for('admin_edit_salle', nom_salle=nom_salle))


# ── Reset mot de passe par l'admin ───────────────────────────────────────────

@app.route('/admin/utilisateur/<username>/reset-password', methods=['POST'])
@login_required
@admin_required
def admin_reset_user_password(username):
    """
    L'admin génère un mot de passe temporaire pour un utilisateur.
    Si l'utilisateur a un e-mail, il est notifié automatiquement.
    Le flag must_change_password est activé : il devra changer son mdp à la connexion.
    """
    import secrets
    import string

    user_row = db.get_user(username)
    if not user_row:
        flash("❌ Utilisateur introuvable.", "danger")
        return redirect(url_for('admin_dashboard') + '#tabUtilisateurs')

    # Générer un mot de passe temporaire lisible (12 caractères)
    alphabet = string.ascii_letters + string.digits
    temp_pwd = ''.join(secrets.choice(alphabet) for _ in range(12))

    ok = db.admin_reset_password(username, temp_pwd, force_change=True)
    if not ok:
        flash("❌ Erreur lors de la réinitialisation.", "danger")
        return redirect(url_for('admin_dashboard') + '#tabUtilisateurs')

    # Notification in-app
    db.create_notification(
        username=username,
        type='reset_pwd',
        titre="Mot de passe réinitialisé par l'administrateur",
        message="Un mot de passe temporaire vous a été attribué. Connectez-vous et changez-le dès que possible.",
        lien=url_for('login'),
    )

    # Notification par e-mail si l'utilisateur a une adresse
    email = user_row.get('email', '').strip()
    email_envoye = False
    if email and '@' in email:
        login_url = url_for('login', _external=True)
        email_envoye = send_password_changed_by_admin(
            username=username,
            nom_complet=user_row.get('nom_complet') or username,
            email=email,
            temp_password=temp_pwd,
            login_url=login_url,
        )

    if email_envoye:
        flash(f"✅ Mot de passe réinitialisé pour {username}. "
              f"Un e-mail de notification a été envoyé à {email}.", "success")
    else:
        # Afficher le mot de passe temporaire dans le flash si pas d'e-mail
        flash(f"✅ Mot de passe temporaire pour {username} : "
              f"<code class='fw-bold'>{temp_pwd}</code>. "
              f"L'utilisateur devra le changer à sa prochaine connexion. "
              f"{'(Pas d\'e-mail configuré — notifiez-le manuellement.)' if not email else '(Échec de l\'envoi e-mail — notifiez-le manuellement.)'}",
              "warning")

    return redirect(url_for('admin_dashboard') + '#tabUtilisateurs')


# =========================================================
# ⚙️  PARAMÈTRES DE L'ÉTABLISSEMENT
# =========================================================

@app.route('/admin/parametres/etablissement', methods=['POST'])
@login_required
@admin_required
def admin_save_etablissement():
    """Sauvegarde les paramètres de l'établissement."""
    if not current_user.is_super_admin():
        abort(403)
    champs = [
        "etablissement_nom", "etablissement_sous_titre",
        "etablissement_adresse", "etablissement_ville",
        "etablissement_email", "etablissement_telephone",
        "etablissement_site_web", "etablissement_couleur",
    ]
    settings_dict = {}
    for cle in champs:
        val = request.form.get(cle, "").strip()
        settings_dict[cle] = val

    db.set_settings_bulk(settings_dict, categorie="etablissement")
    flash("✅ Paramètres de l'établissement mis à jour.", "success")
    return redirect(url_for('admin_dashboard', _tab='parametres'))


# =========================================================
# 🖼️  LOGO DE L'ÉTABLISSEMENT
# =========================================================

LOGO_FOLDER = os.path.join(BASE_DIR, "static", "uploads", "logo")
ALLOWED_LOGO_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
MAX_LOGO_SIZE = 2 * 1024 * 1024   # 2 Mo


@app.route('/admin/parametres/logo', methods=['POST'])
@login_required
@admin_required
def admin_upload_logo():
    """Upload ou suppression du logo de l'établissement."""
    if not current_user.is_super_admin():
        abort(403)

    # ── Suppression du logo ──────────────────────────────────────────────────
    if request.form.get('action') == 'supprimer_logo':
        current_logo = db.get_settings_by_categorie("etablissement").get("etablissement_logo", "")
        if current_logo:
            old_path = os.path.join(LOGO_FOLDER, current_logo)
            try:
                os.remove(old_path)
            except OSError:
                pass
        db.set_settings_bulk({"etablissement_logo": ""}, categorie="etablissement")
        flash("✅ Logo supprimé.", "success")
        return redirect(url_for('admin_dashboard', _tab='parametres'))

    # ── Upload d'un nouveau logo ─────────────────────────────────────────────
    file = request.files.get('logo')
    if not file or file.filename == '':
        flash("❌ Aucun fichier sélectionné.", "warning")
        return redirect(url_for('admin_dashboard', _tab='parametres'))

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        flash("❌ Format non supporté (PNG, JPG, GIF, WebP, SVG).", "warning")
        return redirect(url_for('admin_dashboard', _tab='parametres'))

    # Vérification taille
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > MAX_LOGO_SIZE:
        flash("❌ Logo trop lourd (max 2 Mo).", "warning")
        return redirect(url_for('admin_dashboard', _tab='parametres'))

    # Validation magic bytes (sauf SVG qui est du texte XML)
    if ext != 'svg' and not validate_image_magic(file):
        flash("❌ Contenu du fichier invalide ou corrompu.", "warning")
        return redirect(url_for('admin_dashboard', _tab='parametres'))

    os.makedirs(LOGO_FOLDER, exist_ok=True)

    # Supprimer l'ancien logo s'il existe
    current_logo = db.get_settings_by_categorie("etablissement").get("etablissement_logo", "")
    if current_logo:
        old_path = os.path.join(LOGO_FOLDER, current_logo)
        try:
            os.remove(old_path)
        except OSError:
            pass

    # Sauvegarder le nouveau logo avec un nom unique
    ts = str(int(time.time() * 1000))
    filename = f"logo_{ts}.{ext}"
    filepath = os.path.join(LOGO_FOLDER, filename)
    file.save(filepath)

    db.set_settings_bulk({"etablissement_logo": filename}, categorie="etablissement")
    flash("✅ Logo mis à jour avec succès.", "success")
    return redirect(url_for('admin_dashboard', _tab='parametres'))


# =========================================================
# 🎓 PROMOTIONS
# =========================================================

@app.route('/admin/promotions/ajouter', methods=['POST'])
@login_required
@admin_required
def admin_ajouter_promotion():
    """Ajoute une nouvelle promotion."""
    if not current_user.is_super_admin():
        abort(403)
    nom = request.form.get('nom', '').strip()
    if not nom:
        flash("❌ Le nom de la promotion est obligatoire.", "danger")
        return redirect(url_for('admin_dashboard', _tab='parametres'))

    ok = db.create_promotion(nom)
    if ok:
        flash(f"✅ Promotion '{nom}' ajoutée.", "success")
    else:
        flash(f"❌ La promotion '{nom}' existe déjà.", "danger")
    return redirect(url_for('admin_dashboard', _tab='parametres'))


@app.route('/admin/promotions/<int:promo_id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_promotion(promo_id):
    """Active ou désactive une promotion."""
    if not current_user.is_super_admin():
        abort(403)
    nouvel_etat = db.toggle_promotion_actif(promo_id)
    if nouvel_etat is None:
        flash("❌ Promotion introuvable.", "danger")
    else:
        etat = "activée" if nouvel_etat else "désactivée"
        flash(f"✅ Promotion {etat}.", "success")
    return redirect(url_for('admin_dashboard', _tab='parametres'))


@app.route('/admin/promotions/<int:promo_id>/supprimer', methods=['POST'])
@login_required
@admin_required
def admin_supprimer_promotion(promo_id):
    """Supprime définitivement une promotion."""
    if not current_user.is_super_admin():
        abort(403)
    if db.delete_promotion(promo_id):
        flash("✅ Promotion supprimée.", "success")
    else:
        flash("❌ Promotion introuvable.", "danger")
    return redirect(url_for('admin_dashboard', _tab='parametres'))


@app.route('/admin/promotions/<int:promo_id>/modifier', methods=['POST'])
@login_required
@admin_required
def admin_modifier_promotion(promo_id):
    """Renomme une promotion."""
    if not current_user.is_super_admin():
        abort(403)
    nouveau_nom = request.form.get('nom', '').strip()
    if not nouveau_nom:
        flash("❌ Le nom est obligatoire.", "danger")
        return redirect(url_for('admin_dashboard', _tab='parametres'))
    db.update_promotion(promo_id, nom=nouveau_nom)
    flash(f"✅ Promotion renommée en '{nouveau_nom}'.", "success")
    return redirect(url_for('admin_dashboard', _tab='parametres'))


@app.route('/admin/promotions/reordonner', methods=['POST'])
@login_required
@admin_required
def admin_reordonner_promotions():
    """Reçoit une liste d'IDs ordonnés en JSON et met à jour l'ordre."""
    if not current_user.is_super_admin():
        return jsonify({'ok': False, 'error': 'Accès réservé au super-administrateur.'}), 403
    import json as _json
    try:
        data = _json.loads(request.data)
        ordered_ids = [int(i) for i in data.get('order', [])]
        db.reorder_promotions(ordered_ids)
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


# =========================================================
# 👤 PROFIL ADMINISTRATEUR
# =========================================================

@app.route('/admin/profil', methods=['POST'])
@login_required
@admin_required
def admin_save_profil():
    """
    Permet à l'admin de modifier son propre profil :
    nom complet, e-mail, et optionnellement son mot de passe.
    """
    action = request.form.get('action', '')

    if action == 'update_identity':
        nom_complet = request.form.get('nom_complet', '').strip()
        email       = request.form.get('email', '').strip()
        if not nom_complet:
            flash("❌ Le nom complet est obligatoire.", "danger")
            return redirect(url_for('admin_dashboard', _tab='parametres'))
        db.update_user(current_user.id, nom_complet=nom_complet, email=email)
        flash("✅ Profil mis à jour.", "success")

    elif action == 'change_password':
        from werkzeug.security import check_password_hash as _chk
        user_row    = db.get_user(current_user.id)
        current_pwd = request.form.get('current_password', '')
        new_pwd     = request.form.get('new_password', '').strip()
        confirm_pwd = request.form.get('confirm_password', '').strip()

        if not _chk(user_row['password_hash'], current_pwd):
            flash("❌ Mot de passe actuel incorrect.", "danger")
        elif len(new_pwd) < 8:
            flash("❌ Le nouveau mot de passe doit contenir au moins 8 caractères.", "danger")
        elif new_pwd != confirm_pwd:
            flash("❌ Les deux nouveaux mots de passe ne correspondent pas.", "danger")
        elif _chk(user_row['password_hash'], new_pwd):
            flash("❌ Le nouveau mot de passe doit être différent de l'ancien.", "danger")
        else:
            db.admin_reset_password(current_user.id, new_pwd, force_change=False)
            flash("✅ Mot de passe administrateur mis à jour.", "success")

    return redirect(url_for('admin_dashboard', _tab='parametres'))


# ── Annonces (e-mails de masse) ──

@app.route('/admin/annonce/envoyer', methods=['POST'])
@login_required
@admin_required
def admin_envoyer_annonce():
    """
    Envoie un e-mail d'annonce à un groupe d'utilisateurs.
    Cibles possibles : tous, une promo, plusieurs promos, utilisateurs ciblés.
    """
    sujet       = request.form.get('sujet', '').strip()
    corps       = request.form.get('corps', '').strip()
    cible_type  = request.form.get('cible_type', 'tous').strip()
    # Promotions cochées (liste) ou utilisateurs saisis (texte)
    promos_sel  = request.form.getlist('promos[]')
    users_sel   = request.form.get('users_cibles', '').strip()

    if not sujet:
        flash("❌ Le sujet est obligatoire.", "danger")
        return redirect(url_for('admin_dashboard') + '#tabAnnonces')
    if not corps:
        flash("❌ Le corps du message est obligatoire.", "danger")
        return redirect(url_for('admin_dashboard') + '#tabAnnonces')

    # Construction de cible_value selon le type
    cible_value = None
    if cible_type == 'promotions':
        if not promos_sel:
            flash("❌ Veuillez sélectionner au moins une promotion.", "danger")
            return redirect(url_for('admin_dashboard') + '#tabAnnonces')
        cible_value = ','.join(promos_sel)
    elif cible_type == 'promotion':
        cible_value = request.form.get('promo_unique', '').strip()
        if not cible_value:
            flash("❌ Veuillez choisir une promotion.", "danger")
            return redirect(url_for('admin_dashboard') + '#tabAnnonces')
    elif cible_type == 'utilisateurs':
        if not users_sel:
            flash("❌ Veuillez saisir au moins un identifiant utilisateur.", "danger")
            return redirect(url_for('admin_dashboard') + '#tabAnnonces')
        cible_value = ','.join(u.strip() for u in users_sel.split(',') if u.strip())

    # Récupération des destinataires
    destinataires = db.get_users_by_cible(cible_type, cible_value)
    if not destinataires:
        flash("⚠️ Aucun utilisateur actif trouvé pour cette cible.", "warning")
        return redirect(url_for('admin_dashboard') + '#tabAnnonces')

    # Conversion du corps texte brut → HTML (sauts de ligne → <br/><p>)
    import html as html_module
    corps_escaped = html_module.escape(corps)
    corps_html = ''.join(
        f'<p style="margin:0 0 1em;">{ligne}</p>' if ligne.strip() else '<br>'
        for ligne in corps_escaped.split('\n')
    )

    # Envoi
    admin_row = db.get_user(current_user.id) or {}
    expediteur_nom = admin_row.get('nom_complet') or current_user.id

    try:
        resultats = send_annonce(
            sujet=sujet,
            corps_html=corps_html,
            corps_texte=corps,
            destinataires=destinataires,
            expediteur_nom=expediteur_nom,
        )
    except Exception as e:
        app.logger.error(f"Erreur envoi annonce : {e}")
        flash(f"❌ Erreur lors de l'envoi : {e}", "danger")
        return redirect(url_for('admin_dashboard') + '#tabAnnonces')

    # Sauvegarde dans l'historique
    statut_db = 'erreur' if resultats['echecs'] > 0 and resultats['envoyes'] == 0 else 'envoyee'
    db.save_annonce(
        sujet=sujet,
        corps=corps,
        cible_type=cible_type,
        cible_value=cible_value,
        nb_destinataires=resultats['envoyes'],
        envoye_par=current_user.id,
        statut=statut_db,
    )

    # ── Notifications in-app pour chaque destinataire ──────────────────────
    if destinataires:
        db.create_notifications_bulk(
            usernames=[u['username'] for u in destinataires],
            type='annonce',
            titre=f"📢 {sujet}",
            message=corps[:120] + ('…' if len(corps) > 120 else ''),
            lien=None,
        )
    # ──────────────────────────────────────────────────────────────────────

    msg_parts = [f"✅ Annonce envoyée à {resultats['envoyes']} destinataire(s)."]
    if resultats['echecs']:
        msg_parts.append(f"⚠️ {resultats['echecs']} échec(s) d'envoi.")
    if resultats['sans_email']:
        msg_parts.append(f"ℹ️ {resultats['sans_email']} utilisateur(s) sans adresse e-mail ignoré(s).")
    flash(' '.join(msg_parts), 'success' if resultats['envoyes'] > 0 else 'warning')

    return redirect(url_for('admin_dashboard') + '#tabAnnonces')


# =========================================================
# 🏗️  ADMIN — GESTION DES SITES [MULTI-SITES]
# =========================================================

@app.route('/admin/sites/ajouter', methods=['POST'])
@login_required
@admin_required
def admin_ajouter_site():
    """Crée un nouveau site."""
    if not current_user.is_super_admin():
        abort(403)
    nom              = request.form.get('nom', '').strip()
    adresse          = request.form.get('adresse', '').strip()
    couleur          = request.form.get('couleur', '#0a5ad2').strip()
    timezone         = request.form.get('timezone', 'Europe/Paris').strip()
    heure_ouverture  = request.form.get('heure_ouverture', DEFAULT_HEURE_OUVERTURE).strip()
    heure_fermeture  = request.form.get('heure_fermeture', DEFAULT_HEURE_FERMETURE).strip()
    jours_selectionnes = request.form.getlist('jours_ouverture')

    if not nom:
        flash("❌ Le nom du site est obligatoire.", "danger")
        return redirect(url_for('admin_dashboard', _tab='sites'))

    ok_horaires, err_horaires = valider_horaires(heure_ouverture, heure_fermeture)
    if not ok_horaires:
        flash(f"❌ {err_horaires}", "danger")
        return redirect(url_for('admin_dashboard', _tab='sites'))

    ok_jours, err_jours, jours_csv = valider_jours_ouverture(jours_selectionnes)
    if not ok_jours:
        flash(f"❌ {err_jours}", "danger")
        return redirect(url_for('admin_dashboard', _tab='sites'))

    site_id = db.create_site(nom=nom, adresse=adresse, couleur=couleur, timezone=timezone,
                              heure_ouverture=heure_ouverture, heure_fermeture=heure_fermeture,
                              jours_ouverture=jours_csv)
    if site_id:
        flash(f"✅ Site '{nom}' créé (id={site_id}).", "success")
    else:
        flash("❌ Erreur lors de la création du site.", "danger")
    return redirect(url_for('admin_dashboard', _tab='sites'))


@app.route('/admin/sites/<int:site_id>/modifier', methods=['POST'])
@login_required
@admin_required
def admin_modifier_site(site_id):
    """Modifie un site existant."""
    if not current_user.can_manage_site(site_id):
        abort(403)
    nom              = request.form.get('nom', '').strip()
    adresse          = request.form.get('adresse', '').strip()
    couleur          = request.form.get('couleur', '#0a5ad2').strip()
    timezone         = request.form.get('timezone', 'Europe/Paris').strip()
    heure_ouverture  = request.form.get('heure_ouverture', DEFAULT_HEURE_OUVERTURE).strip()
    heure_fermeture  = request.form.get('heure_fermeture', DEFAULT_HEURE_FERMETURE).strip()
    jours_selectionnes = request.form.getlist('jours_ouverture')

    ok_horaires, err_horaires = valider_horaires(heure_ouverture, heure_fermeture)
    if not ok_horaires:
        flash(f"❌ {err_horaires}", "danger")
        return redirect(url_for('admin_dashboard', _tab='sites'))

    ok_jours, err_jours, jours_csv = valider_jours_ouverture(jours_selectionnes)
    if not ok_jours:
        flash(f"❌ {err_jours}", "danger")
        return redirect(url_for('admin_dashboard', _tab='sites'))

    ok = db.update_site(site_id, nom=nom, adresse=adresse, couleur=couleur, timezone=timezone,
                         heure_ouverture=heure_ouverture, heure_fermeture=heure_fermeture,
                         jours_ouverture=jours_csv)
    if ok:
        flash("✅ Site mis à jour.", "success")
    else:
        flash("❌ Site introuvable.", "danger")
    return redirect(url_for('admin_dashboard', _tab='sites'))


@app.route('/admin/sites/<int:site_id>/supprimer', methods=['POST'])
@login_required
@admin_required
def admin_supprimer_site(site_id):
    """Supprime un site (les salles rattachées perdent leur site_id)."""
    if not current_user.is_super_admin():
        abort(403)
    if db.delete_site(site_id):
        flash("✅ Site supprimé.", "success")
    else:
        flash("❌ Site introuvable.", "danger")
    return redirect(url_for('admin_dashboard', _tab='sites'))


@app.route('/admin/sites/<int:site_id>/toggle', methods=['POST'])
@login_required
@admin_required
def admin_toggle_site(site_id):
    """Active ou désactive un site."""
    if not current_user.can_manage_site(site_id):
        abort(403)
    nouvel_etat = db.toggle_site_actif(site_id)
    if nouvel_etat is None:
        flash("❌ Site introuvable.", "danger")
    else:
        etat = "activé" if nouvel_etat else "désactivé"
        flash(f"✅ Site {etat}.", "success")
    return redirect(url_for('admin_dashboard', _tab='sites'))


@app.route('/admin/sites/assigner-salle', methods=['POST'])
@login_required
@admin_required
def admin_assigner_salle_site():
    """Rattache une salle à un site."""
    nom_salle  = request.form.get('nom_salle', '').strip()
    site_id_s  = request.form.get('site_id', '').strip()
    site_id    = int(site_id_s) if site_id_s.isdigit() else None

    if site_id and not current_user.can_manage_site(site_id):
        abort(403)

    if db.assign_salle_to_site(nom_salle, site_id):
        flash(f"✅ Salle '{nom_salle}' rattachée au site.", "success")
    else:
        flash("❌ Salle introuvable.", "danger")
    return redirect(url_for('admin_dashboard', _tab='sites'))


@app.route('/admin/sites/assigner-admin', methods=['POST'])
@login_required
@admin_required
def admin_assigner_admin_site():
    """Assigne un admin à un site (ou le passe super-admin si site_id vide)."""
    if not current_user.is_super_admin():
        abort(403)
    username   = request.form.get('username', '').strip()
    site_id_s  = request.form.get('site_id', '').strip()
    site_id    = int(site_id_s) if site_id_s.isdigit() else None

    if db.assign_admin_to_site(username, site_id):
        label = f"site {site_id}" if site_id else "super-admin"
        flash(f"✅ Admin '{username}' assigné à {label}.", "success")
    else:
        flash("❌ Utilisateur introuvable ou non-admin.", "danger")
    return redirect(url_for('admin_dashboard', _tab='sites'))


# =========================================================
# 🔔 NOTIFICATIONS
# =========================================================

@app.route('/api/notifications')
@login_required
def api_notifications():
    """Retourne les notifications + le compteur non lu (JSON)."""
    notifs = db.get_notifications(current_user.id, limit=20)
    unread = db.get_unread_count(current_user.id)
    return jsonify({'notifications': notifs, 'unread': unread})


@app.route('/api/notifications/read/<int:notif_id>', methods=['POST'])
@login_required
def api_mark_read(notif_id):
    """Marque une notification comme lue."""
    db.mark_notification_read(notif_id, current_user.id)
    return jsonify({'ok': True})


@app.route('/api/notifications/read-all', methods=['POST'])
@login_required
def api_mark_all_read():
    """Marque toutes les notifications comme lues."""
    count = db.mark_all_read(current_user.id)
    return jsonify({'ok': True, 'count': count})


@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

# =========================================================
# 📺 ROUTE TV
# =========================================================

@app.route('/tv')
@login_required
def tv_mode():
    salles_db = db.get_all_salles()

    liste_salles = []
    for salle_row in salles_db:
        nom_salle = salle_row["nom"]
        infos  = get_infos_salle(nom_salle)
        status = get_salle_status(nom_salle)
        etage, aile = detecter_etage_aile(nom_salle, infos)
        infos['etage_calc'] = etage
        infos['aile_calc']  = aile
        liste_salles.append({'nom': nom_salle, 'status': status, 'infos': infos})

    liste_salles.sort(key=lambda x: (0 if x['status']['etat'] == 'LIBRE' else 1, x['nom']))
    heure_maj = datetime.now(TZ_PARIS).strftime("%H:%M")

    return render_template('tv.html', salles=liste_salles, heure_maj=heure_maj)



@app.route('/tv/<int:site_id>')
@login_required
def tv_mode_site(site_id):
    """Affichage TV filtré pour un site donné."""
    site = db.get_site(site_id)
    if not site:
        abort(404)

    salles_db = db.get_salles_by_site(site_id, include_inactive=False)

    liste_salles = []
    for salle_row in salles_db:
        nom_salle = salle_row["nom"]
        infos  = get_infos_salle(nom_salle)
        status = get_salle_status(nom_salle)
        etage, aile = detecter_etage_aile(nom_salle, infos)
        infos['etage_calc'] = etage
        infos['aile_calc']  = aile
        liste_salles.append({'nom': nom_salle, 'status': status, 'infos': infos})

    liste_salles.sort(key=lambda x: (0 if x['status']['etat'] == 'LIBRE' else 1, x['nom']))
    heure_maj = datetime.now(TZ_PARIS).strftime("%H:%M")

    return render_template('tv.html', salles=liste_salles, heure_maj=heure_maj,
                           site=site, couleur_site=site.get("couleur", "#0a5ad2"))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=DEBUG_MODE)