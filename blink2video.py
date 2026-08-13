import argparse
import asyncio
import bisect
import contextlib
import datetime as dt
import getpass
import hashlib
import json
import math
import os
import re
import stat as stat_module
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

from aiohttp import ClientError, ClientSession

from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink


CONFIG = Path("blink_auth.json")
OUTPUT = Path("Blink_Clips")
STATE_FILENAME = ".blink_download_state.json"
STATE_V1_BACKUP_FILENAME = ".blink_download_state.v1.backup.json"


HUB_LOCK = Path(".blink_hub.lock")


class CloudResult(NamedTuple):
    """Bilan stable d'un passage cloud, sans confondre ses quatre issues."""

    downloaded: int = 0
    adopted: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def had_error(self) -> bool:
        return self.failed > 0

    @property
    def new_downloads(self) -> int:
        return self.downloaded


def hub_lock(owner: str, stale_after: int = 600):
    """Réserve le Sync Module. Conservé ici pour les appelants existants."""
    return runtime.verrou("hub", owner, stale_after)


BusyError = runtime.BusyError


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    result = runtime.lancer(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, errors="replace", check=False,
    )
    return str(pid) in (result.stdout or "")


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
    """Construit un client Blink avec un flux d'authentification explicite."""
    blink = Blink(session=session)
    blink.auth = Auth(login_data, no_prompt=True, session=session)
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
    """Sauvegarde les jetons de session, mais jamais le mot de passe Blink."""
    data = dict(blink.auth.login_attributes)
    data["password"] = ""

    temporary = CONFIG.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(CONFIG)


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


def select_sync_modules(blink: Blink, requested_name: str | None):
    """Sélectionne tous les hubs, ou celui demandé par son nom."""
    modules = list(blink.sync.items())
    if not requested_name:
        return modules

    selected = [
        (name, sync)
        for name, sync in modules
        if name.casefold().strip() == requested_name.casefold().strip()
    ]
    if not selected:
        available = ", ".join(name for name, _ in modules) or "aucun"
        raise ValueError(
            f"Sync Module introuvable : {requested_name!r}. Disponible(s) : {available}."
        )
    return selected


async def read_local_manifest(sync) -> list:
    """Demande au Sync Module la liste à jour de ses clips USB."""
    storage = sync._local_storage  # blinkpy n'expose pas encore d'accesseur public.
    if not storage.get("compatible"):
        raise RuntimeError("ce hub n'est pas compatible avec le stockage local")
    if not storage.get("enabled"):
        raise RuntimeError("le stockage local n'est pas activé sur ce hub")
    if not sync.local_storage:
        raise RuntimeError(
            "le stockage local n'est pas actif (clé USB absente/non reconnue, "
            "ou clips enregistrés dans le cloud)"
        )

    print("  Lecture du manifeste USB du hub...")
    # Le Sync Module ne traite qu'une commande à la fois et répond « System is
    # busy » (code 307) tant qu'il n'a pas fini la précédente : un direct qui
    # vient de se fermer, ou une autre demande de manifeste, suffisent. Ce
    # refus est temporaire, pas une panne, d'où la reprise avec une attente qui
    # s'allonge plutôt qu'un abandon immédiat.
    delays = (3, 8, 15, 25)
    for attempt, delay in enumerate((*delays, None), start=1):
        if await sync.update_local_storage_manifest():
            return list(storage["manifest"])
        if delay is None:
            break
        print(f"  Module occupé, nouvelle tentative dans {delay} s "
              f"({attempt}/{len(delays)})...")
        await asyncio.sleep(delay)

    raise RuntimeError(
        "Blink n'a pas renvoyé le manifeste du stockage local après "
        f"{len(delays) + 1} tentatives (module resté occupé)"
    )


class CloudClip:
    """Un clip du cloud, présenté comme ceux de la clé USB.

    Même surface que les objets de blinkpy : un nom de caméra, un instant, un
    identifiant. Le reste de la chaîne, identité, nom de fichier, registre,
    normalisation, ignore donc d'où vient l'enregistrement, et c'est le but :
    une seule archive, quelle que soit la source."""

    def __init__(self, entree: dict):
        self.raw = entree
        self.name = str(entree.get("device_name") or "").strip() or "camera"
        self.id = entree.get("id")
        self.created_at = dt.datetime.fromisoformat(str(entree["created_at"]))
        self.address = entree.get("media")
        self.network_id = entree.get("network_id")
        self.device_id = (
            entree.get("device_id") or entree.get("camera_id")
        )
        self.download_issue = None

    async def download_to(self, blink: Blink, target: Path) -> bool:
        """Écrit le média dans `target`, en passant par la session Blink."""
        self.download_issue = None
        target.unlink(missing_ok=True)
        if not self.address:
            self.download_issue = ("données", "adresse du média absente")
            return False
        try:
            reponse = await blink.do_http_get(self.address)
            if reponse is None:
                self.download_issue = ("réseau", "aucune réponse du service Blink")
                return False
            statut = getattr(reponse, "status", None)
            if not isinstance(statut, int) or not 200 <= statut < 300:
                detail = f"statut HTTP {statut}" if isinstance(statut, int) else "statut HTTP absent"
                self.download_issue = ("HTTP", f"réponse refusée ({detail})")
                with contextlib.suppress(Exception):
                    await reponse.read()
                return False
            contenu = await reponse.read()
            if not contenu:
                self.download_issue = ("HTTP", "réponse vide")
                return False
            target.write_bytes(contenu)
            return True
        finally:
            if self.download_issue is not None:
                target.unlink(missing_ok=True)


async def read_cloud_manifest(blink: Blink, since_days: int | None) -> list:
    """Inventaire des clips conservés dans le cloud de l'abonnement Blink.

    Distinct du manifeste USB, et indépendant du Sync Module : c'est le compte
    qui répond, pas le module. Un abonnement ne couvrant qu'une partie des
    caméras, les deux inventaires ne se recouvrent que partiellement, d'où le
    rapprochement fait plus loin plutôt qu'un choix de source."""
    jours = 30 if since_days is None else since_days
    if jours < 0:
        raise ValueError("le nombre de jours doit être positif ou nul")
    depuis = dt.datetime.now() - dt.timedelta(days=jours)
    entrees = await blink.get_videos_metadata(
        since=depuis.strftime("%Y/%m/%d %H:%M:%S"), stop=20
    )
    if entrees is None:
        return []
    if not isinstance(entrees, (list, tuple)):
        print("  ! [données] Manifeste cloud ignoré : racine JSON inattendue.")
        return []
    clips = []
    invalides = 0
    for entree in entrees:
        if not isinstance(entree, dict):
            invalides += 1
            continue
        # Un média supprimé reste listé, marqué comme tel ; un média partiel est
        # un enregistrement encore en cours d'écriture côté Blink.
        if entree.get("deleted") or entree.get("partial"):
            continue
        if str(entree.get("type") or "video") != "video":
            continue
        try:
            clips.append(CloudClip(entree))
        except (KeyError, TypeError, ValueError):
            invalides += 1
    if invalides:
        print(f"  ! [données] {invalides} entrée(s) cloud invalide(s) ignorée(s).")
    return clips


def _identifiant_reseau(clip, sync=None) -> str:
    """Retourne le réseau immuable quand l'API l'expose."""
    valeur = getattr(clip, "network_id", None)
    if valeur in (None, "") and sync is not None:
        valeur = getattr(sync, "network_id", None)
    return str(valeur or "")


def _identifiant_camera(clip) -> str:
    """Retourne l'identifiant matériel, absent des objets USB blinkpy 0.25."""
    for attribut in ("device_id", "camera_id"):
        valeur = getattr(clip, attribut, None)
        if valeur not in (None, ""):
            return str(valeur)
    brut = getattr(clip, "raw", None)
    if isinstance(brut, dict):
        for cle in ("device_id", "camera_id"):
            if brut.get(cle) not in (None, ""):
                return str(brut[cle])
    return ""


def _meme_camera(gauche, droite, sync_gauche=None, sync_droite=None) -> bool:
    """Compare deux caméras sans confondre deux réseaux explicites."""
    reseau_gauche = _identifiant_reseau(gauche, sync_gauche)
    reseau_droite = _identifiant_reseau(droite, sync_droite)
    if reseau_gauche and reseau_droite and reseau_gauche != reseau_droite:
        return False

    camera_gauche = _identifiant_camera(gauche)
    camera_droite = _identifiant_camera(droite)
    if camera_gauche and camera_droite:
        return camera_gauche == camera_droite

    # blinkpy 0.25 n'expose ni device_id ni network_id sur certains objets USB.
    # Dans ce seul cas, le nom API original (jamais le nom de chemin assaini)
    # reste le meilleur signal disponible pour rapprocher USB et cloud.
    return str(gauche.name).casefold() == str(droite.name).casefold()


def _apparier_evenements(locaux: list, distants: list, tolerance: int = 2, *,
                         priorites_locaux=None, compatibles=None) -> list:
    """Retourne un appariement maximal ``(local, distant)`` déterministe."""
    if tolerance < 0 or not locaux or not distants:
        return []

    # L'index temporel réduit le graphe aux seuls voisins dans la fenêtre. Les
    # listes d'adjacence sont ensuite ordonnées par écart puis par index afin
    # que deux exécutions sur les mêmes manifestes rendent les mêmes paires.
    marge = dt.timedelta(seconds=tolerance)
    locaux_ordonnes = sorted(
        (clip_datetime_utc(local), indice)
        for indice, local in enumerate(locaux)
    )
    instants_locaux = [instant for instant, _ in locaux_ordonnes]
    priorites = priorites_locaux or [0] * len(locaux)
    meme_camera = compatibles or _meme_camera
    voisinages = [[] for _ in distants]
    for indice_distant, distant in enumerate(distants):
        instant_cloud = clip_datetime_utc(distant)
        debut = bisect.bisect_left(instants_locaux, instant_cloud - marge)
        fin = bisect.bisect_right(instants_locaux, instant_cloud + marge)
        for position in range(debut, fin):
            instant_local, indice_local = locaux_ordonnes[position]
            local = locaux[indice_local]
            if not meme_camera(local, distant):
                continue
            ecart = abs((instant_local - instant_cloud).total_seconds())
            voisinages[indice_distant].append(
                (priorites[indice_local], ecart, indice_local)
            )
        voisinages[indice_distant].sort()

    infini = len(locaux) + len(distants) + 1
    ordre_distants = sorted(
        range(len(distants)),
        key=lambda indice: (
            voisinages[indice][0][:2] if voisinages[indice]
            else (math.inf, math.inf),
            indice,
        ),
    )
    paire_distante = [-1] * len(distants)
    paire_locale = [-1] * len(locaux)

    def niveaux():
        distances = [infini] * len(distants)
        file = []
        for indice_distant in ordre_distants:
            if paire_distante[indice_distant] < 0:
                distances[indice_distant] = 0
                file.append(indice_distant)

        profondeur_libre = infini
        position = 0
        while position < len(file):
            indice_distant = file[position]
            position += 1
            profondeur_suivante = distances[indice_distant] + 1
            if profondeur_suivante > profondeur_libre:
                continue
            for _, _, indice_local in voisinages[indice_distant]:
                autre_distant = paire_locale[indice_local]
                if autre_distant < 0:
                    profondeur_libre = min(profondeur_libre, profondeur_suivante)
                elif (
                    profondeur_suivante < profondeur_libre
                    and distances[autre_distant] == infini
                ):
                    distances[autre_distant] = profondeur_suivante
                    file.append(autre_distant)
        return distances, profondeur_libre

    def augmenter(racine, distances, profondeur_libre, positions):
        # Version itérative du parcours de Hopcroft-Karp : aucun risque de
        # dépasser la profondeur de récursion sur un grand registre.
        pile = [racine]
        parents = {}
        while pile:
            indice_distant = pile[-1]
            avance = False
            while positions[indice_distant] < len(voisinages[indice_distant]):
                _, _, indice_local = voisinages[indice_distant][
                    positions[indice_distant]
                ]
                positions[indice_distant] += 1
                autre_distant = paire_locale[indice_local]
                if autre_distant < 0:
                    if distances[indice_distant] + 1 != profondeur_libre:
                        continue
                    courant, local_libre = indice_distant, indice_local
                    while True:
                        paire_distante[courant] = local_libre
                        paire_locale[local_libre] = courant
                        if courant == racine:
                            return True
                        courant, local_libre = parents[courant]
                elif (
                    distances[autre_distant] == distances[indice_distant] + 1
                    and autre_distant not in parents
                ):
                    parents[autre_distant] = (indice_distant, indice_local)
                    pile.append(autre_distant)
                    avance = True
                    break
            if not avance:
                distances[indice_distant] = infini
                pile.pop()
        return False

    while True:
        distances, profondeur_libre = niveaux()
        if profondeur_libre == infini:
            break
        positions = [0] * len(distants)
        for indice_distant in ordre_distants:
            if paire_distante[indice_distant] < 0:
                augmenter(indice_distant, distances, profondeur_libre, positions)

    return sorted(
        (indice_local, indice_distant)
        for indice_distant, indice_local in enumerate(paire_distante)
        if indice_local >= 0
    )


def rapprocher(locaux: list, cloud: list, tolerance: int = 2) -> tuple:
    """Sépare les clips cloud inédits de ceux déjà offerts par la clé USB.

    Une même détection peut être écrite des deux côtés lorsque l'abonnement
    couvre une caméra dont le stockage local fonctionne aussi. L'identité reste
    la caméra et l'instant, mais les deux sources horodatent l'événement
    séparément : quelques secondes d'écart sont possibles, d'où la tolérance.
    Sans elle, le même enregistrement serait rapatrié deux fois et apparaîtrait
    en double dans la journalière.

    Fonction pure, éprouvée par la suite dédiée sans compte Blink."""
    paires = _apparier_evenements(locaux, cloud, tolerance)
    cloud_pris = {indice_cloud for _, indice_cloud in paires}

    doublons = [clip for indice, clip in enumerate(cloud) if indice in cloud_pris]
    inedits = [clip for indice, clip in enumerate(cloud) if indice not in cloud_pris]
    return inedits, doublons


def clip_datetime_utc(clip) -> dt.datetime:
    """Normalise l'horodatage Blink en UTC."""
    created = clip.created_at
    if created.tzinfo is None:
        return created.replace(tzinfo=dt.timezone.utc)
    return created.astimezone(dt.timezone.utc)


def filter_clips(clips: list, camera: str | None, since_days: int | None) -> list:
    """Applique les filtres demandés, puis trie du plus ancien au plus récent."""
    selected = clips
    if camera:
        selected = [clip for clip in selected if clip.name.casefold() == camera.casefold()]
    if since_days is not None:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=since_days)
        selected = [clip for clip in selected if clip_datetime_utc(clip) >= cutoff]
    return sorted(selected, key=clip_datetime_utc)


def reported_bytes(value) -> int:
    """Convertit en octets la taille du manifeste Blink, exprimée en Kio."""
    try:
        return int(value) * 1024
    except (TypeError, ValueError):
        return 0


def human_size(size: int) -> str:
    """Affiche une taille lisible."""
    amount = float(size)
    for unit in ("o", "Kio", "Mio", "Gio"):
        if amount < 1024 or unit == "Gio":
            return f"{amount:.0f} {unit}" if unit == "o" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} o"


def _tronquer_utf8(value: str, maximum: int) -> str:
    brut = value.encode("utf-8")[:maximum]
    return brut.decode("utf-8", errors="ignore")


def safe_name(value: str) -> str:
    """Produit un composant de chemin sûr à partir du nom d'une caméra."""
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    cleaned = _tronquer_utf8(cleaned, 32).rstrip(". ") or "camera"
    racine = cleaned.split(".", 1)[0].casefold()
    reserves = {"con", "prn", "aux", "nul", *{f"com{i}" for i in range(1, 10)},
                *{f"lpt{i}" for i in range(1, 10)}}
    if racine in reserves:
        cleaned = f"_{cleaned}"
    return cleaned


def target_path(output: Path, clip, sync=None, source: str | None = None) -> Path:
    """Construit un chemin sûr ; ce chemin n'est jamais l'identité métier.

    Le suffixe porte une courte empreinte des valeurs API avant assainissement.
    Ainsi deux noms/identifiants qui deviennent le même composant de chemin, ou
    deux hubs qui réemploient le même numéro de manifeste, restent distincts.
    ``sync_id`` ne concerne que l'USB : dans le cloud, ``network_id`` désigne la
    source et le pseudo-module utilisé par l'appelant ne doit pas modifier le
    chemin.
    """
    created = clip_datetime_utc(clip)
    camera = safe_name(clip.name)
    month = created.strftime("%Y-%m")
    identifiant = safe_name(str(clip.id))[:40]
    provenance = source or (
        "cloud" if isinstance(clip, CloudClip) or hasattr(clip, "download_to")
        else "usb"
    )
    empreinte = json.dumps(
        [
            provenance,
            _identifiant_reseau(clip, sync),
            str(getattr(sync, "sync_id", "")) if provenance == "usb" else "",
            str(clip.name),
            str(getattr(clip, "id", "")),
            created.isoformat(),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    suffixe = hashlib.sha256(empreinte.encode("utf-8")).hexdigest()[:12]
    filename = (
        f"{created:%Y-%m-%d_%H-%M-%SZ}_{camera}_{identifiant}_{suffixe}.mp4"
    )
    return output / camera / month / filename


async def download_clip(blink: Blink, clip, target: Path, overwrite: bool) -> str:
    """Prépare puis télécharge un clip, sans jamais le supprimer du hub."""
    if target.exists() and target.stat().st_size > 0 and not overwrite:
        return "skipped"

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)

    try:
        prepared = await clip.prepare_download(blink)
        if not prepared or not await clip.download_video(blink, str(partial)):
            return "failed"
        if not partial.exists() or partial.stat().st_size == 0:
            return "failed"
        partial.replace(target)
        return "downloaded"
    finally:
        partial.unlink(missing_ok=True)


def load_download_state(output: Path) -> dict:
    """Charge le registre incrémental placé dans le dossier de destination."""
    state_file = output / STATE_FILENAME
    if not state_file.exists():
        return {"version": 1, "clips": {}}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("la racine JSON doit être un objet")
        if state.get("version") not in (1, 2) or not isinstance(state.get("clips"), dict):
            raise ValueError("format inconnu")
        valides = {}
        ignores = 0
        for cle, entree in state["clips"].items():
            if not isinstance(cle, str) or not isinstance(entree, dict):
                ignores += 1
                continue
            instant = entree.get("created_at")
            if not isinstance(instant, str):
                ignores += 1
                continue
            try:
                dt.datetime.fromisoformat(instant)
            except ValueError:
                ignores += 1
                continue
            # Migration v1 additive : les anciennes clés et archives restent
            # intactes. Les métadonnées nouvelles permettent les recherches
            # sans dépendre du nom de fichier ni de l'ID USB renuméroté.
            entree.setdefault("source", "usb")
            entree.setdefault("network_id", "")
            entree.setdefault("device_id", "")
            entree.setdefault("remote_id", "")
            entree.setdefault("sync_id", str(cle).split(":", 1)[0])
            camera_identite = (
                f"device:{entree['device_id']}" if entree["device_id"]
                else f"name:{str(entree.get('camera') or 'camera').casefold()}"
            )
            entree.setdefault("camera_identity", camera_identite)
            empreinte = json.dumps(
                [entree["network_id"], camera_identite, instant],
                ensure_ascii=False, separators=(",", ":"),
            )
            entree.setdefault(
                "correlation_id",
                hashlib.sha256(empreinte.encode("utf-8")).hexdigest(),
            )
            valides[cle] = entree
        if ignores:
            print(f"  ! [données] {ignores} entrée(s) de registre invalide(s) ignorée(s).")
        state["clips"] = valides
        state["version"] = 2
        return state
    except OSError:
        categorie = "fichier"
    except json.JSONDecodeError:
        categorie = "données JSON"
    except ValueError:
        categorie = "schéma JSON"
    print(
        f"  ! État incrémental illisible [{categorie}] ; "
        "les fichiers existants seront vérifiés."
    )
    return {"version": 1, "clips": {}}


def save_download_state(output: Path, state: dict) -> None:
    """Enregistre le registre incrémental, sans écraser le travail d'un autre.

    Deux boucles écrivent désormais ici, l'une pour la clé USB toutes les dix
    minutes, l'autre pour le cloud toutes les minutes. Écrire sa propre copie
    en bloc ferait perdre les clips que l'autre vient d'ajouter : on relit donc
    le fichier juste avant d'écrire, et on superpose ses propres entrées. Une
    entrée n'est jamais retirée par ce chemin, seulement ajoutée ou mise à
    jour, ce qui rend la superposition sûre.

    Le remplacement reste atomique : un plantage en cours d'écriture laisse
    l'ancien registre intact plutôt qu'un fichier tronqué."""
    output.mkdir(parents=True, exist_ok=True)
    state_file = output / STATE_FILENAME
    with runtime.verrou("registre", "ecriture", stale_after=60, attente=10):
        _sauvegarder_registre_v1(state_file)
        _ecrire_registre(state_file, state)


def _sauvegarder_registre_v1(state_file: Path) -> None:
    """Conserve une copie unique du registre v1 avant sa première migration."""
    backup = state_file.with_name(STATE_V1_BACKUP_FILENAME)
    if not state_file.exists():
        return
    try:
        brut = state_file.read_bytes()
        ancien = json.loads(brut)
        if not isinstance(ancien, dict) or ancien.get("version") != 1:
            return
        if backup.exists():
            sauvegarde = backup.read_bytes()
            chargee = json.loads(sauvegarde)
            if not isinstance(chargee, dict) or chargee.get("version") != 1:
                raise OSError("sauvegarde v1 invalide")
            return
        temporaire = backup.with_suffix(backup.suffix + ".tmp")
        temporaire.write_bytes(brut)
        temporaire.replace(backup)
        if backup.read_bytes() != brut:
            raise OSError("sauvegarde v1 non vérifiable")
    except (OSError, json.JSONDecodeError) as erreur:
        raise OSError(
            "migration v2 annulée : sauvegarde du registre v1 impossible"
        ) from erreur


def _ecrire_registre(state_file: Path, state: dict) -> None:
    """Superpose ses propres entrées à celles déjà sur disque, atomiquement."""
    output = state_file.parent
    fusionne = dict(state)
    disque = load_download_state(output)
    clips = dict(disque.get("clips") or {})
    for cle, entree in (state.get("clips") or {}).items():
        precedente = clips.get(cle)
        # Une exclusion posée par l'interface est une décision utilisateur :
        # une copie périmée du downloader ne doit jamais l'annuler.
        if (
            isinstance(precedente, dict)
            and precedente.get("excluded")
            and isinstance(entree, dict)
            and not entree.get("excluded")
        ):
            continue
        clips[cle] = entree
    fusionne["clips"] = clips
    fusionne["version"] = max(
        2, int(disque.get("version") or 1), int(state.get("version") or 1),
    )
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(fusionne, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    temporary.replace(state_file)
    _invalider_index_registre(state)
    state["version"] = fusionne["version"]
    state["clips"] = fusionne.get("clips", state.get("clips"))


def state_key(sync, clip, source: str = "usb") -> str:
    """Identifie la provenance sans la confondre avec la corrélation.

    Une identité USB contient module, réseau, caméra et instant, mais jamais
    l'ID du manifeste : le Sync Module le renumérote lors d'une réindexation.
    Une identité cloud ajoute au contraire l'ID distant du média, immuable
    lorsqu'il est fourni par l'API. Le rapprochement USB/cloud reste une
    opération séparée, tolérante sur l'instant.

    Faute de ``device_id`` dans les objets USB de blinkpy 0.25, leur nom API
    original est le repli documenté ; un renommage de caméra USB ne peut donc
    pas être reconnu avec certitude."""
    created = clip_datetime_utc(clip).isoformat()
    camera = _identifiant_camera(clip) or f"name:{str(clip.name).casefold()}"
    identite = json.dumps(
        [
            source,
            str(getattr(sync, "sync_id", "")),
            _identifiant_reseau(clip, sync),
            camera,
            created,
            str(getattr(clip, "id", "")) if source == "cloud" else "",
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "v2:" + hashlib.sha256(identite.encode("utf-8")).hexdigest()


def remember_download(state: dict, sync, hub_name: str, clip, output: Path,
                      target: Path, source: str = "usb") -> None:
    """Marque un clip comme acquis uniquement lorsque son fichier existe.

    La provenance est notée : une caméra couverte par un abonnement enregistre
    dans le cloud, une autre sur la clé du module, et une Blink Mini n'écrit
    jamais sur la clé. L'interface le montre par caméra, ce qui évite de
    chercher pourquoi telle caméra ne produit rien."""
    _invalider_index_registre(state)
    state["version"] = 2
    state.setdefault("clips", {})[state_key(sync, clip, source)] = {
        "hub": hub_name,
        "camera": clip.name,
        "created_at": clip_datetime_utc(clip).isoformat(),
        "path": target.relative_to(output).as_posix(),
        "bytes": target.stat().st_size,
        "source": source,
        "network_id": _identifiant_reseau(clip, sync),
        "device_id": _identifiant_camera(clip),
        "remote_id": str(getattr(clip, "id", "")),
        "sync_id": str(getattr(sync, "sync_id", "")),
        "camera_identity": (
            f"device:{_identifiant_camera(clip)}" if _identifiant_camera(clip)
            else f"name:{str(clip.name).casefold()}"
        ),
    }
    entree = state["clips"][state_key(sync, clip, source)]
    empreinte = json.dumps(
        [entree["network_id"], entree["camera_identity"], entree["created_at"]],
        ensure_ascii=False, separators=(",", ":"),
    )
    entree["correlation_id"] = hashlib.sha256(empreinte.encode("utf-8")).hexdigest()


def _chemin_entree(output: Path, entry: dict, *, index=None,
                   cle: str | None = None) -> Path | None:
    """Résout un média du registre uniquement à l'intérieur de ``output``."""
    brut = entry.get("path")
    if not isinstance(brut, str) or not brut.strip():
        return None
    cache_cle = (cle, os.fspath(output))
    if index is not None and cle is not None:
        memorise = index.chemins.get(cache_cle)
        if memorise is not None and memorise[0] == brut:
            return memorise[1]
    relatif = Path(brut)
    if relatif.is_absolute() or ".." in relatif.parts:
        return None
    if index is None:
        racine = output.resolve()
    else:
        racine = index.racines.get(os.fspath(output))
        if racine is None:
            racine = output.resolve()
            index.racines[os.fspath(output)] = racine
    chemin = (racine / relatif).resolve()
    try:
        chemin.relative_to(racine)
    except ValueError:
        return None
    if index is not None and cle is not None:
        index.chemins[cache_cle] = (brut, chemin)
    return chemin


class _IndexRegistre:
    """Vue temporelle éphémère d'un registre, jamais incluse dans son JSON."""

    def __init__(self, state: dict):
        self.state = state
        self.clips = state.get("clips")
        self.taille = len(self.clips) if isinstance(self.clips, dict) else 0
        self.valides = []
        self.racines = {}
        self.chemins = {}
        self.fichiers = {}
        temporels = []
        tombstones = []
        for position, (cle, entry) in enumerate((self.clips or {}).items()):
            if not isinstance(cle, str) or not isinstance(entry, dict):
                continue
            if not entry.get("created_at"):
                continue
            try:
                connu = _ClipConnu(entry)
                instant = clip_datetime_utc(connu)
            except (TypeError, ValueError):
                continue
            self.valides.append((cle, entry, connu, position))
            element = (instant, position, cle, entry, connu)
            temporels.append(element)
            if entry.get("excluded"):
                tombstones.append(element)
        self.temporels = sorted(temporels)
        self.instants = [element[0] for element in self.temporels]
        self.tombstones = sorted(tombstones)
        self.instants_tombstones = [element[0] for element in self.tombstones]


_CACHE_INDEX_REGISTRE = None


def _invalider_index_registre(state: dict) -> None:
    """Oublie la vue éphémère avant toute mutation interne du registre."""
    global _CACHE_INDEX_REGISTRE
    if (
        _CACHE_INDEX_REGISTRE is not None
        and _CACHE_INDEX_REGISTRE.state is state
    ):
        _CACHE_INDEX_REGISTRE = None


def _index_registre(state: dict) -> _IndexRegistre:
    """Réutilise un unique index hors JSON pour les recherches successives."""
    global _CACHE_INDEX_REGISTRE
    clips = state.get("clips")
    taille = len(clips) if isinstance(clips, dict) else 0
    if (
        _CACHE_INDEX_REGISTRE is None
        or _CACHE_INDEX_REGISTRE.state is not state
        or _CACHE_INDEX_REGISTRE.clips is not clips
        or _CACHE_INDEX_REGISTRE.taille != taille
    ):
        _CACHE_INDEX_REGISTRE = _IndexRegistre(state)
    return _CACHE_INDEX_REGISTRE


def _trouver_entree(state: dict, sync, clip,
                    consumed: set[str] | None = None,
                    index: _IndexRegistre | None = None,
                    source: str = "usb",
                    ) -> tuple[str | None, dict | None]:
    """Trouve au plus une entrée corrélée, avec priorité aux tombstones."""
    cle_exacte = state_key(sync, clip, source)
    entry = None if cle_exacte in (consumed or ()) else state["clips"].get(cle_exacte)
    if isinstance(entry, dict) and entry.get("excluded"):
        return cle_exacte, entry

    index = index or _index_registre(state)
    instant = clip_datetime_utc(clip)
    marge = dt.timedelta(seconds=2)
    # Avec une clé exacte ordinaire, seule une ancienne tombstone peut avoir
    # priorité. Ne pas rescanner les médias ordinaires préserve le chemin rapide.
    if isinstance(entry, dict):
        temporels = index.tombstones
        instants = index.instants_tombstones
    else:
        temporels = index.temporels
        instants = index.instants
    debut = bisect.bisect_left(instants, instant - marge)
    fin = bisect.bisect_right(instants, instant + marge)
    meilleur = None
    for indice in range(debut, fin):
        instant_connu, position, cle, candidat, connu = temporels[indice]
        if cle in (consumed or ()):
            continue
        ecart = abs((instant_connu - instant).total_seconds())
        if _meme_camera(connu, clip, sync_droite=sync):
            classement = (
                not bool(candidat.get("excluded")),
                cle != cle_exacte,
                ecart,
                position,
            )
            if meilleur is None or classement < meilleur[0]:
                meilleur = (classement, cle, candidat)
    if meilleur is None:
        return (cle_exacte, entry) if isinstance(entry, dict) else (None, None)
    return meilleur[1], meilleur[2]


def _apparier_registre(state: dict, sync, clips: list,
                       index: _IndexRegistre | None = None) -> dict:
    """Associe en lot chaque clip USB à au plus une entrée du registre."""
    index = index or _index_registre(state)
    correspondances = {}
    cles_prises = set()
    clips_pris = set()

    # Une exclusion est une décision utilisateur absolue, même si une entrée
    # ordinaire possède par ailleurs une clé de source exacte pour le clip.
    tombstones = [
        (cle, entry, connu)
        for cle, entry, connu, _ in index.valides
        if entry.get("excluded")
    ]
    if tombstones:
        paires_exclues = _apparier_evenements(
            [element[2] for element in tombstones],
            clips,
            2,
            compatibles=lambda connu, clip: _meme_camera(
                connu, clip, sync_droite=sync,
            ),
        )
        for indice_entree, indice_clip in paires_exclues:
            cle, entry, _ = tombstones[indice_entree]
            correspondances[indice_clip] = (cle, entry)
            cles_prises.add(cle)
            clips_pris.add(indice_clip)

    # Pour le reste, une identité de source exacte est plus forte qu'une
    # corrélation tolérante.
    for indice_clip, clip in enumerate(clips):
        if indice_clip in clips_pris:
            continue
        cle = state_key(sync, clip)
        entry = state.get("clips", {}).get(cle)
        if (
            isinstance(entry, dict)
            and not entry.get("excluded")
            and cle not in cles_prises
        ):
            correspondances[indice_clip] = (cle, entry)
            cles_prises.add(cle)
            clips_pris.add(indice_clip)

    restants = [indice for indice in range(len(clips)) if indice not in clips_pris]

    entrees = [
        (cle, entry, connu)
        for cle, entry, connu, _ in index.valides
        if cle not in cles_prises and not entry.get("excluded")
    ]
    if not entrees or not restants:
        return correspondances

    connus = [element[2] for element in entrees]
    clips_restants = [clips[indice] for indice in restants]
    paires = _apparier_evenements(
        connus,
        clips_restants,
        2,
        compatibles=lambda connu, clip: _meme_camera(
            connu, clip, sync_droite=sync,
        ),
    )
    for indice_entree, indice_restant in paires:
        cle, entry, _ = entrees[indice_entree]
        correspondances[restants[indice_restant]] = (cle, entry)
    return correspondances


def is_downloaded(state: dict, sync, clip, target: Path,
                  consumed: set[str] | None = None,
                  index: _IndexRegistre | None = None,
                  source: str = "usb") -> bool:
    """Un clip est acquis si le registre et le fichier non vide sont présents.

    Exception : un clip marqué « exclu » compte comme acquis même sans fichier.
    C'est une pierre tombale, posée par `merge_daily.py --exclude`, qui dit
    « écarté volontairement, ne pas rapatrier » ; sans elle, supprimer le
    fichier ne ferait que provoquer un nouveau téléchargement. Même principe
    que le fichier d'archive de yt-dlp (--download-archive, hérité de
    youtube-dl) : on retient l'identifiant, pas la présence du média."""
    index = index or _index_registre(state)
    cle_entree, entry = _trouver_entree(
        state, sync, clip, consumed, index, source,
    )
    if isinstance(entry, dict) and entry.get("excluded"):
        acquis = True
    elif not isinstance(entry, dict):
        acquis = False
    else:
        chemin = _chemin_entree(
            target.parents[2], entry, index=index, cle=cle_entree,
        )
        empreinte_fichier = None
        if chemin is not None:
            try:
                stat = chemin.stat()
                empreinte_fichier = (
                    os.fspath(chemin), stat.st_size, stat.st_mtime_ns,
                    entry.get("bytes"),
                )
            except OSError:
                stat = None
        else:
            stat = None
        memorise = index.fichiers.get(cle_entree)
        if memorise is not None and memorise[0] == empreinte_fichier:
            acquis = memorise[1]
        elif stat is None or not stat_module.S_ISREG(stat.st_mode):
            acquis = False
        else:
            taille = stat.st_size
            annoncee = entry.get("bytes")
            acquis = taille > 0 and (
                not isinstance(annoncee, int) or annoncee <= 0 or taille == annoncee
            )
        if cle_entree is not None:
            index.fichiers[cle_entree] = (empreinte_fichier, acquis)
    if acquis and consumed is not None:
        consumed.add(cle_entree)
    return acquis


def print_clip_summary(clips: list) -> None:
    """Affiche un résumé du manifeste, sans URL ni donnée sensible."""
    total_size = sum(reported_bytes(clip.size) for clip in clips)
    print(f"  {len(clips)} clip(s), volume annoncé : environ {human_size(total_size)}")
    if clips:
        first = clip_datetime_utc(clips[0]).astimezone()
        last = clip_datetime_utc(clips[-1]).astimezone()
        print(f"  Période : {first:%Y-%m-%d %H:%M:%S %Z} -> {last:%Y-%m-%d %H:%M:%S %Z}")
        cameras = sorted({clip.name for clip in clips}, key=str.casefold)
        print(f"  Caméra(s) : {', '.join(cameras)}")


def parse_args() -> argparse.Namespace:
    programme = Path(sys.argv[0]).stem or "blink2video"
    parser = argparse.ArgumentParser(
        prog=programme,
        # Les verbes vont dans la description, pas dans un groupe d'arguments :
        # les déclarer à argparse en ferait de faux positionnels, qui
        # pollueraient la ligne d'usage et fausseraient l'analyse.
        description=(
            f"blink2video {runtime.VERSION}\n\n"
            "Gestion des caméras Blink depuis un ordinateur : direct, "
            "armement, archive horodatée.\n\nVerbes :\n"
            + "".join(f"  {nom:11} {verbe.fr}\n"
                      for nom, verbe in runtime.VERBES.items())
            + "\n  <verbe> --help donne les options de chacun."
        ),
        # Les exemples suivent l'ordre dans lequel on rencontre les verbes :
        # se connecter, regarder ce qu'il y a, récupérer, assembler, visionner,
        # puis automatiser. C'est un parcours, pas un catalogue.
        epilog="Premiers pas :\n" + "\n".join(
            f"  {programme} {commande:<20} {intention}"
            for commande, intention in (
                ("login", "se connecter une fois"),
                ("list", "voir ce que contient le module"),
                ("download", "récupérer les clips"),
                ("merge", "assembler les vidéos"),
                ("serve", "ouvrir l'interface"),
                ("autostart on", "surveiller à chaque session"),
            )
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"blink2video {runtime.VERSION}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=tuple(runtime.VERBES),
        # Pas de commande par défaut : sans argument, on affiche l'aide plutôt
        # que d'ouvrir une connexion au compte Blink. Une commande lancée sans
        # rien ne doit pas partir sur le réseau à l'insu de celui qui la tape.
        default=None,
        # L'aide détaillée de chaque verbe est imprimée sous l'aide standard,
        # en une seule liste : séparer les verbes traités ici de ceux qui sont
        # délégués n'apprend rien à l'utilisateur et laisse croire que les
        # premiers n'existent pas.
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--hub", help="nom du Sync Module à utiliser")
    parser.add_argument("--camera", help="ne garder que cette caméra")
    parser.add_argument(
        "--since",
        type=runtime.jours_non_negatifs,
        metavar="JOURS",
        help="ne garder que les clips des N derniers jours",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT,
        help=f"dossier de destination (défaut : {OUTPUT})",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="forcer le retéléchargement des clips visibles, même déjà acquis",
    )
    parser.add_argument(
        "--from", dest="source", choices=("usb", "cloud", "all"), default="all",
        help="où chercher les clips : « usb » la clé du module, « cloud » celui "
             "de l'abonnement, « all » les deux (défaut)",
    )
    # Une boucle propre au verbe : le cloud se sonde à la minute sans rien
    # réveiller, là où le manifeste USB mobilise le module et se contente de dix
    # minutes. Deux cadences valent mieux qu'un compromis unique.
    runtime.ajouter_boucle(parser)
    args = parser.parse_args()
    if args.command is None:
        # Sans commande, l'aide plutôt qu'une connexion au compte : une
        # commande tapée sans argument ne doit pas partir sur le réseau.
        parser.print_help()
        raise SystemExit(0)
    return args


async def traiter_cloud(blink: Blink, args, modules: list) -> CloudResult:
    """Inventorie, puis rapatrie, les clips que l'abonnement garde dans le cloud.

    Le compte répond ici, pas le module : aucune réservation du hub n'est donc
    nécessaire, et cette partie fonctionne même quand le module est occupé par
    un direct. Les fichiers rejoignent la même arborescence et le même registre
    que ceux de la clé, puisqu'un clip reste un clip."""
    clips = await read_cloud_manifest(blink, args.since)
    if args.camera:
        clips = [c for c in clips if c.name.casefold() == args.camera.casefold()]
    if not clips:
        return CloudResult()

    print("\n=== CLOUD DE L'ABONNEMENT ===")
    # Le rapprochement se fait avec ce qui est déjà au registre, et non avec le
    # manifeste USB : celui-ci ne montre que ce que la clé contient encore,
    # alors que le registre garde la trace de tout ce qui a été rapatrié.
    output = args.output.resolve()
    state = load_download_state(output)
    connus = [
        _ClipConnu(entree)
        for entree in state["clips"].values()
        if _entree_acquise(output, entree)
    ]
    tombstones = [
        _ClipConnu(entree)
        for entree in state["clips"].values()
        if isinstance(entree, dict) and entree.get("excluded")
    ]
    sans_tombstone = (
        rapprocher(tombstones, clips)[0] if tombstones else list(clips)
    )
    # `sans_tombstone` contient les clips sans exclusion ; une décision explicite
    # reste donc prioritaire même lors d'un retéléchargement forcé.
    clips_autorises = sans_tombstone
    ignores_exclus = len(clips) - len(clips_autorises)
    if args.overwrite:
        inedits, doublons = clips_autorises, []
    else:
        inedits, doublons = rapprocher(connus, clips_autorises)
    print(f"  {len(clips)} clip(s) dans le cloud, {len(doublons)} déjà acquis "
          f"par ailleurs, {len(inedits)} à rapatrier.")
    if args.command != "download" or not inedits:
        return CloudResult(skipped=len(doublons) + ignores_exclus)

    # Le registre attend un module pour former l'identité. Le cloud n'en
    # dépend pas : à défaut, le réseau du clip en tient lieu, ce qui suffit,
    # l'identité réelle restant la caméra et l'instant.
    downloaded = adopted = failed = 0
    for position, clip in enumerate(sorted(inedits, key=clip_datetime_utc), start=1):
        sync = _HubCloud(clip.network_id)
        target = target_path(output, clip, sync=sync, source="cloud")
        print(f"  [{position}/{len(inedits)}] {target.name}")
        _, entree_connue = _trouver_entree(
            state, sync, clip, source="cloud",
        )
        if (
            entree_connue is None
            and target.exists()
            and target.is_file()
            and target.stat().st_size > 0
            and not args.overwrite
        ):
            remember_download(state, sync, args.hub or "cloud", clip, output,
                              target, source="cloud")
            save_download_state(output, state)
            adopted += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        partiel = target.with_suffix(target.suffix + ".part")
        partiel.unlink(missing_ok=True)
        try:
            if await clip.download_to(blink, partiel):
                partiel.replace(target)
                remember_download(state, sync, args.hub or "cloud", clip, output,
                                  target, source="cloud")
                save_download_state(output, state)
                downloaded += 1
            else:
                failed += 1
                categorie, detail = getattr(
                    clip, "download_issue", None
                ) or ("média", "contenu indisponible")
                print(f"    Échec [{categorie}] : {detail}.")
        except (ClientError, OSError, asyncio.TimeoutError) as erreur:
            failed += 1
            print(f"    Échec [réseau] : {type(erreur).__name__}.")
        except Exception as erreur:  # Isoler un clip invalide des suivants.
            failed += 1
            print(f"    Échec [données] : {type(erreur).__name__}.")
        finally:
            partiel.unlink(missing_ok=True)

    resultat = CloudResult(
        downloaded=downloaded,
        adopted=adopted,
        skipped=len(doublons) + ignores_exclus,
        failed=failed,
    )
    print(
        f"  Terminé : {resultat.downloaded} téléchargé(s), "
        f"{resultat.adopted} adopté(s), {resultat.skipped} ignoré(s), "
        f"{resultat.failed} échec(s)."
    )
    return resultat


class _HubCloud:
    """Module de substitution, quand seul le cloud répond."""

    def __init__(self, network_id):
        self.sync_id = network_id or "cloud"
        self.network_id = network_id or ""


class _ClipConnu:
    """Entrée du registre présentée comme un clip, pour le rapprochement."""

    def __init__(self, entree: dict):
        self.name = entree.get("camera") or "camera"
        self.created_at = dt.datetime.fromisoformat(entree["created_at"])
        self.id = entree.get("remote_id") or 0
        self.device_id = entree.get("device_id") or ""
        self.network_id = entree.get("network_id") or ""


def _entree_acquise(output: Path, entree: dict) -> bool:
    """Une tombstone reste connue ; un média ordinaire doit exister et être entier."""
    if not isinstance(entree, dict):
        return False
    if entree.get("excluded"):
        return True
    chemin = _chemin_entree(output, entree)
    if chemin is None or not chemin.is_file():
        return False
    taille = chemin.stat().st_size
    annoncee = entree.get("bytes")
    return taille > 0 and (
        not isinstance(annoncee, int) or annoncee <= 0 or taille == annoncee
    )


async def main(args: argparse.Namespace) -> int:
    async with ClientSession() as session:
        blink = await connect(session)
        if blink is None:
            print("\nÉchec de la connexion Blink.")
            return 1

        print("\nConnexion Blink réussie.")
        print(f"Session sauvegardée dans : {CONFIG.resolve()}")
        print("\n=== SYNC MODULES ===")
        synchronisations = getattr(blink, "sync", None) or {}
        if not synchronisations:
            print("Aucun Sync Module trouvé sur ce compte.")
        for name, sync in synchronisations.items():
            print(f"- {name} (ID {sync.sync_id}, réseau {sync.network_id})")

        if args.command == "login":
            return 0

        if args.source == "usb" and not synchronisations:
            print("\nLa source USB exige un Sync Module ; utilisez --from cloud.")
            return 1

        try:
            modules = (
                [] if args.source == "cloud"
                else select_sync_modules(blink, args.hub)
            )
        except ValueError as error:
            print(f"\nErreur : {error}")
            return 2

        return await boucler(blink, args, modules)


async def boucler(blink: Blink, args, modules: list) -> int:
    """Répète le passage si --loop est donné, en gardant la session ouverte.

    La session est ouverte une fois pour toutes : à cadence rapide, se
    reconnecter à chaque tour coûterait plus cher que le travail lui-même, et
    multiplierait les authentifications sans raison."""
    while True:
        try:
            # Un seul rapatriement à la fois, quelle que soit la source : deux
            # processus qui prennent le même clip écriraient le même fichier
            # partiel, et le premier renommage laisserait l'autre dans le vide.
            with runtime.verrou("download", "download", attente=30):
                try:
                    code = await un_passage(blink, args, modules)
                finally:
                    runtime.fin_travail()
        except runtime.BusyError as erreur:
            print(f"Téléchargement déjà en cours ({erreur}).")
            code = 0
        if not args.loop:
            return code
        await asyncio.sleep(args.loop * 60)


async def un_passage(blink: Blink, args, modules: list) -> int:
    """Un tour : la clé USB de chaque module, puis le cloud du compte."""
    had_error = False
    neufs_total = 0
    for name, sync in ([] if args.source == "cloud" else modules):
        print(f"\n=== STOCKAGE LOCAL : {name} ===")
        try:
            clips = await read_local_manifest(sync)
        except RuntimeError as error:
            print(f"  Indisponible : {error}.")
            had_error = True
            continue

        clips = filter_clips(clips, args.camera, args.since)
        print_clip_summary(clips)

        if args.command != "download" or not clips:
            continue

        output = args.output.resolve()
        print(f"  Destination : {output}")
        state = load_download_state(output)
        pending = []
        adopted = 0
        index_registre = _index_registre(state)
        correspondances = _apparier_registre(
            state, sync, clips, index_registre,
        )
        for indice_clip, clip in enumerate(clips):
            target = target_path(output, clip, sync=sync, source="usb")
            _, entree_connue = correspondances.get(
                indice_clip, (None, None),
            )
            if isinstance(entree_connue, dict) and entree_connue.get("excluded"):
                continue
            if (
                isinstance(entree_connue, dict)
                and _entree_acquise(output, entree_connue)
                and not args.overwrite
            ):
                continue
            if (
                entree_connue is None
                and target.exists()
                and target.is_file()
                and target.stat().st_size > 0
                and not args.overwrite
            ):
                remember_download(state, sync, name, clip, output, target)
                adopted += 1
                continue
            pending.append(clip)

        save_download_state(output, state)
        already_downloaded = len(clips) - len(pending) - adopted
        print(
            f"  Incrémental : {len(pending)} nouveau(x), "
            f"{already_downloaded + adopted} déjà acquis."
        )
        if not pending:
            continue

        downloaded = skipped = failed = 0
        for position, clip in enumerate(pending, start=1):
            target = target_path(output, clip, sync=sync, source="usb")
            print(f"  [{position}/{len(pending)}] {target.name}")
            runtime.travail("Téléchargement des clips", position - 1, len(pending))
            try:
                result = await download_clip(blink, clip, target, args.overwrite)
            except Exception as error:  # Continuer avec les autres clips.
                print(f"    Échec : {type(error).__name__}: {error}")
                result = "failed"

            if result == "downloaded":
                downloaded += 1
                remember_download(state, sync, name, clip, output, target)
                save_download_state(output, state)
            elif result == "skipped":
                skipped += 1
                if target.exists() and target.stat().st_size > 0:
                    remember_download(state, sync, name, clip, output, target)
                    save_download_state(output, state)
            else:
                failed += 1
                print("    Échec du téléchargement après plusieurs tentatives.")

        print(
            f"  Terminé : {downloaded} téléchargé(s), "
            f"{skipped} déjà présent(s), {failed} échec(s)."
        )
        had_error = had_error or failed > 0
        neufs_total += downloaded

    if args.source != "usb":
        resultat_cloud = await traiter_cloud(blink, args, modules)
        had_error = resultat_cloud.had_error or had_error
        neufs_total += resultat_cloud.new_downloads

    if args.command == "download":
        # Ligne de synthèse, toutes sources confondues.
        print(f"\nNouveaux clips : {neufs_total}")
        runtime.marquer("download")
        if neufs_total:
            # Le verbe qui ramène est celui qui annonce : « watch » regarde les
            # caméras, il n'a pas à parler des clips.
            pluriel = "s" if neufs_total > 1 else ""
            runtime.toast(
                "Blink",
                f"{neufs_total} nouveau{'x' if neufs_total > 1 else ''} "
                f"clip{pluriel} récupéré{pluriel}. Cliquez pour ouvrir.",
                url="http://127.0.0.1:8765/",
            )

    return 1 if had_error else 0


# Point d'entrée unique. Les autres programmes gardent leur propre fichier et
# leur propre analyse d'arguments : on ne fait que les appeler, sans rien
# déplacer. Fusionner les quatre en un seul fichier donnerait un script de
# quatre mille lignes, moins lisible et impossible à éprouver par morceaux.
#
# C'est la forme des commandes à verbe, celle de git ou de docker : un nom à
# retenir, un verbe pour l'action. Chaque verbe reçoit tels quels les arguments
# qui le suivent, donc « blink2video.py review --port 8899 » revient exactement à
# « blink2video serve --port 8899 ».
# Les verbes, leur programme et leur description vivent dans runtime.VERBES :
# une seule table, lue ici pour l'aide et la délégation, par self_command pour
# la relance, et par docs.py pour les README.
DELEGUES = runtime.DELEGUES


def deleguer(verbe: str, arguments: list) -> int:
    """Passe la main au programme d'un verbe, dans le même processus.

    L'import est fait ici et pas en tête de fichier : ces modules importent
    eux-mêmes blink2video.py, et surtout ils tirent ffmpeg ou aiohttp derrière eux.
    Une simple demande de manifeste n'a pas à payer ce chargement."""
    import importlib

    module = importlib.import_module(DELEGUES[verbe])
    sys.argv = [f"{DELEGUES[verbe]}.py", *arguments]
    return int(module.main() or 0)


def ouvrir(arguments: list = ()) -> int:
    """Ouvre l'interface dans le navigateur, et dit si personne n'écoute.

    L'adresse est évidente pour qui la connaît ; elle ne l'est pas pour qui
    installe l'outil. Un verbe se trouve dans « --help », un port se retient
    mal."""
    import socket
    import webbrowser

    parseur = argparse.ArgumentParser(
        prog="blink2video open",
        description=ouvrir.__doc__.splitlines()[0],
    )
    parseur.add_argument("--port", type=runtime.port_valide, default=8765,
                         help="port de l'interface (défaut 8765)")
    options = parseur.parse_args(list(arguments))
    adresse = f"http://127.0.0.1:{options.port}/"

    with socket.socket() as prise:
        prise.settimeout(2)
        if prise.connect_ex(("127.0.0.1", options.port)) != 0:
            print(f"Personne n'écoute sur {adresse}.")
            print("Lancez « blink2video serve », ou « blink2video autostart on » "
                  "pour que l'interface démarre avec la session.")
            return 1

    print(f"Ouverture de {adresse}")
    webbrowser.open(adresse)
    return 0


def arreter(arguments: list = ()) -> int:
    """Arrête les instances en cours, y compris celle du démarrage automatique.

    Une instance lancée sans console ne peut pas recevoir de Ctrl+C, et la tuer
    par son seul numéro laissait ses verbes derrière elle : « watch » continuait
    de tourner, orphelin, en tenant le module de synchronisation. La fiche
    déposée au démarrage donne le processus à interrompre, et le système donne
    sa descendance."""
    # Les options passent par argparse comme pour les autres verbes, même s'il
    # n'en a aucune : sans cela « stop --help » arrêtait l'instance au lieu de
    # s'expliquer, ce que la suite de tests faisait à chaque passage, sur
    # l'instance réelle de la machine.
    argparse.ArgumentParser(
        prog="blink2video stop",
        description=arreter.__doc__.splitlines()[0],
    ).parse_args(list(arguments))

    instances = runtime.lire_instances()
    if not instances:
        print("Rien ne tourne.")
        return 0

    for fiche in instances:
        commande = " ".join(" ".join(groupe) for groupe in fiche.get("verbes") or [])
        print(f"Arrêt de « {commande or 'blink2video'} » "
              f"(PID {fiche['pid']}, depuis {fiche.get('depuis', '?')})")
        runtime.arreter_processus(int(fiche["pid"]))
        for enfant in fiche.get("enfants") or []:
            if runtime.processus_vivant(int(enfant)):
                runtime.arreter_processus(int(enfant), avec_descendance=True)
        # La fiche est retirée ici : le processus tué n'exécute pas ses propres
        # adieux, et une fiche orpheline ferait croire qu'il tourne encore.
        Path(fiche["fiche"]).unlink(missing_ok=True)

    restants = [str(numero) for fiche in instances
                for numero in [fiche["pid"], *(fiche.get("enfants") or [])]
                if runtime.processus_vivant(int(numero))]
    if restants:
        print("Toujours en vie : " + ", ".join(restants))
        return 1
    print("Arrêté.")
    return 0


def executer(groupes: list) -> int:
    """Exécute les verbes cités, ensemble.

    Un seul verbe est traité dans ce processus, ce qui garde la sortie et le
    code de retour directs. Plusieurs sont lancés côte à côte et attendus : ils
    s'arrêtent ensemble, faute de quoi un Ctrl+C laisserait derrière lui des
    programmes sans personne pour les arrêter."""
    if len(groupes) == 1 and groupes[0][0] == "start":
        # L'aide doit s'afficher, pas déclencher la configuration : sans ce
        # traitement, « start --help » lançait les boucles et ne rendait jamais
        # la main, ce que la suite de tests a montré en se bloquant dessus.
        if {"-h", "--help"} & set(groupes[0][1:]):
            print("usage : blink2video start [options de serve]")
            print()
            print("Lance la configuration recommandée :")
            print()
            print("  blink2video " + " ".join(runtime.STANDARD))
            print()
            print("Les options données ici vont à l'interface, --port par exemple.")
            print("« blink2video stop » arrête l'ensemble.")
            return 0
        # « start » n'est pas un travail de plus : c'est le nom de la
        # composition recommandée, options comprises. Les options données après
        # lui s'ajoutent au premier verbe, « serve », d'où le --port qui marche.
        supplement = groupes[0][1:]
        return executer(runtime.decouper_verbes(
            [runtime.STANDARD[0], *supplement, *runtime.STANDARD[1:]]))

    if len(groupes) == 1 and groupes[0][0] == "open":
        return ouvrir(groupes[0][1:])

    if any(groupe[0] == "stop" for groupe in groupes):
        if len(groupes) > 1:
            print("« stop » s'emploie seul : il arrête ce qui tourne déjà.")
            return 2
        return arreter(groupes[0][1:])

    if len(groupes) == 1:
        verbe, *arguments = groupes[0]
        if verbe in DELEGUES:
            # « update » ne s'inscrit pas : une fiche sert à retrouver ce qu'il
            # faut arrêter, et la mise à jour est précisément ce qui arrête tout
            # le reste. Inscrite, elle se trouvait elle-même dans la liste et se
            # tuait au premier « stop », en silence et à mi-chemin.
            if verbe != "update":
                runtime.inscrire_instance(groupes)
            return deleguer(verbe, arguments)
        sys.argv = ["blink2video", verbe, *arguments]
        return asyncio.run(main(parse_args()))

    # Ce qui se termine s'enchaîne, ce qui ne se termine pas tourne à côté.
    # « serve » porte un --loop implicite : il ne rend jamais la main, comme
    # tout verbe à qui on demande de se répéter. Les autres font un passage et
    # s'arrêtent, donc les faire tourner en même temps n'aurait aucun sens :
    # l'assemblage démarrerait pendant que le téléchargement écrit encore.
    persistant = [g for g in groupes if g[0] == "serve" or "--loop" in g]
    ponctuels = [g for g in groupes if g not in persistant]

    runtime.inscrire_instance(groupes)
    lances = []
    for verbe, *arguments in persistant:
        lances.append((verbe, runtime.demarrer(
            runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
            creationflags=runtime.flags_enfant(),
            # Sa propre session hors Windows : « stop » peut alors tuer son
            # groupe, ffmpeg compris, sans emporter le terminal qui a lancé
            # l'ensemble.
            start_new_session=(os.name != "nt"))))
        print(f"Lancé : {verbe} {' '.join(arguments)}".rstrip())
    runtime.inscrire_instance(groupes, [p.pid for _, p in lances])

    # Les passages uniques, l'un après l'autre, dans l'ordre où ils sont cités.
    pire_ponctuel = 0
    for verbe, *arguments in ponctuels:
        print(f"Étape : {verbe} {' '.join(arguments)}".rstrip())
        resultat = runtime.lancer(
            runtime.self_command(verbe, *arguments), cwd=str(runtime.app_dir()),
            stdin=subprocess.DEVNULL, check=False,
        )
        pire_ponctuel = max(pire_ponctuel, abs(resultat.returncode))
    if not lances:
        return pire_ponctuel

    # Surveillés ensemble plutôt qu'attendus l'un après l'autre : un verbe qui
    # meurt à la première seconde doit se voir tout de suite, et non à la fin
    # d'une boucle qui tournera des jours. Les autres continuent, l'interface
    # qui tombe n'étant pas une raison d'arrêter la surveillance.
    pire = 0
    annonces = set()
    try:
        while any(processus.poll() is None for _, processus in lances):
            for rang, (verbe, processus) in enumerate(lances):
                code = processus.poll()
                if code is None or rang in annonces:
                    continue
                annonces.add(rang)
                pire = max(pire, abs(code))
                print(f"Arrêté : {verbe}"
                      + (f" (code {code})" if code else " (fin normale)"))
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        for _, processus in lances:
            if processus.poll() is None:
                processus.terminate()
    return max(pire, pire_ponctuel)


if __name__ == "__main__":
    try:
        # « autostart » vient nécessairement en tête : il n'exécute rien, il
        # ordonnance ce qui suit. Les autres verbes se citent dans n'importe
        # quel ordre, chacun suivi de ses options.
        if len(sys.argv) > 1 and sys.argv[1] == "autostart":
            raise SystemExit(deleguer("autostart", sys.argv[2:]))
        if len(sys.argv) > 1 and sys.argv[1] in runtime.VERBES:
            raise SystemExit(executer(runtime.decouper_verbes(sys.argv[1:])))
        # Une option avant le premier verbe n'appartient à personne :
        # « blink2video --loop 5 merge » se lisait jusqu'ici comme une commande
        # racine qui boucle sur rien, et tournait indéfiniment sans rien faire.
        if len(sys.argv) > 1 and sys.argv[1].startswith("-")                 and sys.argv[1] not in ("-h", "--help", "--version"):
            print(f"« {sys.argv[1]} » précède le premier verbe : les options "
                  "suivent le verbe auquel elles s'appliquent.")
            print(f"Verbes : {', '.join(runtime.VERBES)}")
            raise SystemExit(2)
        raise SystemExit(asyncio.run(main(parse_args())))
    except ValueError as erreur:
        print(f"{erreur}. Verbes : {', '.join(runtime.VERBES)}")
        raise SystemExit(2)
    except (KeyboardInterrupt, EOFError):
        print("\nConnexion annulée.")
        raise SystemExit(130)
