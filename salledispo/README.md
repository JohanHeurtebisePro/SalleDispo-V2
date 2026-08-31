# SalleDispo 🏫

Application web de gestion et réservation de salles en temps réel, développée avec **Flask** et **SQLite**.

> ⚠️ **Dépôt de démonstration.** Ce projet est publié pour montrer le fonctionnement de l'application (architecture, code, interface). Il n'est pas empaqueté pour être redéployé "clé en main" par un tiers : certaines parties (comptes de démo, données d'établissement, intégration ENT) sont spécifiques à mon contexte d'usage. Voir la section [Statut & licence](#-statut--licence).

## 📸 Aperçu

<!-- Ajouter ici 2-3 captures d'écran une fois le repo en place, ex : -->
<!-- ![Page d'accueil](docs/screenshots/accueil.png) -->
<!-- ![Interface admin](docs/screenshots/admin.png) -->

*(captures d'écran à venir dans `docs/screenshots/`)*

## ✨ Fonctionnalités

**Côté utilisateur**
- Consultation en temps réel de la disponibilité des salles (import iCal / ENT)
- Réservation de créneaux, avec participants additionnels
- Historique et gestion de ses propres réservations
- Signalement de problèmes sur une salle (matériel, propreté, etc.)
- Notifications, annonces de l'établissement
- Gestion du profil (mot de passe, informations) et réinitialisation par e-mail
- Vue "TV" en lecture seule pour affichage dans les couloirs
- Support multi-sites/campus

**Côté administrateur**
- Gestion des salles (CRUD, photos, statut actif/inactif, blocages ponctuels)
- Gestion des utilisateurs et des comptes admin, réinitialisation de mot de passe
- Gestion des sites/campus et des promotions
- Suivi et traitement des signalements (statut, priorité, commentaires)
- Export des réservations, purge, création manuelle de créneaux
- Envoi d'annonces aux utilisateurs
- Paramétrage de l'établissement (logo, horaires, etc.)

## 🛠 Stack technique

| Élément | Choix |
|---|---|
| Backend | Flask 3, Flask-Login, Flask-WTF (CSRF), Flask-Limiter (rate limiting) |
| Sécurité HTTP | Flask-Talisman (en-têtes de sécurité) |
| Base de données | SQLite (via le module standard `sqlite3`) |
| Mots de passe | Hachage `scrypt` (Werkzeug `generate_password_hash`) |
| Calendrier | `icalendar` pour la lecture/génération de flux `.ics` |
| Chiffrement | `cryptography` (Fernet) pour chiffrer les URL iCal sensibles en base |
| E-mails | `smtplib` (SMTP standard, testé avec Gmail) |

## 📁 Structure du projet

```
salledispo/
├── app.py                  # Point d'entrée Flask, routes, logique métier
├── database.py             # Accès SQLite, schéma, migrations légères
├── email_service.py        # Envoi des e-mails (confirmation, reset, annonces)
├── ics_crypto.py           # Chiffrement/déchiffrement des URL iCal (Fernet)
├── templates/              # Templates Jinja2 (Bootstrap)
│   ├── base.html
│   ├── index.html
│   ├── login.html
│   ├── profil.html
│   ├── choisir_site.html
│   ├── reserver.html
│   ├── detail.html
│   ├── detail_refresh.htm
│   ├── affichage_resa.html
│   ├── admin.html
│   ├── admin_salle_edit.html
│   └── tv.html
├── requirements.txt
├── .env.mail.example       # Modèle de config SMTP (non-sensible)
├── .env.secrets.example    # Modèle des secrets (clé Flask, mot de passe SMTP...)
└── docs/screenshots/        # Captures d'écran pour ce README
```

> La base de données (`salledispo.db`) et les fichiers `mail.env` / `secrets.env` **ne sont pas inclus** dans le dépôt (voir ci-dessous pourquoi et comment les recréer).

## 🚀 Installation locale

### 1. Cloner et installer les dépendances

```bash
git clone https://github.com/<votre-utilisateur>/salledispo.git
cd salledispo
python -m venv venv
source venv/bin/activate      # Windows : venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurer les variables d'environnement

```bash
cp .env.mail.example mail.env
cp .env.secrets.example secrets.env
```

Puis éditer ces deux fichiers (ils sont ignorés par Git, donc jamais commités) :

| Fichier | Variable | Description |
|---|---|---|
| `mail.env` | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER` | Serveur SMTP utilisé pour l'envoi d'e-mails |
| `mail.env` | `APP_BASE_URL` | URL publique de l'app (utilisée dans les liens des e-mails) |
| `mail.env` | `EMAIL_ENABLED` | Mettre à `false` pour désactiver l'envoi d'e-mails en local |
| `secrets.env` | `FLASK_SECRET_KEY` | Clé de session Flask **et** base du chiffrement des URL iCal. Générer avec `python -c "import secrets; print(secrets.token_hex(32))"` |
| `secrets.env` | `SMTP_PASSWORD` | Mot de passe d'application SMTP (jamais votre mot de passe principal) |

### 3. Initialiser la base de données

```bash
python database.py
```

Cela crée un fichier `salledispo.db` vide avec le schéma complet, sans aucune donnée réelle.

### 4. Créer un premier compte administrateur

Il n'y a volontairement pas de compte admin par défaut. Pour en créer un :

```bash
python -c "
import database as db
db.init_db()
db.create_user('admin', 'un-mot-de-passe-fort', 'Administrateur', role='admin')
"
```

### 5. Lancer l'application

```bash
python app.py
```

L'application est alors disponible sur `http://localhost:5001`.

## 🔒 Sécurité

Quelques choix faits dans le projet, à titre indicatif :

- **Mots de passe** : hachés avec `scrypt` (jamais stockés en clair), changement forcé possible par un admin.
- **CSRF** : formulaires protégés via Flask-WTF.
- **Rate limiting** : `/login` limité à 5 tentatives / 5 minutes par IP (Flask-Limiter).
- **En-têtes de sécurité** : gérés par Flask-Talisman.
- **URL iCal sensibles** : chiffrées en base (Fernet, dérivé de `FLASK_SECRET_KEY`), voir `ics_crypto.py`.
- **Redirections post-login** : validées pour éviter les *open redirects*.
- **Secrets** : jamais commités (voir `.gitignore`), fournis uniquement via variables d'environnement.

Ce projet reste un **projet étudiant / démonstration** : il n'a pas fait l'objet d'un audit de sécurité formel et ne doit pas être considéré comme prêt pour un usage en production sans revue.

## 🗄️ À propos de la base de données

Le fichier `salledispo.db` original (utilisé en développement) contenait de vraies données (comptes, réservations) et n'est **pas publié** ici pour des raisons de confidentialité. La commande `python database.py` régénère une base vide avec le même schéma, prête à être remplie avec vos propres données de test.

## 📄 Statut & licence

Ce dépôt est publié à titre de **démonstration et de portfolio** : il montre comment l'application est construite, mais n'est pas distribué avec une licence d'utilisation libre. Sauf mention contraire, tous droits sont réservés — n'hésitez pas à me contacter si vous souhaitez réutiliser tout ou partie du code.

## 👤 Auteur

Développé par Heurtebise Johan — https://heurtebisej-pro.com
