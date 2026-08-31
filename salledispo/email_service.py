"""
email_service.py — Service d'envoi d'e-mails pour SalleDispo

Envoi de mails de confirmation de réservation via SMTP.
Compatible avec n'importe quel serveur SMTP (Gmail, Outlook, serveur IUT, etc.)

Configuration via variables d'environnement :
    SMTP_HOST       Serveur SMTP            (défaut: localhost)
    SMTP_PORT       Port SMTP               (défaut: 587)
    SMTP_USER       Adresse e-mail émetteur (défaut: "")
    SMTP_PASSWORD   Mot de passe SMTP       (défaut: "")
    SMTP_FROM       Adresse "De:"           (défaut: valeur de SMTP_USER)
    SMTP_TLS        Activer STARTTLS        (défaut: true)
    SMTP_SSL        Utiliser SSL direct     (défaut: false, pour port 465)
    APP_BASE_URL    URL de base de l'appli  (défaut: http://localhost:5001)
    EMAIL_ENABLED   Activer les e-mails     (défaut: true)

Usage :
    from email_service import send_reservation_confirmation
    send_reservation_confirmation(resa_id, salle_nom, salle_infos, dt_debut, dt_fin,
                                  motif, organisateur_user, participants_usernames, db_module)
"""

import os
import smtplib
import logging
from email.message import EmailMessage
from datetime import datetime
from typing import Optional



logger = logging.getLogger(__name__)

# ── Configuration SMTP depuis l'environnement ─────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "localhost")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM     = os.getenv("SMTP_FROM", SMTP_USER) or "noreply@salledispo.local"
SMTP_TLS      = os.getenv("SMTP_TLS", "true").lower() == "true"
SMTP_SSL      = os.getenv("SMTP_SSL", "false").lower() == "true"
APP_BASE_URL  = os.getenv("APP_BASE_URL", "http://localhost:5001").rstrip("/")
EMAIL_ENABLED = os.getenv("EMAIL_ENABLED", "true").lower() == "true"


def _get_smtp_connection():
    """Ouvre et retourne une connexion SMTP configurée."""
    if SMTP_SSL:
        conn = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
    else:
        conn = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10)
        if SMTP_TLS:
            conn.starttls()

    if SMTP_USER and SMTP_PASSWORD:
        conn.login(SMTP_USER, SMTP_PASSWORD)

    return conn


def _sanitize_email(addr: str) -> str:
    """
    Supprime tout caractère non-ASCII d'une adresse e-mail.
    Corrige les espaces insécables (\xa0) et autres caractères parasites
    souvent introduits par un copier-coller depuis Word, PDF ou une interface web.
    """
    return addr.encode("ascii", errors="ignore").decode("ascii").strip()

def _encode_subject(subject: str) -> str:
    """
    Nettoie et encode un sujet d'e-mail en RFC 2047 (Base64 UTF-8)
    pour garantir la compatibilité SMTP même avec emojis et accents.
    Remplace aussi les espaces insécables produits par strftime.
    """
    import base64
    cleaned = subject.replace("\xa0", " ").replace("\u202f", " ")
    try:
        cleaned.encode("ascii")
        return cleaned
    except UnicodeEncodeError:
        encoded = base64.b64encode(cleaned.encode("utf-8")).decode("ascii")
        return f"=?utf-8?b?{encoded}?="


def _send_mail(to_addresses: list[str], subject: str, html_body: str, text_body: str) -> bool:
    """
    Envoie un e-mail HTML+texte à une liste d'adresses.
    Retourne True si l'envoi a réussi, False sinon.
    Les adresses vides ou invalides sont filtrées automatiquement.
    """
    recipients = [_sanitize_email(addr) for addr in to_addresses if addr]
    recipients = [addr for addr in recipients if "@" in addr]
    if not recipients:
        logger.warning("email_service: aucune adresse valide, e-mail non envoyé.")
        return False

    if not EMAIL_ENABLED:
        logger.info(f"email_service: désactivé (EMAIL_ENABLED=false). Destinataires: {recipients}")
        return True  # Silencieux en dev

    smtp_from = _sanitize_email(SMTP_FROM)

    msg = EmailMessage()
    msg["Subject"] = _encode_subject(subject)
    msg["From"]    = smtp_from
    msg["To"]      = ", ".join(recipients)
    msg.set_content(text_body, charset="utf-8")
    msg.add_alternative(html_body, subtype="html", charset="utf-8")

    try:
        with _get_smtp_connection() as smtp:
            smtp.send_message(msg)
        logger.info(f"email_service: e-mail envoyé à {recipients} — Sujet: {subject}")
        return True
    except smtplib.SMTPException as e:
        logger.error(f"email_service: erreur SMTP — {e}")
        return False
    except OSError as e:
        logger.error(f"email_service: impossible de joindre le serveur SMTP ({SMTP_HOST}:{SMTP_PORT}) — {e}")
        return False


# ── Templates e-mail ──────────────────────────────────────────────────────────

def _html_confirmation(
    resa_id: str,
    salle_nom: str,
    salle_nom_complet: str,
    dt_debut: datetime,
    dt_fin: datetime,
    motif: str,
    nom_organisateur: str,
    role: str,                      # "organisateur" | "participant"
    participants_infos: list[dict],  # [{"nom_complet": ..., "username": ...}]
    duree_min: int,
    photo_url: Optional[str] = None,
) -> str:
    """Génère le corps HTML de l'e-mail de confirmation."""

    url_detail     = f"{APP_BASE_URL}/salle/{salle_nom}"
    url_mes_resas  = f"{APP_BASE_URL}/mes-reservations"
    date_fmt       = dt_debut.strftime("%A %d %B %Y").replace("\xa0", " ").replace("\u00a0", " ").capitalize()
    heure_debut    = dt_debut.strftime("%H:%M")
    heure_fin      = dt_fin.strftime("%H:%M")
    duree_h        = duree_min // 60
    duree_m        = duree_min % 60
    duree_str      = (f"{duree_h}h" if duree_h else "") + (f"{duree_m}min" if duree_m else "")

    role_label = "Organisateur" if role == "organisateur" else "Participant invité"
    role_color = "#0d6efd" if role == "organisateur" else "#6f42c1"

    # ── En-tête : photo si dispo, sinon fond bleu uni ──
    # position:absolute est ignoré par Gmail/Outlook → on empile image + texte
    # en deux lignes de tableau : photo recadrée en haut, bloc bleu texte en bas.
    if photo_url:
        header_block = f"""
        <tr>
          <td style="border-radius:16px 16px 0 0;overflow:hidden;padding:0;line-height:0;font-size:0;">
            <img src="{photo_url}" width="600" height="180" alt="Photo de la salle"
                 style="display:block;width:100%;height:180px;object-fit:cover;
                        border-radius:16px 16px 0 0;filter:brightness(0.75) saturate(0.9);">
          </td>
        </tr>
        <tr>
          <td style="background:linear-gradient(135deg,#0d6efd,#0950c8);padding:24px 40px;text-align:center;">
            <div style="font-size:1.6rem;margin-bottom:6px;">🏫</div>
            <h1 style="margin:0;color:#fff;font-size:1.5rem;font-weight:800;letter-spacing:-0.5px;">
              Réservation confirmée
            </h1>
            <p style="margin:6px 0 0;color:rgba(255,255,255,0.8);font-size:0.9rem;">
              SalleDispo — IUT
            </p>
          </td>
        </tr>"""
    else:
        header_block = """
        <tr>
          <td style="background:#0d6efd;border-radius:16px 16px 0 0;padding:32px 40px;text-align:center;">
            <div style="font-size:2rem;margin-bottom:8px;">🏫</div>
            <h1 style="margin:0;color:#fff;font-size:1.6rem;font-weight:800;letter-spacing:-0.5px;">
              Réservation confirmée
            </h1>
            <p style="margin:8px 0 0;color:rgba(255,255,255,0.8);font-size:0.95rem;">
              SalleDispo — IUT
            </p>
          </td>
        </tr>"""

    participants_html = ""
    if participants_infos:
        lignes = "".join(
            f'<li style="margin:4px 0;">'
            f'<span style="background:#f1f5f9;border-radius:6px;padding:2px 10px;font-size:0.9rem;">'
            f'{p.get("nom_complet") or p.get("username","?")}'
            f'</span></li>'
            for p in participants_infos
        )
        participants_html = f"""
        <tr>
          <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:0.85rem;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;width:40%;">
            Participants
          </td>
          <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;">
            <ul style="margin:0;padding-left:16px;">{lignes}</ul>
          </td>
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Confirmation de réservation</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:'Helvetica Neue',Arial,sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        {header_block}

        <!-- Corps -->
        <tr>
          <td style="background:#fff;padding:36px 40px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">

            <!-- Badge rôle -->
            <div style="margin-bottom:24px;">
              <span style="background:{role_color}1a;color:{role_color};border:1px solid {role_color}33;
                           border-radius:20px;padding:4px 14px;font-size:0.8rem;font-weight:700;
                           text-transform:uppercase;letter-spacing:0.5px;">
                {role_label}
              </span>
            </div>

            <!-- Bloc salle -->
            <div style="background:#f1f5f9;border-radius:12px;padding:20px 24px;margin-bottom:28px;
                        border-left:4px solid #0d6efd;">
              <div style="font-size:0.75rem;color:#64748b;text-transform:uppercase;font-weight:700;
                          letter-spacing:0.5px;margin-bottom:4px;">Salle réservée</div>
              <div style="font-size:1.5rem;font-weight:900;color:#1e293b;letter-spacing:-0.5px;">
                {salle_nom_complet}
              </div>
              <div style="font-size:0.85rem;color:#64748b;margin-top:4px;">Code : {salle_nom}</div>
            </div>

            <!-- Détails -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:28px;">
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:0.85rem;
                           font-weight:600;text-transform:uppercase;letter-spacing:0.5px;width:40%;">
                  Date
                </td>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-weight:600;color:#1e293b;">
                  {date_fmt}
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:0.85rem;
                           font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">
                  Horaire
                </td>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-weight:600;color:#1e293b;">
                  {heure_debut} → {heure_fin}
                  <span style="color:#64748b;font-weight:400;font-size:0.85rem;margin-left:8px;">
                    ({duree_str})
                  </span>
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:0.85rem;
                           font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">
                  Motif
                </td>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#1e293b;">
                  {motif}
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:0.85rem;
                           font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">
                  Organisateur
                </td>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;font-weight:600;color:#1e293b;">
                  {nom_organisateur}
                </td>
              </tr>
              <tr>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;color:#64748b;font-size:0.85rem;
                           font-weight:600;text-transform:uppercase;letter-spacing:0.5px;">
                  Réf.
                </td>
                <td style="padding:10px 0;border-bottom:1px solid #f1f5f9;">
                  <code style="background:#f1f5f9;border-radius:4px;padding:2px 8px;
                               font-size:0.85rem;color:#0d6efd;">#{resa_id}</code>
                </td>
              </tr>
              {participants_html}
            </table>

            <!-- CTA -->
            <div style="text-align:center;margin-bottom:16px;">
              <a href="{url_detail}"
                 style="background:#0d6efd;color:#fff;border-radius:50px;padding:14px 32px;
                        font-weight:700;font-size:0.95rem;text-decoration:none;display:inline-block;">
                Voir la salle
              </a>
            </div>
            <div style="text-align:center;">
              <a href="{url_mes_resas}"
                 style="color:#64748b;font-size:0.85rem;text-decoration:none;">
                Gérer mes réservations →
              </a>
            </div>

          </td>
        </tr>

        <!-- Pied -->
        <tr>
          <td style="background:#f1f5f9;border-radius:0 0 16px 16px;padding:20px 40px;
                     text-align:center;border:1px solid #e2e8f0;border-top:none;">
            <p style="margin:0;font-size:0.78rem;color:#94a3b8;line-height:1.6;">
              Cet e-mail a été envoyé automatiquement par <strong>SalleDispo</strong>.<br>
              Ne pas répondre à ce message.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _text_confirmation(
    resa_id: str,
    salle_nom: str,
    salle_nom_complet: str,
    dt_debut: datetime,
    dt_fin: datetime,
    motif: str,
    nom_organisateur: str,
    role: str,
    participants_infos: list[dict],
    duree_min: int,
) -> str:
    """Version texte brut de l'e-mail de confirmation."""
    date_fmt   = dt_debut.strftime("%A %d %B %Y").replace("\xa0", " ").replace("\u00a0", " ").capitalize()
    heure_debut = dt_debut.strftime("%H:%M")
    heure_fin   = dt_fin.strftime("%H:%M")
    duree_h     = duree_min // 60
    duree_m     = duree_min % 60
    duree_str   = (f"{duree_h}h" if duree_h else "") + (f"{duree_m}min" if duree_m else "")
    role_label  = "Organisateur" if role == "organisateur" else "Participant invité"

    parts_txt = ""
    if participants_infos:
        noms = ", ".join(p.get("nom_complet") or p.get("username", "?") for p in participants_infos)
        parts_txt = f"\nParticipants     : {noms}"

    return f"""SalleDispo — Confirmation de réservation
==========================================

Rôle              : {role_label}

SALLE             : {salle_nom_complet} ({salle_nom})
Date              : {date_fmt}
Horaire           : {heure_debut} → {heure_fin} ({duree_str})
Motif             : {motif}
Organisateur      : {nom_organisateur}
Référence         : #{resa_id}{parts_txt}

Voir la salle     : {APP_BASE_URL}/salle/{salle_nom}
Mes réservations  : {APP_BASE_URL}/mes-reservations

--
Cet e-mail a été envoyé automatiquement par SalleDispo.
Ne pas répondre à ce message.
"""


# ── Point d'entrée principal ──────────────────────────────────────────────────

def send_reservation_confirmation(
    resa_id: str,
    salle_nom: str,
    salle_infos: dict,
    dt_debut: datetime,
    dt_fin: datetime,
    motif: str,
    organisateur_user_row: dict,
    participants_usernames: list[str],
    db_module,
) -> dict[str, bool]:
    """
    Envoie un e-mail de confirmation à l'organisateur ET à tous les participants.

    Paramètres :
        resa_id                 Identifiant de la réservation (ex: "a1b2c3d4")
        salle_nom               Nom technique de la salle (ex: "B103")
        salle_infos             Dict retourné par get_infos_salle()
        dt_debut / dt_fin       datetimes timezone-aware (Europe/Paris)
        motif                   Motif de la réservation
        organisateur_user_row   Dict utilisateur de l'organisateur (depuis db.get_user())
        participants_usernames  Liste des usernames des participants
        db_module               Module database (pour get_user())

    Retourne :
        {"organisateur": bool, "participants": bool}
        True = envoi réussi (ou adresse manquante/feature désactivée)
        False = erreur d'envoi
    """
    salle_nom_complet = salle_infos.get("nom_complet", salle_nom)
    nom_organisateur  = organisateur_user_row.get("nom_complet") or organisateur_user_row.get("username", "?")
    duree_min         = int((dt_fin - dt_debut).total_seconds() / 60)

    # Photo de la salle pour l'en-tête du mail (première photo si disponible)
    photo_url = None
    try:
        photos = db_module.get_photos_salle(salle_nom)
        if photos:
            first_photo = sorted(photos, key=lambda p: p.get("ordre", 0))[0]
            photo_url = f"{APP_BASE_URL}/static/uploads/salles/{first_photo['filename']}"
    except Exception:
        photo_url = None

    # Récupère les infos complètes des participants (nom + email)
    participants_infos = []
    for uname in participants_usernames:
        row = db_module.get_user(uname)
        if row:
            participants_infos.append(row)

    results = {"organisateur": True, "participants": True}

    # ── E-mail organisateur ────────────────────────────────────────────────────
    org_email = (organisateur_user_row.get("email") or "").strip()
    if org_email:
        html = _html_confirmation(
            resa_id, salle_nom, salle_nom_complet,
            dt_debut, dt_fin, motif, nom_organisateur,
            role="organisateur",
            participants_infos=participants_infos,
            duree_min=duree_min,
            photo_url=photo_url,
        )
        txt = _text_confirmation(
            resa_id, salle_nom, salle_nom_complet,
            dt_debut, dt_fin, motif, nom_organisateur,
            role="organisateur",
            participants_infos=participants_infos,
            duree_min=duree_min,
        )
        _heure_org = dt_debut.strftime("%d/%m %H:%M").replace("\xa0", " ").replace("\u202f", " ")
        subject = f"Reservation confirmee - {salle_nom_complet} - {_heure_org}"
        results["organisateur"] = _send_mail([org_email], subject, html, txt)
    else:
        logger.info(f"email_service: organisateur '{organisateur_user_row.get('username')}' sans e-mail, skip.")

    # ── E-mails participants ───────────────────────────────────────────────────
    participant_emails = [
        (p.get("email") or "").strip()
        for p in participants_infos
        if (p.get("email") or "").strip()
    ]

    if participant_emails:
        for participant_row in participants_infos:
            p_email = (participant_row.get("email") or "").strip()
            if not p_email:
                continue
            html = _html_confirmation(
                resa_id, salle_nom, salle_nom_complet,
                dt_debut, dt_fin, motif, nom_organisateur,
                role="participant",
                participants_infos=participants_infos,
                duree_min=duree_min,
                photo_url=photo_url,
            )
            txt = _text_confirmation(
                resa_id, salle_nom, salle_nom_complet,
                dt_debut, dt_fin, motif, nom_organisateur,
                role="participant",
                participants_infos=participants_infos,
                duree_min=duree_min,
            )
            _heure_part = dt_debut.strftime("%d/%m %H:%M").replace("\xa0", " ").replace("\u202f", " ")
            subject = f"Invitation - {salle_nom_complet} reservee par {nom_organisateur} - {_heure_part}"
            ok = _send_mail([p_email], subject, html, txt)
            if not ok:
                results["participants"] = False
    else:
        logger.info("email_service: aucun participant avec e-mail, skip.")

    return results


# ── Envoi d'annonces admin ────────────────────────────────────────────────────

def _html_annonce(sujet: str, corps_html: str, expediteur_nom: str) -> str:
    """Génère le corps HTML d'une annonce admin."""
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{sujet}</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- En-tête -->
        <tr>
          <td style="background:linear-gradient(135deg,#0d6efd,#0950c8);border-radius:16px 16px 0 0;
                     padding:32px 40px;text-align:center;">
            <div style="font-size:2rem;margin-bottom:8px;">📢</div>
            <h1 style="margin:0;color:#fff;font-size:1.4rem;font-weight:800;letter-spacing:-0.5px;">
              {sujet}
            </h1>
            <p style="margin:8px 0 0;color:rgba(255,255,255,0.8);font-size:0.9rem;">
              SalleDispo — IUT · Message de l'administration
            </p>
          </td>
        </tr>

        <!-- Corps -->
        <tr>
          <td style="background:#fff;padding:36px 40px;border-left:1px solid #e2e8f0;
                     border-right:1px solid #e2e8f0;line-height:1.7;color:#1e293b;font-size:0.95rem;">
            {corps_html}
          </td>
        </tr>

        <!-- Pied -->
        <tr>
          <td style="background:#f1f5f9;border-radius:0 0 16px 16px;padding:20px 40px;
                     text-align:center;border:1px solid #e2e8f0;border-top:none;">
            <p style="margin:0;font-size:0.78rem;color:#94a3b8;line-height:1.6;">
              Ce message a été envoyé par <strong>{expediteur_nom}</strong> via <strong>SalleDispo</strong>.<br>
              Ne pas répondre à cet e-mail.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _text_annonce(sujet: str, corps_texte: str, expediteur_nom: str) -> str:
    """Version texte brut d'une annonce admin."""
    return f"""SalleDispo — Annonce de l'administration
==========================================

{sujet}

{corps_texte}

--
Ce message a été envoyé par {expediteur_nom} via SalleDispo.
Ne pas répondre à cet e-mail.
"""


def send_annonce(
    sujet: str,
    corps_html: str,
    corps_texte: str,
    destinataires: list[dict],
    expediteur_nom: str,
) -> dict:
    """
    Envoie une annonce admin à une liste de destinataires.

    Paramètres :
        sujet           Sujet de l'e-mail
        corps_html      Corps HTML (peut contenir du balisage)
        corps_texte     Corps texte brut (fallback)
        destinataires   Liste de dicts utilisateurs (avec champ 'email')
        expediteur_nom  Nom affiché de l'expéditeur

    Retourne :
        {"envoyes": int, "echecs": int, "sans_email": int}
    """
    html = _html_annonce(sujet, corps_html, expediteur_nom)
    txt  = _text_annonce(sujet, corps_texte, expediteur_nom)

    envoyes = 0
    echecs  = 0
    sans_email = 0

    for user in destinataires:
        email = (user.get("email") or "").strip()
        if not email or "@" not in email:
            sans_email += 1
            continue
        ok = _send_mail([email], sujet, html, txt)
        if ok:
            envoyes += 1
        else:
            echecs += 1

    logger.info(
        f"send_annonce: '{sujet}' — {envoyes} envoyés, {echecs} échecs, {sans_email} sans e-mail"
    )
    return {"envoyes": envoyes, "echecs": echecs, "sans_email": sans_email}


# ── E-mail de réinitialisation de mot de passe ───────────────────────────────

def send_password_reset(username: str, nom_complet: str, email: str, reset_url: str) -> bool:
    """
    Envoie un e-mail de réinitialisation de mot de passe avec un lien unique (30 min).

    Paramètres :
        username    Identifiant de l'utilisateur
        nom_complet Nom complet affiché dans l'e-mail
        email       Adresse e-mail du destinataire
        reset_url   URL complète du lien de réinitialisation

    Retourne True si l'envoi a réussi.
    """
    subject = "Réinitialisation de votre mot de passe — SalleDispo"

    html_body = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Réinitialisation du mot de passe</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- En-tête -->
        <tr>
          <td style="background:linear-gradient(135deg,#f59e0b,#d97706);border-radius:16px 16px 0 0;
                     padding:32px 40px;text-align:center;">
            <div style="font-size:2rem;margin-bottom:8px;">🔑</div>
            <h1 style="margin:0;color:#fff;font-size:1.5rem;font-weight:800;letter-spacing:-0.5px;">
              Réinitialisation du mot de passe
            </h1>
            <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:0.9rem;">
              SalleDispo — IUT
            </p>
          </td>
        </tr>

        <!-- Corps -->
        <tr>
          <td style="background:#fff;padding:36px 40px;border-left:1px solid #e2e8f0;
                     border-right:1px solid #e2e8f0;line-height:1.7;color:#1e293b;">

            <p style="margin:0 0 16px;">Bonjour <strong>{nom_complet}</strong>,</p>
            <p style="margin:0 0 24px;color:#475569;">
              Une demande de réinitialisation de mot de passe a été soumise pour le compte
              <code style="background:#f1f5f9;border-radius:4px;padding:2px 8px;font-size:0.85rem;
                           color:#0d6efd;">{username}</code>.
              Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail.
            </p>

            <!-- Bloc lien -->
            <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;
                        padding:20px 24px;margin-bottom:28px;text-align:center;">
              <p style="margin:0 0 16px;font-size:0.85rem;color:#92400e;font-weight:600;">
                Ce lien est valable <strong>30 minutes</strong> et ne peut être utilisé qu'une seule fois.
              </p>
              <a href="{reset_url}"
                 style="display:inline-block;background:#f59e0b;color:#fff;border-radius:50px;
                        padding:14px 32px;font-weight:700;font-size:0.95rem;text-decoration:none;
                        box-shadow:0 4px 14px rgba(245,158,11,0.4);">
                Choisir un nouveau mot de passe
              </a>
            </div>

            <p style="margin:0;font-size:0.82rem;color:#94a3b8;">
              Si le bouton ne fonctionne pas, copiez ce lien dans votre navigateur :<br>
              <span style="color:#0d6efd;word-break:break-all;">{reset_url}</span>
            </p>
          </td>
        </tr>

        <!-- Pied -->
        <tr>
          <td style="background:#f1f5f9;border-radius:0 0 16px 16px;padding:20px 40px;
                     text-align:center;border:1px solid #e2e8f0;border-top:none;">
            <p style="margin:0;font-size:0.78rem;color:#94a3b8;line-height:1.6;">
              Cet e-mail a été envoyé automatiquement par <strong>SalleDispo</strong>.<br>
              Ne pas répondre à ce message.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    text_body = f"""SalleDispo — Réinitialisation du mot de passe
=============================================

Bonjour {nom_complet},

Une demande de réinitialisation de mot de passe a été soumise pour le compte : {username}

Cliquez sur le lien ci-dessous pour choisir un nouveau mot de passe.
Ce lien est valable 30 minutes et ne peut être utilisé qu'une seule fois.

{reset_url}

Si vous n'êtes pas à l'origine de cette demande, ignorez cet e-mail.

--
SalleDispo — IUT
"""

    return _send_mail([email], subject, html_body, text_body)


def send_password_changed_by_admin(username: str, nom_complet: str, email: str,
                                    temp_password: str, login_url: str) -> bool:
    """
    Notifie un utilisateur que l'admin a défini un mot de passe temporaire.
    L'utilisateur devra le changer à sa première connexion.
    """
    subject = "Votre mot de passe a été réinitialisé — SalleDispo"

    html_body = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mot de passe réinitialisé</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:'Helvetica Neue',Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <tr>
          <td style="background:linear-gradient(135deg,#6366f1,#4f46e5);border-radius:16px 16px 0 0;
                     padding:32px 40px;text-align:center;">
            <div style="font-size:2rem;margin-bottom:8px;">🛡️</div>
            <h1 style="margin:0;color:#fff;font-size:1.5rem;font-weight:800;letter-spacing:-0.5px;">
              Mot de passe réinitialisé
            </h1>
            <p style="margin:8px 0 0;color:rgba(255,255,255,0.85);font-size:0.9rem;">
              SalleDispo — IUT · Action administrateur
            </p>
          </td>
        </tr>

        <tr>
          <td style="background:#fff;padding:36px 40px;border-left:1px solid #e2e8f0;
                     border-right:1px solid #e2e8f0;line-height:1.7;color:#1e293b;">

            <p style="margin:0 0 16px;">Bonjour <strong>{nom_complet}</strong>,</p>
            <p style="margin:0 0 24px;color:#475569;">
              L'administrateur a réinitialisé le mot de passe de votre compte
              <code style="background:#f1f5f9;border-radius:4px;padding:2px 8px;font-size:0.85rem;
                           color:#0d6efd;">{username}</code>.
            </p>

            <div style="background:#f0f0ff;border:1px solid #c7d2fe;border-radius:12px;
                        padding:20px 24px;margin-bottom:28px;text-align:center;">
              <p style="margin:0 0 8px;font-size:0.8rem;color:#4338ca;font-weight:700;
                        text-transform:uppercase;letter-spacing:0.5px;">
                Mot de passe temporaire
              </p>
              <div style="font-family:monospace;font-size:1.6rem;font-weight:900;color:#4f46e5;
                          letter-spacing:4px;margin-bottom:12px;">
                {temp_password}
              </div>
              <p style="margin:0;font-size:0.8rem;color:#6366f1;">
                ⚠️ Vous devrez le changer dès votre première connexion.
              </p>
            </div>

            <div style="text-align:center;">
              <a href="{login_url}"
                 style="display:inline-block;background:#4f46e5;color:#fff;border-radius:50px;
                        padding:14px 32px;font-weight:700;font-size:0.95rem;text-decoration:none;">
                Se connecter
              </a>
            </div>
          </td>
        </tr>

        <tr>
          <td style="background:#f1f5f9;border-radius:0 0 16px 16px;padding:20px 40px;
                     text-align:center;border:1px solid #e2e8f0;border-top:none;">
            <p style="margin:0;font-size:0.78rem;color:#94a3b8;line-height:1.6;">
              Cet e-mail a été envoyé automatiquement par <strong>SalleDispo</strong>.<br>
              Ne pas répondre à ce message.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""

    text_body = f"""SalleDispo — Mot de passe réinitialisé par l'administrateur
=============================================================

Bonjour {nom_complet},

L'administrateur a réinitialisé le mot de passe de votre compte : {username}

Mot de passe temporaire : {temp_password}

Vous devrez le changer dès votre première connexion.

Se connecter : {login_url}

--
SalleDispo — IUT
"""

    return _send_mail([email], subject, html_body, text_body) 