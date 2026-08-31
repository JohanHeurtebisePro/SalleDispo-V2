# SalleDispo — Gestionnaire de salles en temps réel

> **V2** — Réécriture complète du projet initial : passage d'un prototype fichier (JSON + ICS locaux) à une application multi-sites avec base de données relationnelle, comptes utilisateurs, réservation réelle et administration complète.

## Sommaire

- [Description](#description)
- [Contexte et besoin](#contexte-et-besoin)
- [Fonctionnalités](#fonctionnalités)
- [Ce qui a changé depuis la V1](#ce-qui-a-changé-depuis-la-v1)
- [Stack technique](#stack-technique)
- [Architecture & fonctionnement interne](#architecture--fonctionnement-interne)
- [Structure du projet](#structure-du-projet)
- [Modèle de données](#modèle-de-données)
- [Installation locale](#installation-locale)
- [Sécurité](#sécurité)
- [Statut & licence](#statut--licence)

---

## Description

**SalleDispo** est une application web destinée aux IUT et universités, qui permet aux étudiants et au personnel de trouver instantanément une salle libre pour travailler, en agrégeant en temps réel les emplois du temps (calendriers ICS) de l'établissement.

Contrairement à un simple visualiseur d'emploi du temps, SalleDispo va plus loin : elle **croise** les cours planifiés (via ICS) avec les **réservations réelles** faites par les utilisateurs sur l'application, pour donner à tout moment une vision fiable et unique de la disponibilité de chaque salle — combien de places restent libres, jusqu'à quelle heure, et pour combien de personnes on peut encore réserver.

## Contexte et besoin

**Le problème.** Les emplois du temps changent constamment, l'information sur l'ENT n'est pas toujours consultable rapidement, et trouver une salle libre implique souvent de vérifier physiquement chaque salle dans les couloirs — ou de se fier à un planning qui ne dit rien des réservations informelles déjà prises par d'autres groupes.

**La solution apportée par SalleDispo :**
- lecture automatique des calendriers ICS des salles (compatibles exports ADE/ENT), en fichier local ou en **URL distante**, avec rafraîchissement à la demande ;
- calcul en temps réel de l'occupation (Libre / Occupé / Bientôt libre), avec le **nombre de places encore disponibles** quand la salle n'est occupée qu'en partie ;
- réservation directe d'un créneau, avec vérification automatique qu'il n'entre pas en conflit avec un cours ou une réservation existante ;
- système de signalement d'incidents matériels, avec suivi de statut et échanges entre utilisateurs et administration ;
- gestion multi-sites, pour un établissement réparti sur plusieurs campus ou bâtiments.

## Fonctionnalités

### Recherche et disponibilité
- **Disponibilité en temps réel**, calculée à partir des cours (ICS) et des réservations, avec gestion précise du fuseau horaire (`Europe/Paris`).
- **Places restantes** : une salle "occupée" par un petit groupe reste réservable pour la capacité restante, calculée à partir du nombre réel de participants inscrits sur chaque réservation.
- **Barre de progression** du cours ou de la réservation en cours, pour savoir en un coup d'œil si la salle se libère bientôt.
- **Filtres multi-critères** : équipements (PC, vidéoprojecteur, tableau), étage, aile du bâtiment, capacité minimale.
- **Heatmap horaire** : vue d'ensemble de l'occupation par jour de la semaine et tranche horaire, pour repérer les créneaux les plus demandés.
- **Choix du site/campus** en session, pour filtrer automatiquement les salles pertinentes.

### Réservation
- Réservation d'un créneau libre, avec sélection du nombre de participants et ajout de participants nommés.
- Vérification automatique des conflits (cours planifié, blocage administratif, autre réservation, capacité dépassée).
- Vue "Mes réservations" : à venir (avec possibilité d'annulation) et passées.
- Blocages ponctuels d'une salle par l'administration (maintenance, événement), invisibles aux utilisateurs comme des créneaux libres.

### Mode Kiosque (écran TV)
- Interface dédiée `/tv` (et `/tv/<site>` pour un affichage filtré par campus), pensée pour les écrans dans les halls ou salles de projet.
- Tri automatique : salles libres mises en avant.
- Mise à jour de l'horaire affiché à chaque chargement de page.

### Signalement et suivi d'incidents
- Formulaire de signalement (panne, ménage, matériel manquant) associé à une salle.
- Historique complet par signalement : changements de statut horodatés et fil de commentaires entre l'utilisateur et l'administration.
- Alerte visible sur le tableau de bord pour prévenir les autres usagers d'un problème en cours.

### Compte utilisateur
- Inscription, connexion sécurisée, gestion du profil.
- Changement de mot de passe depuis le profil (avec jauge de robustesse en direct).
- Réinitialisation de mot de passe par e-mail en cas d'oubli.
- Notifications et annonces envoyées par l'établissement.

### Administration
- Gestion des salles : création, modification, photos, équipements, statut actif/inactif, association à une URL ICS (avec bouton de rafraîchissement manuel) ou à un fichier local.
- Gestion des utilisateurs : création de comptes, y compris comptes administrateurs, réinitialisation de mot de passe côté admin.
- Deux niveaux d'administration : **super-admin** (vue et gestion sur tous les sites) et **admin de site** (limité à son propre campus).
- Gestion des sites/campus (horaires d'ouverture, jours d'ouverture, couleur, logo) et des promotions d'étudiants.
- Traitement des signalements : changement de statut, priorité, réponse aux utilisateurs.
- Envoi d'annonces aux utilisateurs, export des réservations, purge et création manuelle de créneaux.
- Paramétrage général de l'établissement (nom, logo).

## Ce qui a changé depuis la V1

Le projet est parti d'un prototype simple (lecture de fichiers ICS statiques + stockage JSON) pour devenir une application multi-utilisateurs avec base de données. Principaux changements :

| Aspect | V1 (prototype) | V2 (actuelle) |
|---|---|---|
| **Stockage** | Fichiers JSON (`config.json`, `reports.json`) | Base de données relationnelle **SQLite**, 17 tables |
| **Comptes utilisateurs** | Aucun compte, accès libre | Comptes utilisateurs complets (Flask-Login), rôles **utilisateur / admin de site / super-admin** |
| **Sites/campus** | Un seul établissement | Support **multi-sites**, sélection de campus, admin scindé par site |
| **Réservation** | Consultation seule (libre/occupé) | **Réservation réelle** de créneaux, gestion des participants, détection des conflits, capacité restante calculée précisément |
| **Signalements** | Écriture directe dans `reports.json`, sans historique | Table `reports` + `report_events` (historique horodaté) + `report_comments` (échanges) |
| **Sources ICS** | Fichiers `.ics` déposés manuellement dans `salleICS/` | Fichier local **ou** URL distante (ADE/ENT), **chiffrée en base** (Fernet), rafraîchissable depuis l'admin |
| **Mode TV** | Accessible sans authentification | Protégé par connexion (`@login_required`), avec variante par site (`/tv/<site_id>`) |
| **Mot de passe oublié** | Non géré | Réinitialisation par e-mail avec token à usage unique |
| **Notifications** | Aucune | Notifications utilisateur + annonces diffusées par l'établissement |
| **Sécurité** | Aucune protection spécifique | CSRF (Flask-WTF), limitation du taux de requêtes sur `/login` (Flask-Limiter), en-têtes de sécurité (Flask-Talisman), mots de passe hachés (`scrypt`) |
| **Administration** | Aucune interface dédiée | Back-office complet (salles, utilisateurs, sites, promotions, signalements, annonces) |

Le cœur du calcul de disponibilité (lecture ICS, calcul libre/occupé, fuseau horaire) reste dans la même logique que la V1, mais a été étendu pour tenir compte des réservations et des capacités restantes, et non plus seulement des cours.

## Stack technique

| Élément | Choix | Rôle |
|---|---|---|
| Backend | Flask 3 | Routing, logique métier |
| Authentification | Flask-Login | Sessions utilisateurs |
| Formulaires | Flask-WTF | Protection CSRF |
| Anti-abus | Flask-Limiter | Limitation du taux de tentatives de connexion |
| En-têtes HTTP | Flask-Talisman | Sécurisation des réponses HTTP |
| Base de données | SQLite (`sqlite3`) | Persistance de toutes les données métier |
| Calendrier | `icalendar` | Lecture des flux/fichiers `.ics` |
| Fuseaux horaires | `pytz` / `zoneinfo` | Calculs fiables en `Europe/Paris` |
| Chiffrement | `cryptography` (Fernet) | Chiffrement des URL ICS sensibles en base |
| E-mails | `smtplib` | Envoi (confirmation, reset mot de passe, annonces) |
| Frontend | HTML5, Bootstrap 5.3 | Interface responsive, style "glassmorphism" |

## Architecture & fonctionnement interne

1. **Récupération des données ICS.** Pour chaque salle, l'application lit un flux ICS — soit un fichier local, soit une URL distante déchiffrée à la volée (`ics_crypto.py`) — et en extrait les événements du jour.
2. **Calcul de disponibilité.** Ces événements sont croisés avec les réservations enregistrées en base et les blocages administratifs, pour déterminer l'état de la salle (`LIBRE` / `OCCUPÉ` / bientôt libre), le nombre de places encore disponibles et la progression du créneau en cours.
3. **Réservation.** Lorsqu'un utilisateur réserve un créneau, l'application revérifie l'absence de conflit (cours, blocage, autre réservation, capacité) avant d'enregistrer la réservation et ses participants.
4. **Rôles et périmètre.** Chaque compte a un rôle (`user`, `admin` scindé par site, ou `super_admin` sans site associé) qui détermine les salles, utilisateurs et signalements visibles et modifiables.
5. **Notifications.** Les actions clés (réservation, changement de statut d'un signalement, annonce) génèrent des notifications visibles par les utilisateurs concernés, et certains événements déclenchent un e-mail.

## Structure du projet

```
salledispo/
├── app.py # Point d'entrée Flask, routes, logique métier
├── database.py # Accès SQLite, schéma, requêtes
├── email_service.py # Envoi des e-mails (confirmation, reset, annonces)
├── ics_crypto.py # Chiffrement/déchiffrement des URL ICS (Fernet)
├── templates/ # Templates Jinja2 (Bootstrap)
│ ├── base.html # Structure commune (header, navbar)
│ ├── index.html # Tableau de bord principal & filtres
│ ├── detail.html # Vue détaillée d'une salle
│ ├── detail_refresh.htm # Fragment HTML rechargé en AJAX (état d'une salle)
│ ├── reserver.html # Formulaire de réservation d'un créneau
│ ├── affichage_resa.html # Historique des réservations utilisateur
│ ├── choisir_site.html # Sélection du site/campus
│ ├── profil.html # Gestion du compte utilisateur
│ ├── login.html # Connexion
│ ├── tv.html # Mode kiosque (écran TV)
│ ├── admin.html # Back-office administrateur
│ └── admin_salle_edit.html # Édition d'une salle (admin)
├── requirements.txt
├── .env.mail.example # Modèle de config SMTP (non-sensible)
├── .env.secrets.example # Modèle des secrets (clé Flask, mot de passe SMTP...)
└── docs/screenshots/ # Captures d'écran pour ce README
```

> La base de données (`salledispo.db`) et les fichiers `mail.env` / `secrets.env` **ne sont pas inclus** dans le dépôt : ils contiennent des données réelles ou des secrets (voir [Sécurité](#sécurité)).

## Modèle de données

Les 17 tables de la base couvrent quatre grands domaines :

- **Établissement** : `sites`, `salles`, `salle_photos`, `promotions`, `settings`, `annonces`.
- **Comptes** : `users`, `password_reset_tokens`, `pwd_requests`, `notifications`.
- **Réservation** : `reservations`, `reservation_participants`, `blocages`.
- **Signalements** : `reports`, `report_events` (historique), `report_comments` (échanges).

(`sqlite_sequence` est une table technique gérée automatiquement par SQLite pour les identifiants auto-incrémentés.)

## Installation locale

### 1. Cloner et installer les dépendances

```bash
git clone https://github.com/<votre-utilisateur>/salledispo.git
cd salledispo
python -m venv venv
source venv/bin/activate # Windows : venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configurer les variables d'environnement

```bash
cp .env.mail.example mail.env
cp .env.secrets.example secrets.env
```

Puis éditer ces deux fichiers (ignorés par Git, jamais commités) :

| Fichier | Variable | Description |
|---|---|---|
| `mail.env` | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER` | Serveur SMTP utilisé pour l'envoi d'e-mails |
| `mail.env` | `APP_BASE_URL` | URL publique de l'app (utilisée dans les liens des e-mails) |
| `mail.env` | `EMAIL_ENABLED` | Mettre à `false` pour désactiver l'envoi d'e-mails en local |
| `secrets.env` | `FLASK_SECRET_KEY` | Clé de session Flask **et** base du chiffrement des URL ICS. Générer avec `python -c "import secrets; print(secrets.token_hex(32))"` |
| `secrets.env` | `SMTP_PASSWORD` | Mot de passe d'application SMTP (jamais votre mot de passe principal) |

### 3. Initialiser la base de données

```bash
python database.py
```

Crée un `salledispo.db` vide avec le schéma complet, sans aucune donnée réelle.

### 4. Créer un premier compte administrateur

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

L'application est disponible sur `http://localhost:5001`.

## Sécurité

- **Mots de passe** hachés avec `scrypt` (jamais stockés en clair).
- **CSRF** : formulaires protégés via Flask-WTF.
- **Rate limiting** : `/login` limité à 5 tentatives / 5 minutes par IP (Flask-Limiter).
- **En-têtes de sécurité** gérés par Flask-Talisman.
- **URL ICS sensibles** chiffrées en base (Fernet, dérivé de `FLASK_SECRET_KEY`), voir `ics_crypto.py`.
- **Redirections post-login** validées, pour éviter les *open redirects*.
- **Secrets** jamais commités (voir `.gitignore`), fournis uniquement via variables d'environnement.

Ce projet reste un **projet étudiant / démonstration** : il n'a pas fait l'objet d'un audit de sécurité formel et ne doit pas être considéré comme prêt pour un usage en production sans revue.

## Statut & licence

Ce dépôt est publié à titre de **démonstration et de portfolio** : il montre comment l'application est construite, mais n'est pas distribué avec une licence d'utilisation libre. Sauf mention contraire, tous droits sont réservés — n'hésitez pas à me contacter si vous souhaitez réutiliser tout ou partie du code.

## Auteur

Développé par [Votre nom] — [lien vers votre profil / portfolio / LinkedIn]
