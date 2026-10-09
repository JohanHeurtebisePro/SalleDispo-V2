"""
ics_crypto.py — Chiffrement symétrique des URLs iCal (Niveau 2)

Utilise Fernet (AES-128-CBC + HMAC-SHA256) dérivé du FLASK_SECRET_KEY.
Une URL chiffrée commence par le préfixe ENC: pour être détectée facilement.

Usage :
    from ics_crypto import encrypt_url, decrypt_url, is_encrypted

    encrypted = encrypt_url("https://ent.univ.fr/cal.ics")  # → "ENC:gAAAAAB..."
    original  = decrypt_url("ENC:gAAAAAB...")               # → "https://..."
    is_encrypted("ENC:...")                                  # → True
"""

import os
import base64
import hashlib
from cryptography.fernet import Fernet, InvalidToken

# Préfixe pour identifier les URLs chiffrées stockées en base
_ENC_PREFIX = "ENC:"


def _get_fernet() -> Fernet:
    """
    Dérive une clé Fernet 32 octets depuis le FLASK_SECRET_KEY.
    SHA-256(secret) → base64url → clé Fernet valide.
    La clé est reconstruite à chaque appel (stateless, pas de cache global
    pour éviter les problèmes de rechargement de config).
    """
    secret = os.environ.get("FLASK_SECRET_KEY", "cle_par_defaut_dev_a_changer_en_prod")
    # SHA-256 donne toujours 32 octets → encodé en base64url = clé Fernet valide
    key_bytes = hashlib.sha256(secret.encode("utf-8")).digest()
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)


def is_encrypted(value: str | None) -> bool:
    """Retourne True si la valeur est une URL chiffrée (préfixe ENC:)."""
    return bool(value) and value.startswith(_ENC_PREFIX)


def encrypt_url(url: str | None) -> str | None:
    """
    Chiffre une URL en clair.
    - None ou chaîne vide → retourne None.
    - URL déjà chiffrée (ENC:...) → retourne telle quelle (idempotent).
    Retourne une chaîne de la forme : ENC:<token_fernet>
    """
    if not url:
        return None
    if is_encrypted(url):
        return url  # déjà chiffré, on ne double-chiffre pas
    token = _get_fernet().encrypt(url.encode("utf-8"))
    return _ENC_PREFIX + token.decode("utf-8")


def decrypt_url(value: str | None) -> str | None:
    """
    Déchiffre une URL stockée.
    - None ou vide → None.
    - URL en clair (sans préfixe ENC:) → retourne telle quelle (compatibilité
      avec d'anciennes entrées non chiffrées en base).
    - URL chiffrée → déchiffre et retourne l'URL originale.
    - Token invalide ou clé incorrecte → retourne None et log une alerte.
    """
    if not value:
        return None
    if not is_encrypted(value):
        return value  # URL en clair legacy — toujours utilisable
    token = value[len(_ENC_PREFIX):].encode("utf-8")
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken:
        # Clé changée ou données corrompues
        import logging
        logging.getLogger(__name__).error(
            "ics_crypto: impossible de déchiffrer une URL ICS "
            "(clé FLASK_SECRET_KEY changée ou données corrompues)"
        )
        return None


def mask_url_for_display(url: str | None, show_chars: int = 30) -> str:
    """
    Retourne une version masquée pour l'affichage UI (Niveau 1).
    Ex: "https://ent.univ.fr/cal/salle101.ics" → "https://ent.univ.fr/cal/…[masqué]"
    """
    if not url:
        return ""
    if len(url) <= show_chars:
        return url
    return url[:show_chars] + "…[masqué]"