"""Authentification Blink : session enregistrée, identifiants, 2FA.

Extrait de blink2video.py à l'étape 8 du plan de remédiation
(AUDIT-2026-08-13.md, section 20, 8.1) : ce fichier ne connaît que la
session, jamais les clips ni le registre. Il suppose que runtime.bootstrap()
a déjà tourné (l'appelant décide quand : voir blink_cli.py et O-06/8.7)."""

from __future__ import annotations  # Python 3.8 (build Windows 7) : les annotations "X | None" ne s'évaluent qu'à l'écriture des chaînes, jamais à l'exécution.

import asyncio
from contextlib import asynccontextmanager
import getpass
import json
import os
import ssl
import stat as stat_module
import time
import uuid

import runtime

import certifi
from aiohttp import ClientSession, TCPConnector

from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink

# I-01 : merge_daily.py, serve.py et watch.py dérivent déjà leurs chemins par
# défaut de runtime.app_dir() (BASE_DIR) ; blink2video.py était le seul des
# quatre à utiliser des chemins relatifs, résolus au hasard du répertoire
# courant du lancement plutôt que de la racine de données.
BASE_DIR = runtime.app_dir()
CONFIG = BASE_DIR / "blink_auth.json"

# I-02/4.5 : liste blanche des champs persistés. blink.auth.login_attributes
# renvoie self.data au complet, qui contient encore username/password tels
# que passés à Auth() ; s'y fier sans filtrer laisserait fuir le mot de passe
# dès qu'un champ de plus y apparaîtrait un jour côté blinkpy.
AUTH_FIELDS = (
    "username", "token", "expires_in", "expiration_date", "refresh_token",
    "host", "region_id", "client_id", "account_id", "user_id", "hardware_id",
)


def contexte_tls() -> ssl.SSLContext:
    """Étend les racines système avec le magasin Mozilla livré par certifi.

    Windows 7 ne reçoit plus forcément les nouvelles autorités racines. On
    conserve son magasin (notamment utile derrière un proxy d'entreprise) et
    on lui ajoute celui de certifi, sans jamais désactiver la validation TLS ni
    le contrôle du nom d'hôte.
    """
    contexte = ssl.create_default_context()
    contexte.load_verify_locations(cafile=certifi.where())
    return contexte


def session_http() -> ClientSession:
    """Crée une session aiohttp dont toutes les requêtes utilisent ce magasin."""
    return ClientSession(connector=TCPConnector(ssl=contexte_tls()))


@asynccontextmanager
async def session_http_temporaire():
    """Ferme aussi le transport SSL avant que la boucle courte disparaisse."""
    session = session_http()
    try:
        yield session
    finally:
        await session.close()
        # aiohttp recommande 250 ms pour laisser asyncio fermer ses transports
        # SSL ; nécessaire notamment avec la ProactorEventLoop de Python 3.8.
        await asyncio.sleep(0.250)


def load_saved_session() -> dict | None:
    """Charge une session Blink sans jamais réutiliser un mot de passe stocké."""
    if not CONFIG.exists():
        return None

    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except OSError:
        print("Session illisible [fichier]. Une nouvelle connexion est nécessaire.")
        return None
    except json.JSONDecodeError:
        print("Session illisible [données JSON]. Une nouvelle connexion est nécessaire.")
        return None

    if not isinstance(data, dict):
        print("Session illisible [schéma JSON]. Une nouvelle connexion est nécessaire.")
        return None

    if not isinstance(data.get("refresh_token"), str) or not data["refresh_token"]:
        return None

    # Auth.startup() exige ces clés, même lorsqu'un refresh_token est présent.
    # Une chaîne vide évite de conserver le mot de passe sur disque.
    data["username"] = data.get("username", "")
    data["password"] = ""
    return data


def ask_credentials() -> dict:
    """Demande les identifiants dans le terminal."""
    print("\nConnexion au compte Blink")
    username = input("Adresse e-mail : ").strip()
    password = getpass.getpass("Mot de passe (non affiché) : ")
    return {"username": username, "password": password}


def make_blink(session: ClientSession, login_data: dict) -> Blink:
    """Construit un client Blink avec un flux d'authentification explicite.

    I-03 : Auth rafraîchit son jeton en mémoire au fil des requêtes (voir
    Auth.query dans blinkpy), sans jamais l'écrire sur disque de lui-même. Une
    boucle de fond peut tourner des heures : sans persistance à chaque
    rafraîchissement, un crash ou un simple redémarrage retombe sur un jeton
    périmé. `callback` est le point d'extension prévu par blinkpy pour cela."""
    blink = Blink(session=session)
    blink.auth = Auth(login_data, no_prompt=True, session=session,
                      callback=lambda: save_session(blink))
    return blink


async def prompt_2fa_code(attempt: int) -> str:
    """Demande le code de vérification dans le terminal."""
    return input("Code Blink : ").strip()


async def finish_login(blink: Blink, ask_code=None) -> bool:
    """Démarre Blink et termine éventuellement la vérification en deux étapes.

    `ask_code` permet de poser la question ailleurs que dans le terminal :
    serve.py y branche un formulaire de navigateur. C'est une coroutine pour
    que l'attente du code n'immobilise pas la boucle d'événements pendant que
    la session Blink reste ouverte."""
    try:
        connected = await blink.start()
    except BlinkTwoFARequiredError:
        ask_code = ask_code or prompt_2fa_code
        print("\nUn code de vérification Blink vient d'être envoyé.")
        for attempt in range(3):
            code = (await ask_code(attempt) or "").strip()
            if code and await blink.send_2fa_code(code):
                return True
            remaining = 2 - attempt
            if remaining:
                print(f"Code refusé. {remaining} tentative(s) restante(s).")
        return False

    return bool(connected)


async def connect_saved(session: ClientSession):
    """Ouvre une session Blink à partir du fichier enregistré, sans rien demander.

    Distinct de `connect`, qui se rabat sur le terminal quand la session n'est
    plus valable : derrière un serveur il n'y a personne pour répondre, mieux
    vaut renvoyer None et laisser l'appelant proposer une reconnexion."""
    saved = load_saved_session()
    if not saved:
        return None
    blink = make_blink(session, saved)
    if not await finish_login(blink, ask_code=_no_code):
        return None
    save_session(blink)
    return blink


async def _no_code(attempt: int) -> str:
    """Refuse la vérification en deux étapes hors session interactive."""
    return ""


async def login(session: ClientSession, username: str, password: str, ask_code=None):
    """Ouvre une session Blink à partir d'identifiants déjà recueillis.

    Voie d'entrée de serve.py, qui les collecte dans le navigateur. Le mot de
    passe ne sert qu'ici : `save_session` ne l'écrit jamais sur disque."""
    blink = make_blink(session, {"username": username, "password": password})
    if not await finish_login(blink, ask_code):
        return None
    save_session(blink)
    return blink


def save_session(blink: Blink) -> None:
    """Sauvegarde les jetons de session, jamais le mot de passe Blink.

    Deux à quatre processus peuvent se connecter en parallèle au démarrage
    (download USB, download cloud, watch, serve), et ce callback est aussi
    celui que l'auto-rafraîchissement de blinkpy déclenche en cours de route
    (voir make_blink, I-03) : deux écritures peuvent donc se chevaucher sans
    ordre garanti. Trois précautions : un temporaire propre à ce processus
    (I-02, l'ancien `blink_auth.tmp` était partagé par tous), un horodatage
    interne qui empêche une sauvegarde en retard d'écraser une plus récente
    déjà sur disque, et - depuis la revue de code du 0eab463, bug #7 - un
    verrou (`runtime.verrou`, déjà utilisé ailleurs pour le Sync Module et
    l'assemblage) qui rend la lecture-puis-décision-puis-écriture atomique :
    sans lui, deux processus pouvaient chacun lire l'ancien contenu, chacun
    conclure « le mien est plus récent », puis écrire dans un ordre non
    garanti - celui qui finit en second écrase le jeton le plus récent,
    quel que soit l'horodatage qu'il portait."""
    complet = blink.auth.login_attributes
    data = {champ: complet[champ] for champ in AUTH_FIELDS if champ in complet}
    data["updated_at"] = time.time()

    try:
        with runtime.verrou("session-save", f"blink_auth-{os.getpid()}", attente=5):
            precedent = None
            if CONFIG.exists():
                try:
                    precedent = json.loads(CONFIG.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    precedent = None
            if (isinstance(precedent, dict)
                    and float(precedent.get("updated_at") or 0) > data["updated_at"]):
                return

            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG.with_name(
                f"{CONFIG.stem}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            )
            try:
                temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
                if os.name != "nt":
                    os.chmod(temporary, stat_module.S_IRUSR | stat_module.S_IWUSR)
                temporary.replace(CONFIG)
            finally:
                temporary.unlink(missing_ok=True)
    except runtime.BusyError:
        # Un autre processus tient le verrou depuis plus de 5 s pour une
        # simple écriture de jeton : plus probable qu'il vient de finir la
        # sienne (déjà à jour) qu'un vrai blocage - abandonner ici plutôt que
        # de faire attendre l'appelant plus longtemps pour une sauvegarde
        # redondante.
        pass


async def connect(session: ClientSession) -> Blink | None:
    """Ouvre une session Blink, avec reconnexion interactive en dernier recours."""
    saved_session = load_saved_session()

    if saved_session:
        print(f"Réutilisation de la session enregistrée dans {CONFIG}...")
        blink = make_blink(session, saved_session)
        connected = await finish_login(blink)
        if not connected:
            print("La session enregistrée n'est plus valide.")
            blink = make_blink(session, ask_credentials())
            connected = await finish_login(blink)
    else:
        blink = make_blink(session, ask_credentials())
        connected = await finish_login(blink)

    if not connected:
        return None

    save_session(blink)
    return blink


async def preflight() -> dict:
    """Contrôle read-only de la session enregistrée (E-01, section 17.3).

    Strictement sans effet de bord : aucun armement, direct, manifeste USB,
    téléchargement, assemblage ni notification. Sert uniquement à décider si
    « start » peut continuer directement ou doit d'abord passer par
    l'onboarding web."""
    etat = {"authenticated": False, "networks": 0, "sync_modules": 0,
            "cameras": 0, "cloud_only": False, "error": None}
    try:
        async with session_http_temporaire() as session:
            blink = await connect_saved(session)
            if blink is None:
                return etat
            etat["authenticated"] = True
            synchronisations = getattr(blink, "sync", None) or {}
            etat["sync_modules"] = len(synchronisations)
            etat["cloud_only"] = not synchronisations
            reseaux = set()
            for sync in synchronisations.values():
                etat["cameras"] += len(getattr(sync, "cameras", None) or {})
                reseau = getattr(sync, "network_id", None)
                if reseau is not None:
                    reseaux.add(reseau)
            etat["networks"] = len(reseaux)
    except Exception as erreur:  # une panne réseau ne doit pas abattre « start »
        etat["error"] = f"{type(erreur).__name__}: {erreur}"
    return etat
