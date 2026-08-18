"""Interface locale pour visionner les clips Blink et les écarter ou les reprendre.

Pourquoi un petit serveur HTTP et pas un fichier HTML autonome : une page
ouverte en file:// ne peut ni déplacer un fichier ni écrire un registre. Elle
ne saurait produire qu'une liste à recopier à la main, c'est-à-dire la moitié
d'un outil. Il faut donc un processus local, et une fois qu'il existe, autant
qu'il serve aussi les vidéos.

Pourquoi pas une fenêtre Tk : Tk ne lit pas de vidéo. Il faudrait déléguer à un
lecteur externe, et la boucle « je regarde, je juge, je passe au suivant » se
casse à chaque clip. La balise <video> d'un navigateur donne gratuitement la
lecture, le défilement, la vitesse et le plein écran.

Rien d'autre que la bibliothèque standard n'est requis, et toutes les décisions
(exclure, réintégrer) sont déléguées à merge_daily.py : cette interface ne
réimplémente aucune règle, elle appelle les mêmes fonctions que la ligne de
commande.
"""

import argparse
import asyncio
import datetime as dt
import http.server
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

from aiohttp import ClientSession

import blink_auth
import blink_engine
import maj
import merge_daily as md


BASE_DIR = runtime.app_dir()

# Un identifiant de clip est un chemin relatif « caméra/mois/fichier.mp4 ».
# Tout ce qui arrive du navigateur est confronté au registre avant d'ouvrir
# quoi que ce soit : aucun chemin fabriqué à la main n'est servi.
IDENTITY = re.compile(r"^[\w.\- ]+(/[\w.\- ]+)*\.mp4$")

# Avancement annoncé par blink2video.py et merge_daily.py, et titres de phase émis
# par daily.py (« === TÉLÉCHARGEMENT INCRÉMENTAL === »).
PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")
# Ligne d'avancement à l'intérieur d'un clip : « [3/24] 45% », rien d'autre.
INNER = re.compile(r"^\s*\[\d+/\d+\]\s+(\d+)%\s*$")
HEADING = re.compile(r"^=== (.+?) ===$")

# Limite le nombre d'extractions de vignettes simultanées (voir send_thumb).
THUMB_SLOTS = threading.Semaphore(2)

# Le Sync Module ne traite qu'une commande à la fois et refuse les suivantes
# avec « System is busy ». Inutile de le lui demander : mesuré, son état publié
# est identique au repos et pendant un direct (busy reste vide, status reste
# « online »), et le lui demander coûte treize secondes, plus cher que de tenter
# la commande. On tient donc le compte localement, puisque c'est nous qui
# l'occupons : ce jeton est pris par un direct comme par une actualisation, ce
# qui les empêche de se marcher dessus.
#
# Il ne couvre évidemment pas un autre client, l'application mobile par
# exemple : là seul le refus renseigne, d'où la reprise avec attente croissante
# côté blink2video.py.
MODULE_SLOT = threading.Semaphore(1)
# Diagnostic seul : dit qui tient MODULE_SLOT et depuis quand, pour qu'un 409
# distingue « occupé, ça va se libérer » d'« occupé depuis 20 minutes, ça sent
# la fuite » sans avoir à redémarrer le serveur pour le savoir.
MODULE_SLOT_INFO: dict = {}


def _slot_pris(quoi: str, name: str = "") -> None:
    MODULE_SLOT_INFO.clear()
    MODULE_SLOT_INFO.update({"quoi": quoi, "camera": name, "depuis": time.monotonic()})


def _slot_rendu() -> None:
    MODULE_SLOT_INFO.clear()


def _slot_occupe_message() -> str:
    quoi = MODULE_SLOT_INFO.get("quoi")
    if not quoi:
        return "Le module est deja occupe (direct ou actualisation)."
    depuis = time.monotonic() - MODULE_SLOT_INFO.get("depuis", time.monotonic())
    camera = MODULE_SLOT_INFO.get("camera") or ""
    cible = f" ({camera})" if camera else ""
    return (f"Le module est deja occupe : {quoi}{cible}, depuis "
            f"{depuis:.0f}s.")
LIVE_MAX_SECONDS = 300
# Délai accordé à la première image. Une caméra sur batterie doit se réveiller,
# donc on est patient ; au-delà on considère qu'elle ne répondra pas.
LIVE_FIRST_FRAME_SECONDS = 40
LIVE_BOUNDARY = "blinkframe"

# Aucune vignette n'est redemandée d'elle-même : elle est récupérée une fois,
# puis conservée jusqu'à ce qu'on clique sur Actualiser. Une image qui change
# seule sous les yeux, sans qu'on l'ait demandé, n'est pas un service rendu.


def safe_file(name: str) -> str:
    """Nom de fichier sûr pour une caméra, dont le nom est libre côté Blink."""
    cleaned = re.sub(r"[^\w.-]+", "_", name.strip(), flags=re.UNICODE).strip("._")
    return cleaned or "camera"


def read_entries(paths: dict) -> dict:
    return md.read_registry(paths["input"] / md.DOWNLOAD_STATE)


ETIQUETTES_SOURCE = {"usb": "USB", "cloud": "cloud"}

# Un seul réassemblage à la fois : deux assemblages simultanés de la même
# journée écriraient le même fichier.
REASSEMBLAGE = threading.Lock()

# Écarter un clip ne touche que le registre. Lui donner son propre verrou plutôt
# que celui de l'actualisation : sinon un clic pendant un téléchargement attend
# la fin de ce téléchargement, et le bouton semble bloqué une minute entière.
REGISTRE = threading.Lock()


def provenances(entrees: dict) -> dict:
    """Où sont enregistrés les clips de chaque caméra, par caméra.

    Constaté plutôt que déduit : c'est la provenance des clips réellement
    rapatriés, pas une lecture des réglages du compte. Une caméra couverte par
    un abonnement dit « cloud », une autre « USB », et l'absence des deux se
    voit aussi, ce qui explique une caméra qui ne produit rien. Les entrées
    antérieures à cette distinction viennent forcément de la clé, seule source
    existante alors."""
    vues = {}
    for entree in entrees.values():
        camera = str(entree.get("camera") or "").strip()
        if camera:
            vues.setdefault(camera, set()).add(str(entree.get("source") or "usb"))
    return {camera: " + ".join(ETIQUETTES_SOURCE.get(s, s) for s in sorted(sources))
            for camera, sources in vues.items()}


def known_identities(paths: dict) -> set:
    """Chemins de clips que le registre reconnaît, sans rien mesurer.

    Séparé de collect() à dessein : la validation d'une requête média passe par
    ici et doit rester instantanée, alors que l'inventaire complet peut avoir à
    lancer ffmpeg."""
    return {
        entry["path"]
        for entry in read_entries(paths).values()
        if isinstance(entry, dict) and entry.get("path")
    }


CAMERA_FACTS = "cameras.json"
# Durée mesurée des clips écartés, qui n'ont plus d'entrée dans le registre
# normalisé (voir collect) : sans ce cache, ffmpeg -i était relancé pour
# chacun d'eux à chaque ouverture de la page.
EXCLUDED_DURATIONS = "excluded_durations.json"

# Fenêtre par défaut de /api/clips : au-delà, le nombre de vignettes ne fait
# que croître de jour en jour sans jamais être consulté. L'historique complet
# reste disponible sur demande explicite (paramètre ?all=1), jamais perdu.
DEFAULT_WINDOW_DAYS = 30

# L'API ne renvoie que des noms de code internes d'Amazon, jamais de référence
# commerciale. Deux seulement sont établis : « owl », documenté comme étant le
# Blink Mini dans le code de blinkpy, et « catalina », identifié par le
# propriétaire du matériel comme un Blink Outdoor. Tout autre nom de code reste
# affiché tel quel, annoncé comme interne, plutôt que traduit au jugé.
CAMERA_MODELS = {"owl": "Blink Mini", "catalina": "Blink Outdoor"}
# Le module, lui, porte sa génération dans son type : sm2 = Sync Module 2.
MODULE_MODELS = {"sm": "Sync Module", "sm2": "Sync Module 2"}


def model_name(kind: str | None) -> str | None:
    if not kind:
        return None
    return CAMERA_MODELS.get(kind) or f"type Blink « {kind} »"


def remember_cameras(paths: dict, systems: list) -> None:
    """Note le modèle de chaque caméra à côté des vignettes.

    L'inventaire des clips doit rester instantané et fonctionner hors ligne :
    il ne peut pas interroger Blink pour connaître un modèle. On garde donc ce
    qu'on a appris lors du dernier passage par la vue Direct."""
    facts = {c["name"]: {"kind": c.get("kind"), "firmware": c.get("firmware")}
             for s in systems for c in s["cameras"]}
    if facts:
        md.save_json(paths["thumbs"] / CAMERA_FACTS, facts)


def load_excluded_durations(paths: dict) -> dict:
    cache = md.load_json(paths["thumbs"] / EXCLUDED_DURATIONS, {})
    return cache if isinstance(cache, dict) else {}


def probe_duration_cached(ffmpeg: str, source: Path, identity: str, cache: dict) -> float:
    """Durée d'un clip écarté, mesurée une seule fois puis mise en cache.

    La clé retient taille et date de modification du fichier source : un clip
    remplacé (réparation d'un média absent, par exemple) est ainsi re-mesuré
    plutôt que de garder une durée périmée."""
    stat = source.stat()
    empreinte = [stat.st_size, stat.st_mtime]
    entree = cache.get(identity)
    if entree and entree.get("empreinte") == empreinte:
        return entree["duration"]
    duration = probe_duration(ffmpeg, source)
    cache[identity] = {"duration": duration, "empreinte": empreinte}
    return duration


def collect(paths: dict, timezone: ZoneInfo, ffmpeg: str = "",
            depuis: "dt.date | None" = None) -> dict:
    """Inventorie les clips connus du registre de téléchargement.

    Contrairement à merge_daily.load_groups, les clips écartés sont conservés :
    c'est précisément ce qu'on veut pouvoir revoir et reprendre.

    `depuis` borne la fenêtre renvoyée : le stock grossit chaque jour, et sans
    cette borne la page finirait par transmettre et dessiner un nombre de
    vignettes sans rapport avec ce qu'on regarde réellement. L'historique
    complet reste accessible explicitement (voir DEFAULT_WINDOW_DAYS)."""
    entries = read_entries(paths)
    # Les durées déjà mesurées par merge_daily sont reprises telles quelles ;
    # seules celles des clips écartés, dont l'entrée a été balayée du registre
    # normalisé, restent à mesurer, via leur propre cache (voir
    # load_excluded_durations) pour ne pas relancer ffmpeg à chaque requête.
    probed = md.load_json(paths["normalized"] / md.NORMALIZED_STATE, {}).get("clips")
    probed = probed if isinstance(probed, dict) else {}
    facts = md.load_json(paths["thumbs"] / CAMERA_FACTS, {})
    duration_cache = None

    clips = []
    total = 0
    for entry in entries.values():
        try:
            identity = entry["path"]
            created = md.parse_created_at(entry["created_at"])
            camera = str(entry.get("camera") or "camera").strip() or "camera"
        except (KeyError, TypeError, ValueError):
            continue

        local = created.astimezone(timezone)
        total += 1
        if depuis is not None and local.date() < depuis:
            continue
        excluded = bool(entry.get("excluded"))
        # On sert de préférence la version normalisée : c'est celle qui porte
        # l'horodatage incrusté, donc celle qui aide vraiment à juger.
        source = None
        for kind in ("normalized", "input", "excluded"):
            if (paths[kind] / identity).is_file():
                source = kind
                break

        probe = (probed.get(identity) or {}).get("probe") or {}
        seconds = probe.get("duration")
        # Un clip écarté n'a plus d'entrée dans le registre normalisé : on le
        # mesure alors une fois, depuis Blink_Excluded, puis on se souvient.
        if seconds is None and source and ffmpeg:
            if duration_cache is None:
                duration_cache = load_excluded_durations(paths)
            seconds = probe_duration_cached(
                ffmpeg, paths[source] / identity, identity, duration_cache)

        clips.append({
            "identity": identity,
            "camera": camera,
            "day": local.date().isoformat(),
            "time": local.strftime("%H:%M:%S"),
            "excluded": excluded,
            "source": source,
            "origine": ETIQUETTES_SOURCE.get(str(entry.get("source") or "usb"),
                                             str(entry.get("source"))),
            "duration": float(seconds or 0.0),
            # Pas de « modèle » : l'API Blink n'expose qu'un nom de code interne
            # (« owl », « catalina ») dont seul le premier est documenté. La
            # définition de l'image, elle, est mesurée et parlante.
            "model": model_name((facts.get(camera) or {}).get("kind")),
        })

    if duration_cache is not None:
        md.save_json(paths["thumbs"] / EXCLUDED_DURATIONS, duration_cache)

    # Du plus récent au plus ancien : c'est ce qu'on vient regarder. Un seul
    # tri global est nécessaire ; trier ensuite par caméra ferait passer une
    # caméra plus ancienne devant les clips récents d'une autre caméra.
    clips.sort(key=lambda clip: (clip["day"], clip["time"]), reverse=True)
    return {
        "clips": clips,
        "cameras": sorted({clip["camera"] for clip in clips}),
        # Le modèle est propre à la caméra, pas au clip : envoyé une fois ici,
        # et retiré de chaque clip pour qu'aucun affichage ne le répète.
        "passages": runtime.passages(),
        "sources": provenances(entries),
        "models": {nom: modele for nom, modele in (
            (clip["camera"], clip.pop("model", None)) for clip in clips) if modele},
        "days": sorted({clip["day"] for clip in clips}, reverse=True),
        # Permet à la page de dire « 30 derniers jours, X clips sur Y connus »
        # et de proposer explicitement de charger le reste.
        "window_days": None if depuis is None else DEFAULT_WINDOW_DAYS,
        "total_known": total,
    }


def collect_videos(paths: dict, ffmpeg: str) -> dict:
    """Inventorie les vidéos assemblées, par période.

    On lit le disque plutôt qu'un registre : ces fichiers sont le produit
    visible du pipeline, et les lister tels qu'ils existent évite d'afficher
    une entrée pour une vidéo effacée à la main."""
    result = {}
    for kind in ("daily", "weekly", "monthly"):
        items = []
        root = paths[kind]
        if root.is_dir():
            for camera_dir in sorted(
                p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
            ):
                for video in sorted(camera_dir.glob("*.mp4"), reverse=True):
                    if not md.valid_mp4(video):
                        continue
                    items.append({
                        "kind": kind,
                        "path": f"{camera_dir.name}/{video.name}",
                        "camera": camera_dir.name,
                        "label": video.stem.replace(f"_{camera_dir.name}", ""),
                        "bytes": video.stat().st_size,
                        "duration": probe_duration(ffmpeg, video),
                    })
        result[kind] = items
    return result


_DURATIONS: dict = {}


def probe_facts(ffmpeg: str, path: Path) -> tuple:
    """Durée et définition d'une vidéo, mémorisées tant qu'elle ne change pas.

    Chaque mesure coûte un lancement de ffmpeg ; sans ce cache, ouvrir l'onglet
    des mensuelles relancerait l'analyse à chaque affichage."""
    key = (str(path), path.stat().st_mtime_ns)
    if key not in _DURATIONS:
        try:
            duration, width, height, _, _ = md.probe_clip_info(ffmpeg, path)
            _DURATIONS[key] = (duration, width, height)
        except RuntimeError:
            _DURATIONS[key] = (0.0, 0, 0)
    return _DURATIONS[key]


def probe_duration(ffmpeg: str, path: Path) -> float:
    return probe_facts(ffmpeg, path)[0]


class LecteurTube:
    """Lit un tube bloquant en continu dans un fil unique, pour donner à
    chaque lecture un vrai délai sans jamais créer de second lecteur
    concurrent sur le même tube.

    `pipe.read(n)` est bloquant et ne se laisse pas interrompre. Le lancer
    dans un fil à *chaque* appel (comme le faisait l'ancien
    `read_with_deadline`, réutilisé en boucle par le direct) laisse un fil
    orphelin dès qu'un appel dépasse son délai : `pipe.read()` continue
    d'attendre en arrière-plan, et l'appel suivant en lance un second,
    concurrent du premier sur le même tube. Le prochain octet qui arrive
    peut alors atterrir chez le fil orphelin, invisible pour l'appelant
    (bug réel, voir AUDIT-2026-08-13.md section 28.22 pour `/live-mse`, et
    28.26 pour la boucle d'envoi principale des deux directs — un délai
    global vérifié seulement *entre* deux lectures ne borne rien si une
    lecture individuelle peut, elle, bloquer indéfiniment)."""

    def __init__(self, pipe, taille_bloc: int = 16384):
        self._file: queue.Queue = queue.Queue()
        self._fin = False

        def lire_en_continu():
            while True:
                morceau = pipe.read(taille_bloc)
                self._file.put(morceau)
                if not morceau:
                    return

        threading.Thread(target=lire_en_continu, daemon=True).start()

    def lire(self, delai: float):
        """Un bloc de données ; ``b""`` si le tube est à sa vraie fin (EOF) ;
        ``None`` si rien n'est arrivé avant `delai` (le flux est
        probablement juste lent — le fil, lui, continue d'attendre, un
        appel suivant peut encore recevoir quelque chose)."""
        if self._fin:
            return b""
        try:
            morceau = self._file.get(timeout=max(0.01, delai))
        except queue.Empty:
            return None
        if not morceau:
            self._fin = True
        return morceau


_MOOV_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


def h264_mime_codec_from_moov(segment: bytes) -> str:
    """Cherche avcC dans un segment d'initialisation MP4 (ftyp+moov) pour en
    tirer la chaîne de codec MSE exacte (avc1.PPCCLL : profil, contraintes,
    niveau). Deviner cette chaîne plante addSourceBuffer dès que la caméra
    encode dans un profil ou un niveau différent de celui supposé ; la lire
    dans le flux réel est le seul moyen fiable.

    Ne suit que le chemin utile : moov > trak > mdia > minf > stbl > stsd >
    avc1 > avcC. avcC porte déjà profil/contraintes/niveau en clair
    (AVCDecoderConfigurationRecord), pas besoin de redescendre jusqu'au
    SPS qu'il contient lui-même."""
    def walk(buf: bytes) -> bytes | None:
        i, n = 0, len(buf)
        while i + 8 <= n:
            size = int.from_bytes(buf[i:i + 4], "big")
            kind = buf[i + 4:i + 8]
            if size < 8 or i + size > n:
                break
            payload = buf[i + 8:i + size]
            if kind == b"avcC":
                return payload
            if kind in _MOOV_CONTAINERS:
                found = walk(payload)
            elif kind == b"stsd":
                found = walk(payload[8:])  # version+flags+entry_count
            elif kind == b"avc1":
                # SampleEntry (8) + champs fixes VisualSampleEntry (70) avant
                # les boîtes filles (avcC, ...).
                found = walk(payload[78:])
            else:
                found = None
            if found is not None:
                return found
            i += size
        return None

    avcc = walk(segment)
    if avcc is None or len(avcc) < 4:
        raise RuntimeError(
            "avcC introuvable dans le segment d'initialisation MP4 (profil illisible).")
    profile_idc, constraint, level_idc = avcc[1], avcc[2], avcc[3]
    return f"avc1.{profile_idc:02x}{constraint:02x}{level_idc:02x}"


def read_mp4_init_segment(lecteur: LecteurTube, seconds: float) -> bytes:
    """Accumule la sortie d'ffmpeg jusqu'à pouvoir y lire un avcC complet, ou
    renonce au bout de `seconds`. Un seul bloc de 4096 octets (comme pour le
    MJPEG) ne suffit pas toujours : le moov peut déborder du premier bloc.

    Prend un `LecteurTube` déjà créé, pas un tube brut : le même doit
    ensuite servir à la boucle d'envoi principale de l'appelant. En créer
    un second sur le même tube réintroduirait le bug des lecteurs
    concurrents que `LecteurTube` existe justement pour éviter."""
    deadline = time.monotonic() + seconds
    buf = bytearray()
    while time.monotonic() < deadline and len(buf) < 65536:
        morceau = lecteur.lire(deadline - time.monotonic())
        if not morceau:
            break  # b"" (EOF) ou None (délai global épuisé) : rien de plus à tenter
        buf += morceau
        try:
            h264_mime_codec_from_moov(bytes(buf))
            break  # trouvé : pas la peine de lire plus avant d'envoyer les en-têtes
        except RuntimeError:
            continue
    return bytes(buf)


# Dernier échec de direct, relu par la page : une balise <img> ne peut pas lire
# le corps d'une réponse en erreur, elle ne voit qu'un échec de chargement.
LAST_LIVE_ERROR: dict = {}


async def _stop_stream(stream, feed) -> str:
    """Referme proprement un direct, et surtout prévient Blink qu'il est fini.

    L'ordre compte. `stream.stop()` ferme la liaison vers Amazon, ce qui fait
    sortir la boucle de scrutation de blinkpy, dont le bloc `finally` envoie la
    commande « done » qui libère la caméra. Il faut donc *attendre* cette tâche,
    pas l'annuler : une tâche annulée ne peut plus rien attendre, la commande ne
    partait jamais, et Blink laissait la session ouverte. La caméra suivante
    trouvait alors le module occupé et ne renvoyait aucune image.

    L'annulation reste en dernier recours, suivie d'un envoi manuel de « done »
    pour ne pas laisser une session pendante côté Amazon."""
    stream.stop()
    if feed is None:
        return "aucune tache a attendre"
    try:
        await asyncio.wait_for(feed, timeout=20)
        return "session rendue normalement"
    except asyncio.TimeoutError:
        pass
    except Exception as error:
        return f"fin de flux : {type(error).__name__}"

    try:
        from blinkpy import api

        await api.request_command_done(
            stream.camera.sync.blink, stream.camera.network_id, stream.command_id
        )
        return "session rendue de force"
    except Exception as error:
        return f"session peut-etre restee ouverte : {type(error).__name__}"


class BlinkSession:
    """Session Blink partagée, vivant sur sa propre boucle asyncio.

    Armer une caméra ou ouvrir un direct suppose une session authentifiée. La
    rouvrir à chaque clic coûterait deux secondes et un aller-retour inutile
    chez Amazon : on la garde donc ouverte, et on lui parle depuis les fils du
    serveur HTTP via `call`."""

    def __init__(self):
        self.lock = threading.Lock()
        self.loop = None
        self.session = None
        self.blink = None

    def _ensure_loop(self):
        if self.loop is None:
            self.loop = asyncio.new_event_loop()
            threading.Thread(
                target=lambda: (asyncio.set_event_loop(self.loop),
                                self.loop.run_forever()),
                daemon=True,
            ).start()
        return self.loop

    def call(self, coroutine_factory, timeout: float = 60.0):
        """Exécute `coroutine_factory(blink)` sur la boucle, en se connectant
        si besoin. Lève RuntimeError si aucune session valable n'existe."""
        with self.lock:
            loop = self._ensure_loop()

            async def run():
                if self.blink is None:
                    self.session = ClientSession()
                    self.blink = await blink_auth.connect_saved(self.session)
                if self.blink is None:
                    raise RuntimeError(
                        "Session Blink absente ou expirée. Reconnectez-vous "
                        "depuis le bouton Actualiser."
                    )
                return await coroutine_factory(self.blink)

            return asyncio.run_coroutine_threadsafe(run(), loop).result(timeout)

    def forget(self):
        """Oublie la session courante : la prochaine demande se reconnectera."""
        with self.lock:
            self.blink = None
            if self.session is not None and self.loop is not None:
                asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
            self.session = None

    def find_camera(self, blink, name: str):
        for sync in blink.sync.values():
            for camera_name, camera in sync.cameras.items():
                if camera_name.strip() == name.strip():
                    return sync, camera
        raise RuntimeError(f"Caméra inconnue : {name}")


class LoginFlow:
    """Conduit une connexion Blink dont les réponses arrivent du navigateur.

    La difficulté : les identifiants et le code de vérification arrivent dans
    deux requêtes HTTP distinctes, mais doivent parler à la même session Blink,
    ouverte entre les deux. On garde donc une boucle asyncio vivante dans un
    fil, et la tâche de connexion s'y suspend en attendant le code au lieu de
    le lire au clavier.

    Le mot de passe ne quitte jamais la mémoire de ce processus : blink2video.py
    n'écrit que les jetons de session, jamais le mot de passe."""

    def __init__(self):
        self.loop = None
        self.future = None
        self.queue = None
        self.asked = threading.Event()

    def start(self, username: str, password: str) -> dict:
        self.close()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._serve, daemon=True).start()
        self.asked.clear()
        self.future = asyncio.run_coroutine_threadsafe(
            self._login(username, password), self.loop
        )
        return self.wait()

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _login(self, username: str, password: str) -> dict:
        self.queue = asyncio.Queue()

        async def ask_code(attempt: int) -> str:
            self.asked.set()
            return await self.queue.get()

        try:
            async with ClientSession() as session:
                blink = await blink_auth.login(session, username, password, ask_code)
        except Exception as error:  # remonter la cause plutôt que planter le serveur
            return {"status": "error", "message": f"{type(error).__name__}: {error}"}
        if blink is None:
            return {"status": "error", "message": "Identifiants ou code refusés."}
        return {"status": "ok"}

    def submit_code(self, code: str) -> dict:
        if self.loop is None or self.queue is None:
            return {"status": "error", "message": "Aucune connexion en cours."}
        self.asked.clear()
        self.loop.call_soon_threadsafe(self.queue.put_nowait, code)
        return self.wait()

    def wait(self, timeout: float = 120.0) -> dict:
        """Rend la main dès qu'un code est réclamé ou que la connexion aboutit."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.future.done():
                result = self.future.result()
                self.close()
                return result
            if self.asked.is_set():
                return {"status": "2fa"}
            time.sleep(0.1)
        self.close()
        return {"status": "error", "message": "Délai dépassé."}

    def close(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop, self.future, self.queue = None, None, None


BLINK = BlinkSession()


class Handler(http.server.BaseHTTPRequestHandler):
    # HTTP/1.1 pour garder la connexion ouverte : un navigateur qui se déplace
    # dans une vidéo enchaîne les requêtes Range, une par saut. En HTTP/1.0 il
    # rouvrirait une connexion à chaque fois.
    protocol_version = "HTTP/1.1"
    paths: dict = {}
    timezone: ZoneInfo = ZoneInfo("Europe/Paris")
    hub: str = "Maison"
    ffmpeg: str = ""
    lock = threading.Lock()
    login_flow = LoginFlow()

    def log_message(self, fmt, *args):
        pass  # le journal d'accès n'apprend rien ici

    # ------------------------------------------------------------------ envoi

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_media(self, path: Path) -> None:
        """Sert un fichier en gérant les requêtes Range.

        Sans Range, un navigateur ne peut pas se déplacer dans la vidéo : il
        n'a d'autre choix que de tout télécharger depuis le début. C'est la
        seule raison pour laquelle ce serveur n'est pas trois lignes."""
        size = path.stat().st_size
        start, end = 0, size - 1
        partial = False
        header = self.headers.get("Range", "")
        match = re.match(r"bytes=(\d*)-(\d*)", header)
        if match:
            first, last = match.group(1), match.group(2)
            if first:
                start = min(int(first), size - 1)
                end = min(int(last), size - 1) if last else size - 1
            elif last:  # suffixe : les N derniers octets
                start = max(size - int(last), 0)
            partial = True

        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header(
            "Content-Type", mimetypes.guess_type(path.name)[0] or "video/mp4"
        )
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # l'utilisateur a changé de clip, c'est normal
                remaining -= len(chunk)

    def send_thumb(self, route: str, source: Path) -> None:
        """Sert la miniature d'un clip, en la fabriquant à la première demande.

        Elle est prise un peu après le début, jamais sur la première image :
        une caméra qui vient de se déclencher livre souvent une ou deux images
        noires ou surexposées, le temps que l'exposition s'ajuste. Et on
        l'extrait de la version normalisée quand elle existe, pour que
        l'horodatage incrusté apparaisse dans la vignette."""
        thumb = (self.paths["thumbs"] / route).with_suffix(".jpg")
        fresh = (
            thumb.is_file()
            and thumb.stat().st_size > 0
            and thumb.stat().st_mtime >= source.stat().st_mtime
        )
        if not fresh:
            thumb.parent.mkdir(parents=True, exist_ok=True)
            pending = thumb.with_suffix(".tmp.jpg")
            # Deux extractions à la fois au plus : le navigateur réclame toutes
            # les vignettes de la page d'un coup, et autant de ffmpeg simultanés
            # saturerait la machine pour rien.
            with THUMB_SLOTS:
                runtime.lancer(
                    [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                     # -ss avant -i : ffmpeg saute directement à la position
                     # demandée au lieu de décoder tout ce qui précède.
                     "-ss", "1.5", "-i", str(source), "-frames:v", "1",
                     "-vf", "scale=480:-2", "-q:v", "5", str(pending)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, check=False,
                )
                if not pending.is_file() or pending.stat().st_size == 0:
                    # Clip plus court que la position demandée : on se rabat
                    # sur la toute première image.
                    runtime.lancer(
                        [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                         "-i", str(source), "-frames:v", "1",
                         "-vf", "scale=480:-2", "-q:v", "5", str(pending)],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=False,
                    )
            if not pending.is_file() or pending.stat().st_size == 0:
                pending.unlink(missing_ok=True)
                self.send_error(404)
                return
            pending.replace(thumb)

        body = thumb.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def describe_camera(self, camera_name: str, camera, raw: dict) -> dict:
        """Décrit une caméra, en datant ses mesures.

        Une caméra injoignable continue de renvoyer sa dernière température
        connue. La présenter sans date reviendrait à annoncer comme actuelle
        une valeur qui peut avoir des semaines."""
        info = raw.get(camera_name.strip(), {})
        status = str(info.get("status") or "").strip()
        measured = info.get("updated_at")
        age = None
        if measured:
            try:
                moment = md.parse_created_at(str(measured))
                age = (dt.datetime.now(dt.timezone.utc) - moment).total_seconds()
                local = moment.astimezone(self.timezone)
                # Un relevé du jour se lit mieux à l'heure seule ; au-delà la
                # date devient nécessaire pour juger de sa fraîcheur.
                today = dt.datetime.now(self.timezone).date() == local.date()
                measured = local.strftime("%H:%M" if today else "%d/%m à %H:%M")
            except (ValueError, TypeError):
                measured, age = None, None

        # Les « signals » de l'écran d'accueil sont les jauges que montre
        # l'application Blink. On les transmet telles quelles, sans inventer
        # d'échelle : rien dans l'API ne documente leur maximum, et chez cette
        # installation une caméra qui fonctionne très bien affiche lfr 1.
        signals = info.get("signals") or {}

        # Ne pas utiliser camera.arm : pour un mini d'intérieur, blinkpy
        # renvoie l'armement du module de synchronisation à la place de la
        # détection propre à la caméra (BlinkCameraMini.arm rend sync.arm).
        # Système armé plus détection coupée s'affichait donc « active ».
        # motion_enabled est juste pour tous les modèles, et le champ
        # `enabled` de l'écran d'accueil sert de recoupement.
        enabled = camera.motion_enabled
        if enabled is None:
            enabled = info.get("enabled")

        return {
            "name": camera_name.strip(),
            "armed": bool(enabled),
            "battery": camera.attributes.get("battery"),
            "battery_signal": signals.get("battery"),
            "voltage": camera.attributes.get("battery_voltage"),
            # Blink rapporte des degrés Fahrenheit ; blinkpy expose la
            # conversion, autant l'utiliser plutôt que de la refaire ici.
            "temperature": camera.temperature_c,
            "wifi": camera.attributes.get("wifi_strength"),
            "lfr": signals.get("lfr"),
            "firmware": camera.attributes.get("version"),
            "kind": camera.attributes.get("type"),
            "model": model_name(camera.attributes.get("type")),
            "serial": info.get("serial") or camera.attributes.get("serial"),
            "status": status,
            "offline": status == "offline",
            "measured_at": measured,
            "age_seconds": age,
        }

    def system_state(self) -> dict:
        """État d'armement du système et des caméras.

        L'armement Blink a deux niveaux, et les confondre mène à des surprises :
        le module de synchronisation commande l'ensemble, chaque caméra a en
        plus son propre interrupteur. Une caméra armée dans un système désarmé
        ne détecte rien, d'où l'affichage des deux."""
        # Lu une fois pour toutes les caméras : c'est un fichier, pas une
        # question posée à Blink.
        venues = provenances(read_entries(self.paths))

        def read(blink):
            async def run(_blink=blink):
                await _blink.refresh(force=True)
                # Les attributs de blinkpy ne disent pas *quand* une mesure a
                # été prise. L'écran d'accueil, lui, porte un `status` et un
                # `updated_at` par appareil : sans eux, la température d'une
                # caméra hors de portée s'affiche comme si elle était actuelle
                # alors qu'elle peut dater de plusieurs semaines.
                raw = {}
                home = getattr(_blink, "homescreen", None) or {}
                for group in ("cameras", "owls", "doorbells"):
                    for item in home.get(group) or []:
                        raw[str(item.get("name") or "").strip()] = item

                modules = {str(m.get("name") or "").strip(): m
                           for m in (home.get("sync_modules") or [])}
                systems = []
                for name, sync in _blink.sync.items():
                    module = modules.get(str(sync.name or "").strip()) or {}
                    if not module and modules:
                        module = list(modules.values())[0]
                    systems.append({
                        "name": name.strip(),
                        "armed": bool(sync.arm),
                        "module": MODULE_MODELS.get(module.get("type"))
                                  or (f"module « {module.get('type')} »"
                                      if module.get("type") else None),
                        "module_firmware": module.get("fw_version"),
                        "module_serial": module.get("serial"),
                        "cameras": [
                            dict(self.describe_camera(camera_name, camera, raw),
                                 clips_source=venues.get(camera_name.strip()))
                            for camera_name, camera in sync.cameras.items()
                        ],
                    })
                return {"systems": systems, "passages": runtime.passages()}
            return run()

        state = BLINK.call(read, timeout=60)
        remember_cameras(self.paths, state.get("systems") or [])
        return state

    def set_armed(self, scope: str, name: str, armed: bool) -> None:
        def apply(blink):
            async def run(_blink=blink):
                if scope == "system":
                    for sync_name, sync in _blink.sync.items():
                        if sync_name.strip() == name:
                            await sync.async_arm(armed)
                            return
                    raise RuntimeError(f"Système inconnu : {name}")
                _, camera = BLINK.find_camera(_blink, name)
                await camera.async_arm(armed)
            return run()

        BLINK.call(apply, timeout=60)

    def send_camera_thumb(self, name: str) -> None:
        """Sert la dernière vignette connue d'une caméra.

        Elle remplace le cadre noir avant qu'on lance un direct : on voit
        d'emblée ce que regarde la caméra, y compris pour celles qui sont hors
        de portée et dont le direct échouera. C'est l'image que Blink garde de
        son côté, pas une capture neuve : la demander réveillerait la caméra et
        userait sa batterie à chaque affichage de la page.

        Récupérée une seule fois, puis servie telle quelle : seul « Actualiser »
        la renouvelle."""
        cached = (self.paths["thumbs"] / "cameras" / f"{safe_file(name)}.jpg")
        if not (cached.is_file() and cached.stat().st_size > 0):
            def fetch(blink):
                async def run(_blink=blink):
                    _, camera = BLINK.find_camera(_blink, name)
                    response = await camera.get_media()
                    if response is None or response.status != 200:
                        return b""
                    return await response.read()
                return run()
            try:
                body = BLINK.call(fetch, timeout=45)
            except Exception as error:
                print(f"[vignette] {name} : {type(error).__name__}: {error}", flush=True)
                body = b""
            if not body and not cached.is_file():
                self.send_error(404)
                return
            if body:
                cached.parent.mkdir(parents=True, exist_ok=True)
                pending = cached.with_suffix(".tmp.jpg")
                pending.write_bytes(body)
                pending.replace(cached)

        body = cached.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def live_failure_reason(self, stream) -> str:
        """Demande à Blink pourquoi aucune image n'arrive.

        La réponse de suivi de commande porte un motif lisible (« Live view
        failed » quand la caméra est hors de portée ou endormie). blinkpy le
        journalise mais ne le remonte pas : on interroge donc nous-mêmes."""
        if stream is None:
            return "La caméra n'a envoyé aucune image."
        def ask(blink):
            async def run(_blink=blink):
                from blinkpy import api
                return await api.request_command_status(
                    _blink, stream.camera.network_id, stream.command_id
                )
            return run()
        try:
            status = BLINK.call(ask, timeout=30) or {}
        except Exception:
            return "La caméra n'a envoyé aucune image."

        message = str(status.get("status_msg") or "").strip()
        commands = status.get("commands") or []
        condition = ""
        for command in commands:
            if command.get("id") == stream.command_id:
                condition = str(command.get("state_condition") or "")
        if message.lower() == "live view failed" or condition == "error":
            return ("La caméra n'a pas répondu (Blink signale « "
                    f"{message or condition} »). Hors de portée du module, "
                    "endormie, ou déjà occupée par une autre session.")
        return message or "La caméra n'a envoyé aucune image."

    def send_live(self, name: str) -> None:
        """Diffuse le direct d'une caméra en MJPEG.

        Chaîne complète : Blink livre un flux « immis » que blinkpy sait
        déchiffrer et republier en MPEG-TS sur un port local, ffmpeg le lit et
        le convertit en suite d'images JPEG, que l'on pousse en
        multipart/x-mixed-replace. Une simple balise <img> suffit alors à
        l'afficher, sans lecteur ni bibliothèque.

        Tout le cycle de vie tient à cette requête HTTP : quand le navigateur
        ferme l'onglet, la connexion tombe, on arrête ffmpeg et on rend la
        session à la caméra. Rien à révoquer, rien à oublier de fermer."""
        if not MODULE_SLOT.acquire(blocking=False):
            self.send_error(409, _slot_occupe_message())
            return
        _slot_pris("direct MJPEG", name)

        holder: dict = {}
        try:
            # Verrou sur disque en plus du jeton mémoire : la surveillance est un
            # autre processus, elle ne voit pas nos sémaphores. Sans lui, un
            # téléchargement lancé en arrière-plan tomberait sur « System is
            # busy » pendant qu'on diffuse.
            holder["lock"] = blink_engine.hub_lock("direct")
            holder["lock"].__enter__()
            def start(blink):
                async def run(_blink=blink):
                    sync, camera = BLINK.find_camera(_blink, name)
                    try:
                        stream = await camera.init_livestream()
                    except KeyError as error:
                        # Blink accepte la demande mais ne renvoie aucune
                        # adresse de flux quand la caméra est injoignable.
                        # blinkpy va alors chercher une clé absente ; le
                        # KeyError brut n'apprendrait rien à personne.
                        raise RuntimeError(
                            f"Blink n'a fourni aucune adresse de flux pour "
                            f"« {name} ». La caméra est probablement hors de "
                            f"portée du module de synchronisation, éteinte, ou "
                            f"sa batterie est vide."
                        ) from error
                    except NotImplementedError as error:
                        raise RuntimeError(
                            f"Cette caméra diffuse dans un format non pris en "
                            f"charge ({error})."
                        ) from error
                    await stream.start()
                    holder["stream"] = stream
                    holder["feed"] = asyncio.ensure_future(stream.feed())
                    return stream.url
                return run()

            url = BLINK.call(start, timeout=45)

            print(f"[direct] {name} : flux Blink ouvert sur {url}", flush=True)
            process = runtime.demarrer(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                 # Le flux arrive au fil de l'eau : on ne veut ni analyse
                 # préalable longue ni mise en tampon, sinon l'image affichée
                 # aurait plusieurs secondes de retard. La caméra reste, elle,
                 # le vrai goulot (réveil, négociation côté Blink) : ces deux
                 # bornes ne raccourcissent que la part qui nous revient.
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 "-analyzeduration", "500000", "-probesize", "300000",
                 "-i", url,
                 # La fluidité perçue tenait plus à 10 im/s qu'à la définition :
                 # un flux caméra dépasse rarement ce qu'un cadre de vidéo-
                 # surveillance a besoin d'afficher. Écrêter la largeur avant
                 # d'encoder laisse la place à 15 im/s pour un débit local
                 # comparable ; min(1280,iw) ne réduit jamais un flux déjà
                 # plus étroit.
                 "-vf", "scale='min(1280,iw)':-2",
                 "-f", "mpjpeg", "-q:v", "6", "-r", "15",
                 "-boundary_tag", LIVE_BOUNDARY, "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            holder["process"] = process
            errors: list = []
            holder["drain"] = threading.Thread(
                target=lambda: errors.append(process.stderr.read()), daemon=True
            )
            holder["drain"].start()

            # On attend la première image avant d'annoncer un succès. Envoyer
            # les en-têtes tout de suite condamnerait à ne plus rien pouvoir
            # signaler ensuite : une caméra hors de portée laisserait le
            # navigateur devant un cadre vide sans explication.
            lecteur = LecteurTube(process.stdout)
            first = lecteur.lire(LIVE_FIRST_FRAME_SECONDS)
            if not first:
                reason = self.live_failure_reason(holder.get("stream"))
                drain = holder.get("drain")
                if drain is not None:
                    drain.join(timeout=2)
                trace = (errors[0] or b"").decode("utf-8", "replace").strip()[:300] \
                    if errors else ""
                if trace:
                    reason = f"{reason} | ffmpeg : {trace}"
                raise RuntimeError(reason)

            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={LIVE_BOUNDARY}",
            )
            self.send_header("Cache-Control", "no-store")
            # Indispensable : un corps de longueur inconnue n'est licite en
            # HTTP/1.1 que si la connexion se ferme à la fin. Sans cet en-tête
            # le navigateur ignore où s'arrête la réponse et n'affiche rien,
            # alors que curl, plus tolérant, montre bien les images.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()

            def _ecrire(data: bytes) -> bool:
                # Le premier envoi mérite la même tolérance que les suivants :
                # un onglet déjà fermé quand ce premier bloc part ne doit pas
                # remonter comme un échec (vu en vrai : ConnectionAbortedError
                # sur ce tout premier write, alors que c'est une fin normale).
                try:
                    self.wfile.write(data)
                    return True
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    return False

            if not _ecrire(first):
                holder["sent"] = 0
                return
            sent = len(first)
            # deadline n'était vérifiée qu'entre deux lectures : une lecture
            # individuelle bloquée (ffmpeg qui se tait sans jamais fermer
            # stdout) ne rendait jamais la main, et LIVE_MAX_SECONDS ne
            # bornait donc rien dans ce cas précis (vu en vrai : un direct
            # resté ouvert plus de 600 s, MODULE_SLOT jamais rendu). lire()
            # porte maintenant elle-même un délai réel sur chaque lecture.
            deadline = time.monotonic() + LIVE_MAX_SECONDS
            while time.monotonic() < deadline:
                chunk = lecteur.lire(deadline - time.monotonic())
                if chunk is None:
                    continue  # juste lent : le délai global n'est pas encore écoulé
                if not chunk:
                    break
                if not _ecrire(chunk):
                    break  # onglet fermé : c'est la fin normale d'un direct
                sent += len(chunk)
            holder["sent"] = sent
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError)                 else f"{type(error).__name__}: {error}"
            LAST_LIVE_ERROR.clear()
            LAST_LIVE_ERROR.update({"camera": name, "message": message})
            print(f"[direct] {name} : echec, {message}", flush=True)
            try:
                self.send_error(503, message[:200])
            except Exception:
                pass  # en-tetes deja envoyes : le flux s'arrete, c'est tout
        finally:
            process = holder.get("process")
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            stream = holder.get("stream")
            if stream is not None:
                try:
                    verdict = BLINK.call(
                        lambda _b: _stop_stream(stream, holder.get("feed")),
                        timeout=45,
                    )
                except Exception as error:
                    verdict = f"echec de fermeture, {type(error).__name__}: {error}"
                print(f"[direct] {name} : {verdict}", flush=True)
            sent = holder.get("sent")
            if sent is not None:
                detail = ""
                if not sent:
                    drain = holder.get("drain")
                    if drain is not None:
                        drain.join(timeout=3)
                    detail = " | ffmpeg : " + (
                        (errors[0] or "").strip()[:300] if errors else "rien"
                    )
                print(f"[direct] {name} : termine, {sent} octets transmis{detail}",
                      flush=True)
            verrou = holder.get("lock")
            if verrou is not None:
                verrou.__exit__(None, None, None)
            _slot_rendu()
            MODULE_SLOT.release()

    def send_live_mse(self, name: str) -> None:
        """Diffuse le direct d'une caméra en fMP4 fragmenté, pour MediaSource.

        Face à /live (MJPEG) : au lieu de faire réencoder chaque image en
        JPEG par ffmpeg puis redécoder par un <img>, on ne fait que changer
        de conteneur (Annexe B -> boîtes MP4 fragmentées), sans toucher à
        l'image. Un sous-processus ffmpeg fait le remux, exactement comme
        /live : une première version passait par PyAV en lecture directe du
        flux Blink, plus simple pour lire le codec exact, mais nettement
        moins fiable sur ce flux précis (essais réels sur caméra) que le
        sous-processus ffmpeg qu'utilise /live avec succès depuis le début.
        Le prix : lire le codec exige de parcourir les boîtes du segment
        d'initialisation MP4 (moov > trak > mdia > minf > stbl > stsd >
        avc1 > avcC) plutôt que de le lire via une API."""
        if not MODULE_SLOT.acquire(blocking=False):
            self.send_error(409, _slot_occupe_message())
            return
        _slot_pris("direct MSE", name)

        holder: dict = {}
        try:
            holder["lock"] = blink_engine.hub_lock("direct")
            holder["lock"].__enter__()

            def start(blink):
                async def run(_blink=blink):
                    sync, camera = BLINK.find_camera(_blink, name)
                    try:
                        stream = await camera.init_livestream()
                    except KeyError as error:
                        raise RuntimeError(
                            f"Blink n'a fourni aucune adresse de flux pour "
                            f"« {name} ». La caméra est probablement hors de "
                            f"portée du module de synchronisation, éteinte, ou "
                            f"sa batterie est vide."
                        ) from error
                    except NotImplementedError as error:
                        raise RuntimeError(
                            f"Cette caméra diffuse dans un format non pris en "
                            f"charge ({error})."
                        ) from error
                    await stream.start()
                    holder["stream"] = stream
                    holder["feed"] = asyncio.ensure_future(stream.feed())
                    return stream.url
                return run()

            url = BLINK.call(start, timeout=45)
            print(f"[direct-mse] {name} : flux Blink ouvert sur {url}", flush=True)

            process = runtime.demarrer(
                [self.ffmpeg, "-hide_banner", "-loglevel", "error",
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 # Plus généreux qu'en MJPEG (500000/300000) : un moov figé
                 # (empty_moov) doit connaître les dimensions *avant*
                 # d'écrire son en-tête, alors que le décodage MJPEG les
                 # apprend au fil de l'eau sans jamais avoir à s'engager
                 # d'avance. Vu en vrai sur une caméra plus lente à livrer
                 # son SPS (jardin) : « dimensions not set / Could not
                 # write header » côté ffmpeg, jamais côté MJPEG avec les
                 # mêmes réglages sur la même caméra. Toujours largement
                 # sous LIVE_FIRST_FRAME_SECONDS (40 s) : une caméra qui
                 # répond vite n'attend pas plus longtemps pour autant, ce
                 # ne sont que des plafonds.
                 "-analyzeduration", "5000000", "-probesize", "5000000",
                 "-i", url,
                 # copy : remux sans réencodage, coût CPU quasi nul.
                 "-c:v", "copy", "-an",
                 "-f", "mp4",
                 "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                 "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            holder["process"] = process
            errors: list = []
            holder["drain"] = threading.Thread(
                target=lambda: errors.append(process.stderr.read()), daemon=True
            )
            holder["drain"].start()

            lecteur = LecteurTube(process.stdout)
            first = read_mp4_init_segment(lecteur, LIVE_FIRST_FRAME_SECONDS)
            print(f"[direct-mse] {name} : segment initial {len(first)} octets", flush=True)
            if not first:
                reason = self.live_failure_reason(holder.get("stream"))
                # ffmpeg peut s'être arrêté avant tout octet exploitable (pas
                # forcément une caméra hors de portée) : son message, quand il
                # y en a un, en dit souvent plus que le statut Blink seul. Le
                # fil de lecture n'a que ça à faire depuis le lancement du
                # sous-processus : s'il n'a pas fini, ffmpeg tourne encore.
                drain = holder.get("drain")
                if drain is not None:
                    drain.join(timeout=2)
                trace = (errors[0] or b"").decode("utf-8", "replace").strip()[:300] \
                    if errors else ""
                if trace:
                    reason = f"{reason} | ffmpeg : {trace}"
                raise RuntimeError(reason)
            codec_str = h264_mime_codec_from_moov(first)

            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Codec", codec_str)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            print(f"[direct-mse] {name} : en-tetes envoyes, codec {codec_str}", flush=True)

            def _ecrire(data: bytes) -> bool:
                # Même tolérance sur ce premier envoi que sur les suivants :
                # un onglet déjà fermé quand ce bloc part ne doit pas remonter
                # comme un échec (vu en vrai : ConnectionAbortedError sur ce
                # tout premier write, alors que c'est une fin normale).
                try:
                    self.wfile.write(data)
                    return True
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    return False

            if not _ecrire(first):
                holder["sent"] = 0
                return
            sent = len(first)
            # deadline n'était vérifiée qu'entre deux lectures : une lecture
            # individuelle bloquée (ffmpeg qui se tait sans jamais fermer
            # stdout) ne rendait jamais la main, et LIVE_MAX_SECONDS ne
            # bornait donc rien dans ce cas précis (vu en vrai : un direct
            # resté ouvert plus de 600 s, MODULE_SLOT jamais rendu). lire()
            # porte maintenant elle-même un délai réel sur chaque lecture.
            deadline = time.monotonic() + LIVE_MAX_SECONDS
            while time.monotonic() < deadline:
                chunk = lecteur.lire(deadline - time.monotonic())
                if chunk is None:
                    continue  # juste lent : le délai global n'est pas encore écoulé
                if not chunk:
                    break
                if not _ecrire(chunk):
                    break  # onglet fermé : c'est la fin normale d'un direct
                sent += len(chunk)
            holder["sent"] = sent
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) \
                else f"{type(error).__name__}: {error}"
            LAST_LIVE_ERROR.clear()
            LAST_LIVE_ERROR.update({"camera": name, "message": message})
            print(f"[direct-mse] {name} : echec, {message}", flush=True)
            try:
                self.send_error(503, message[:200])
            except Exception:
                pass  # en-tetes deja envoyes : le flux s'arrete, c'est tout
        finally:
            process = holder.get("process")
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
            stream = holder.get("stream")
            if stream is not None:
                try:
                    verdict = BLINK.call(
                        lambda _b: _stop_stream(stream, holder.get("feed")),
                        timeout=45,
                    )
                except Exception as error:
                    verdict = f"echec de fermeture, {type(error).__name__}: {error}"
                print(f"[direct-mse] {name} : {verdict}", flush=True)
            sent = holder.get("sent")
            if sent is not None:
                detail = ""
                if not sent:
                    drain = holder.get("drain")
                    if drain is not None:
                        drain.join(timeout=3)
                    detail = " | ffmpeg : " + (
                        (errors[0] or "").strip()[:300] if errors else "rien"
                    )
                print(f"[direct-mse] {name} : termine, {sent} octets transmis{detail}",
                      flush=True)
            verrou = holder.get("lock")
            if verrou is not None:
                verrou.__exit__(None, None, None)
            _slot_rendu()
            MODULE_SLOT.release()

    def resolve_media(self, route: str) -> Path | None:
        """Traduit « clip/… » ou « daily/… » en fichier réel, ou rien.

        Deux vérifications indépendantes : la forme du chemin, puis le fait
        qu'il retombe bien sous la racine annoncée une fois résolu. La seconde
        est celle qui compte, elle neutralise les « .. » quelle que soit leur
        écriture."""
        kind, _, relative = route.partition("/")
        if not relative or not IDENTITY.match(relative):
            return None

        if kind == "clip":
            if relative not in known_identities(self.paths):
                return None
            roots = [self.paths[name] for name in ("normalized", "input", "excluded")]
        elif kind in ("daily", "weekly", "monthly"):
            roots = [self.paths[kind]]
        else:
            return None

        for root in roots:
            candidate = (root / relative).resolve()
            if candidate.is_relative_to(root.resolve()) and candidate.is_file():
                return candidate
        return None

    # ------------------------------------------------------------------ routes

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # La page est intégrée au script : elle change dès qu'on modifie
            # serve.py. Sans cette consigne, le navigateur ressert sa copie et
            # l'on croit une modification perdue alors qu'elle est bien là.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route.startswith("/camthumb/"):
            # Pas de contrôle du nom ici : il coûterait un rafraîchissement
            # complet du compte à chaque vignette. C'est find_camera, plus bas,
            # qui refuse un nom inconnu, et safe_file qui assainit le nom de
            # fichier du cache.
            self.send_camera_thumb(unquote(route[len("/camthumb/"):]))
            return

        if route.startswith("/live/"):
            self.send_live(unquote(route[len("/live/"):]))
            return

        if route.startswith("/live-mse/"):
            self.send_live_mse(unquote(route[len("/live-mse/"):]))
            return

        if route == "/api/live-error":
            self.send_json(LAST_LIVE_ERROR or {})
            return

        if route == "/api/system":
            try:
                self.send_json(self.system_state())
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 503)
            return

        if route == "/api/videos":
            self.send_json(collect_videos(self.paths, self.ffmpeg))
            return

        if route == "/api/status":
            self.send_json({"authenticated": (BASE_DIR / "blink_auth.json").is_file()})
            return

        if route == "/api/clips":
            # ?all=1 lève explicitement la fenêtre par défaut : l'historique
            # complet reste à un clic, jamais perdu, seulement pas chargé
            # d'office (voir DEFAULT_WINDOW_DAYS).
            voir_tout = parse_qs(urlparse(self.path).query).get("all", ["0"])[0] == "1"
            depuis = None if voir_tout else (
                dt.datetime.now(self.timezone).date()
                - dt.timedelta(days=DEFAULT_WINDOW_DAYS)
            )
            try:
                self.send_json(collect(self.paths, self.timezone, self.ffmpeg, depuis))
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 500)
            return

        if route.startswith("/thumb/"):
            target = unquote(route[len("/thumb/"):])
            source = self.resolve_media(target)
            if source is None:
                self.send_error(404)
                return
            self.send_thumb(target, source)
            return

        if route.startswith("/media/"):
            path = self.resolve_media(unquote(route[len("/media/"):]))
            if path is None:
                self.send_error(404)
                return
            self.send_media(path)
            return

        if route == "/api/passages":
            # Le nombre de clips au registre accompagne les heures : la page le
            # compare à ce qu'elle affiche et signale l'écart, sans rien
            # redessiner de lui-même.
            # La version publiée est lue dans le cache, jamais chez GitHub : la
            # page ne doit pas attendre un serveur distant. Un fil de fond tient
            # ce cache à jour.
            self.send_json({"passages": runtime.passages(),
                            "clips": len(read_entries(self.paths)),
                            "travail": runtime.travail_en_cours(),
                            "maj": maj.disponible(reseau=False)})
            return

        if route == "/api/refresh":
            self.stream_refresh()
            return

        self.send_error(404)

    # ------------------------------------------------------- actualisation

    def send_event(self, payload: dict) -> bool:
        """Émet un événement SSE. Renvoie False si le navigateur a coupé."""
        body = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(body.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return False
        return True

    def stream_refresh(self) -> None:
        """Enchaîne téléchargement puis fusion, en poussant la sortie au fil de l'eau.

        Server-Sent Events plutôt qu'une réponse unique en fin de traitement :
        c'est le mécanisme du navigateur pour recevoir un flux de messages
        (une simple balise data:, pas de protocole à inventer), et il n'y a
        rien à installer côté page. Le sens du flux est unique, du serveur vers
        la page, ce qui suffit ici : la page n'a rien à répondre.

        On lance daily.py, pas blink2video.py puis merge_daily.py : daily.py *est*
        cet enchaînement, le dupliquer ici téléchargerait et fusionnerait deux
        fois."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        if not self.lock.acquire(blocking=False):
            self.send_event({"line": "Une actualisation est déjà en cours."})
            self.send_event({"done": True, "ok": False})
            return
        # Un calcul lancé par les boucles de fond occupe déjà ffmpeg et le
        # registre. En lancer un second ne ferait qu'attendre le verrou pendant
        # de longues minutes, en donnant l'impression d'un blocage.
        en_cours = runtime.travail_en_cours()
        if en_cours:
            self.send_event({"line": (
                f"{en_cours.get('quoi', 'Un calcul')} est déjà en cours. "
                "Attendez la fin de la barre, puis réessayez."
            )})
            self.send_event({"done": True, "ok": False})
            self.lock.release()
            return
        # Les vignettes de caméra ne sont pas renouvelées ici : « Actualiser »
        # demande les clips, pas une nouvelle photo de chaque caméra. Elles sont
        # prises une fois, à la première ouverture de la vue Direct, et ne
        # changent plus tant qu'on ne les efface pas.
        try:
            # Le téléchargement interroge le Sync Module, comme le direct. Les
            # laisser se chevaucher garantirait un « System is busy » : mieux
            # vaut le dire tout de suite que d'échouer au milieu.
            if not MODULE_SLOT.acquire(blocking=False):
                self.send_event({"line": _slot_occupe_message()})
                self.send_event({"done": True, "ok": False})
                return
            _slot_pris("actualisation")
            try:
                # Même verrou de fichier que la surveillance : le téléchargement
                # lancé ici est exactement celui qu'elle fait de son côté, et
                # elle tourne dans un autre processus qui ne voit pas nos
                # sémaphores.
                with blink_engine.hub_lock("actualisation", stale_after=3600):
                    self.run_refresh()
            except blink_engine.BusyError as error:
                self.send_event({"line": f"Module occupé : {error}."})
                self.send_event({"done": True, "ok": False})
            finally:
                _slot_rendu()
                MODULE_SLOT.release()
        finally:
            self.lock.release()

    def run_refresh(self) -> None:
        """Télécharge puis assemble, en poussant la sortie des deux au fil de l'eau.

        Deux programmes enchaînés ici plutôt qu'un verbe qui les enchaînerait :
        « download » et « merge » sont les deux seules mains de l'outil, et les
        appeler l'un après l'autre dit exactement ce qui se passe."""
        auth = BASE_DIR / "blink_auth.json"
        etapes = [("Téléchargement", runtime.self_command("download", "--hub", self.hub)),
                  ("Fusion", runtime.self_command("merge"))]
        if not auth.is_file():
            # Le téléchargement demanderait l'e-mail, le mot de passe et le code
            # de vérification sur l'entrée standard, qui n'existe pas ici : le
            # processus resterait bloqué. On le dit et on se contente
            # d'assembler ce qui est déjà là.
            self.send_event({"line": (
                f"Session Blink absente ({auth.name}). Lancez « blink2video login » "
                "dans un terminal pour vous connecter. Reconstruction des vidéos seule."
            )})
            etapes = etapes[1:]

        # PYTHONUNBUFFERED : sans lui, la sortie arriverait par blocs de
        # plusieurs kilo-octets et la barre avancerait par à-coups.
        # PYTHONIOENCODING évite les accents mutilés par la console Windows.
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        for phase, command in etapes:
            self.send_event({"phase": phase, "line": f"$ {phase.lower()}"})
            if not self.suivre(command, env, phase):
                return
        self.send_event({"done": True, "ok": True})

    def suivre(self, command: list, env: dict, phase: str) -> bool:
        """Fait avancer la barre au fil des lignes. Faux si on doit s'arrêter."""
        process = runtime.demarrer(
            command, cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        for raw in process.stdout:
            line = raw.rstrip("\n")
            event = {"line": line}
            # Les deux programmes annoncent leur avancement sous la forme
            # « [3/24] ». Une seule règle de lecture suffit, et chaque phase
            # repart naturellement de 1.
            counter = PROGRESS.search(line)
            if counter:
                index, total = int(counter.group(1)), int(counter.group(2))
                inner = INNER.match(line)
                fraction = int(inner.group(1)) / 100 if inner else 0.0
                if inner:
                    event.pop("line")
                event["progress"] = {
                    "done": round(index - 1 + fraction, 3), "total": total
                }
            heading = HEADING.match(line)
            if heading:
                titre = heading.group(1).strip()
                event["phase"] = titre.capitalize() if titre.isupper() else titre
            if not self.send_event(event):
                process.terminate()
                process.wait()
                return False
        process.wait()
        if process.returncode != 0:
            self.send_event({"line": f"{phase} : échec (code {process.returncode})"})
            self.send_event({"done": True, "ok": False})
            return False
        return True

    def do_POST(self):
        route = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json({"error": "corps JSON illisible"}, 400)
            return

        if route == "/api/login":
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))
            if not username or not password:
                self.send_json({"status": "error",
                                "message": "Adresse e-mail et mot de passe requis."})
                return
            self.send_json(self.login_flow.start(username, password))
            return

        if route == "/api/2fa":
            code = str(payload.get("code", "")).strip()
            if not code:
                self.send_json({"status": "error", "message": "Code requis."})
                return
            self.send_json(self.login_flow.submit_code(code))
            return

        if route == "/api/arm":
            name = str(payload.get("name", "")).strip()
            armed = bool(payload.get("armed"))
            scope = str(payload.get("scope", "camera"))
            try:
                self.set_armed(scope, name, armed)
                self.send_json(self.system_state())
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 503)
            return

        if route == "/api/update":
            neuve = maj.disponible(reseau=False)
            if not neuve:
                self.send_json({"error": "Aucune version plus récente."}, 409)
                return
            # Détaché, et volontairement sans attendre : ce processus fait
            # partie de ce que la mise à jour va arrêter. Elle rend compte dans
            # maj.log, et la page attend simplement le retour du serveur.
            runtime.demarrer(
                runtime.self_command("update"), cwd=str(runtime.app_dir()),
                stdin=subprocess.DEVNULL,
                stdout=(runtime.app_dir() / "maj.log").open("ab"),
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"))
            self.send_json({"ok": True, "version": neuve["version"]})
            return

        if route == "/api/toggle":
            identity = str(payload.get("identity", ""))
            excluded = bool(payload.get("excluded"))
            if not IDENTITY.match(identity):
                self.send_json({"error": "identifiant invalide"}, 400)
                return
            # Une seule décision à la fois : le registre est un fichier, deux
            # écritures concurrentes en perdraient une.
            with REGISTRE:
                try:
                    md.set_excluded(
                        self.paths["input"], self.paths["normalized"],
                        self.paths["excluded"], [str(self.paths["input"] / identity)],
                        excluded,
                    )
                except RuntimeError as error:
                    self.send_json({"error": str(error)}, 500)
                    return
            # Écarter un clip change la liste des segments d'une journée, donc
            # son empreinte : sans reconstruction, la journalière, la semaine et
            # le mois continuent de le montrer jusqu'au prochain assemblage. La
            # ligne de commande enchaîne déjà ; l'interface le fait maintenant
            # aussi, en tâche de fond pour que le clic reste immédiat.
            self.reassembler(identity)
            self.send_json({"ok": True})
            return

        self.send_error(404)

    def reassembler(self, identity: str) -> None:
        """Reconstruit, en arrière-plan, les vidéos de la journée touchée.

        Ciblé sur une caméra et un jour : les agrégats de la semaine et du mois
        suivent, et rien d'autre n'est réencodé, l'assemblage n'étant qu'une
        copie de flux."""
        entree = (read_entries(self.paths).get(identity)
                  or next((e for e in read_entries(self.paths).values()
                           if e.get("path") == identity), None))
        camera = str((entree or {}).get("camera") or "").strip()
        try:
            jour = md.parse_created_at(str((entree or {}).get("created_at")))                      .astimezone(self.timezone).date().isoformat()
        except (TypeError, ValueError):
            jour = None
        if not camera or not jour:
            return

        def travailler():
            with REASSEMBLAGE:
                runtime.lancer(
                    runtime.self_command("merge", "--camera", camera, "--date", jour),
                    cwd=str(runtime.app_dir()), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )

        threading.Thread(target=travailler, daemon=True).start()


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>blink2video</title>
<style>
  :root { color-scheme: dark; --bg:#16181d; --card:#1e2128; --line:#2c313b;
          --text:#e6e8ec; --dim:#9aa2b1; --out:#e0574a; --in:#4aa96c; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 system-ui, "Segoe UI", sans-serif; }
  header { position:sticky; top:0; z-index:10; background:var(--bg);
           border-bottom:1px solid var(--line); padding:14px 20px;
           display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:17px; margin:0 10px 0 0; font-weight:600; }
  h1 .v { font-size:11px; font-weight:400; color:var(--dim); vertical-align:super; margin-left:3px; }
  select, button { font:inherit; color:var(--text); background:var(--card);
                   border:1px solid var(--line); border-radius:7px;
                   padding:7px 12px; cursor:pointer; }
  button:hover, select:hover { border-color:#4b5262; }
  button.primary { background:#3a5a86; border-color:#48699a; }
  label { color:var(--dim); display:flex; align-items:center; gap:7px; cursor:pointer; }
  /* Un display explicite l'emporte sur l'attribut hidden : sans cette règle,
     « voir les écartés » restait affiché dans le Direct et les vidéos
     assemblées, où il ne veut rien dire. */
  [hidden] { display:none !important; }
  .count { color:var(--dim); margin-left:auto; font-variant-numeric:tabular-nums; }
  /* Le groupe « mise à jour » : encadré discret pour qu'on voie d'un coup que
     l'heure, la coche et le bouton parlent de la même chose. */
  .maj { display:flex; align-items:center; gap:10px; padding:5px 5px 5px 12px;
         border:1px solid var(--line); border-radius:9px; background:var(--card); }
  .maj #passages { font-variant-numeric:tabular-nums; }
  /* Une version qui attend se remarque sans crier : la couleur suffit. */
  #update { background:#2f4a33; border-color:var(--in); color:#a8e6c0; }
  main { padding:20px; }
  h2 { font-size:14px; color:var(--dim); font-weight:600; margin:28px 0 12px;
       border-bottom:1px solid var(--line); padding-bottom:7px; }
  h2:first-child { margin-top:0; }
  .grid { display:grid; gap:16px;
          grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); }
  .grid.wide { grid-template-columns:repeat(auto-fill, minmax(460px, 1fr)); }
  a.act { text-decoration:none; border:1px solid var(--line); background:var(--bg);
          color:var(--dim); }
  a.act:hover { border-color:#4b5262; color:var(--text); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          overflow:hidden; display:flex; flex-direction:column; }
  .card.out { opacity:.55; border-color:var(--out); }
  video { width:100%; aspect-ratio:16/9; background:#000; display:block; }
  .meta { display:flex; align-items:center; gap:10px; padding:10px 12px; }
  .time { font-variant-numeric:tabular-nums; font-weight:600; }
  /* Tout sur une ligne : en faisant défiler, le titre de jour sort de l'écran
     et l'on ne sait plus de quand datent les clips affichés. */
  .time.line { font-weight:500; font-size:13.5px; line-height:1.35; }
  .sub { color:var(--dim); font-size:13px; }
  .sub.tiny { font-size:12px; opacity:.65; }
  .act { margin-left:auto; padding:6px 12px; border-radius:6px; }
  .act.out { background:#4a2320; border-color:var(--out); color:#ffb3ab; }
  .act.in  { background:#1e3d2b; border-color:var(--in); color:#a8e6c0; }
  #log { white-space:pre-wrap; font:13px/1.45 ui-monospace, Consolas, monospace;
         background:#101216; border:1px solid var(--line); border-radius:8px;
         padding:14px; margin-top:20px; color:var(--dim); display:none;
         max-height:340px; overflow-y:auto; }
  .empty { color:var(--dim); padding:40px 0; text-align:center; }
  .window { color:var(--dim); font-size:13px; margin:0 0 16px; }
  .window a { color:inherit; }
  #work { display:none; width:100%; align-items:center; gap:12px; padding-top:4px; }
  #work.on { display:flex; }
  progress { flex:1; height:8px; border:0; border-radius:4px;
             background:var(--card); appearance:none; }
  progress::-webkit-progress-bar { background:var(--card); border-radius:4px; }
  progress::-webkit-progress-value { background:#4d8ee0; border-radius:4px; }
  progress::-moz-progress-bar { background:#4d8ee0; border-radius:4px; }
  #phase { color:var(--dim); font-size:13px; white-space:nowrap; }
  h2 { display:flex; align-items:center; gap:12px; }
  h2 .act { margin-left:auto; font-size:13px; }
  .live { position:relative; aspect-ratio:16/9; background:#000; display:flex;
          align-items:center; justify-content:center; }
  .live img, .live video { width:100%; height:100%; object-fit:contain; }
  /* La vignette reste en fond, le bouton se pose dessus. */
  .live img.still { position:absolute; inset:0; opacity:.55; }
  .live .watch { position:relative; }
  .watch { border-radius:7px; padding:8px 14px; }
  .watch.stop { position:absolute; right:10px; bottom:10px; opacity:.85; }
  .live { flex-direction:column; gap:12px; }
  .live .hint { color:var(--dim); font-size:14px; margin:0;
                text-align:center; padding:0 20px; line-height:1.4; }
  /* Sans décalage, l'astuce se centre au même endroit que le bouton
     « Réessayer » (seul enfant en flux dans .live) et capte ses clics :
     on la remonte au-dessus et on la rend transparente aux événements. */
  .live .hint.overlay { position:absolute; bottom:56px; left:0; right:0;
                         pointer-events:none; }
  dialog { background:var(--card); color:var(--text); border:1px solid var(--line);
           border-radius:12px; padding:24px; width:min(380px, 92vw); }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  dialog h3 { margin:0 0 6px; font-size:16px; }
  dialog p { margin:0 0 18px; color:var(--dim); font-size:13px; }
  dialog input { width:100%; font:inherit; color:var(--text); background:var(--bg);
                 border:1px solid var(--line); border-radius:7px;
                 padding:9px 11px; margin-bottom:12px; }
  .champMdp { display:flex; gap:8px; margin-bottom:12px; }
  .champMdp input { margin-bottom:0; }
  .champMdp button { flex:none; padding:0 12px; }
  dialog .row { display:flex; gap:10px; justify-content:flex-end; margin-top:6px; }
  #authError { color:var(--out); font-size:13px; min-height:18px; margin:0 0 6px; }
</style>
</head>
<body>
<header>
  <h1>blink2video<span class="v">__VERSION__</span></h1>
  <select id="view">
    <option value="live">Direct</option>
    <option value="clips">Clips</option>
    <option value="daily">Journalières</option>
    <option value="weekly">Hebdomadaires</option>
    <option value="monthly">Mensuelles</option>
  </select>
  <select id="camera"></select>
  <select id="day"></select>
  <label id="outLabel"><input type="checkbox" id="showOut"> voir les écartés</label>
  <span class="count" id="count"></span>
  <!-- Tout ce qui concerne la mise à jour tient ensemble, à droite : l'heure du
       dernier passage, la coche qui recharge seule, et le bouton. -->
  <div class="maj">
    <button id="update" hidden></button>
    <span class="sub tiny" id="passages"></span>
    <label id="autoLabel" title="Recharger la liste dès que des clips arrivent">
      <input type="checkbox" id="auto"> auto
    </label>
    <button class="primary" id="refresh">Actualiser</button>
  </div>
  <div id="work"><span id="phase"></span><progress id="bar"></progress></div>
</header>
<main><div id="list"></div><pre id="log"></pre></main>

<dialog id="auth">
  <h3 id="authTitle">Connexion Blink</h3>
  <p id="authHint">Le mot de passe sert uniquement à ouvrir la session ; seuls
     les jetons sont enregistrés, jamais le mot de passe.</p>
  <p id="authError"></p>
  <div id="authCreds">
    <input id="user" type="email" placeholder="Adresse e-mail" autocomplete="username">
    <div class="champMdp">
      <input id="pass" type="password" placeholder="Mot de passe"
             autocomplete="current-password">
      <button type="button" id="passToggle" aria-label="Afficher le mot de passe">Afficher</button>
    </div>
  </div>
  <div id="authCode" hidden>
    <input id="code" inputmode="numeric" placeholder="Code reçu par SMS ou e-mail"
           autocomplete="one-time-code">
  </div>
  <div class="row">
    <button id="authCancel">Annuler</button>
    <button class="primary" id="authOk">Se connecter</button>
  </div>
</dialog>
<script>
let data = { clips: [], cameras: [], days: [] };
let videos = { daily: [], weekly: [], monthly: [] };
const $ = (id) => document.getElementById(id);

function fill(select, values, all, label) {
  const kept = select.value;
  select.innerHTML = `<option value="">${all}</option>` +
    values.map((v) => `<option value="${v}">${label ? label(v) : v}</option>`).join("");
  if (values.includes(kept)) select.value = kept;
}

function visible() {
  return data.clips.filter((c) =>
    (!$("camera").value || c.camera === $("camera").value) &&
    (!$("day").value || c.day === $("day").value) &&
    ($("showOut").checked || !c.excluded));
}

function duration(seconds) {
  const s = Math.round(seconds);
  const parts = [Math.floor(s / 3600), Math.floor(s / 60) % 60, s % 60];
  return (parts[0] ? parts : parts.slice(1))
    .map((v, i) => (i ? String(v).padStart(2, "0") : v)).join(":");
}

function render() {
  heuresDePassage();
  const kind = $("view").value;
  const clips = kind === "clips";
  $("day").hidden = !clips;
  $("outLabel").hidden = !clips;
  $("camera").hidden = kind === "live";
  if (kind === "live") return renderLive();
  return clips ? renderClips() : renderVideos(kind);
}

// --- direct et armement ----------------------------------------------------
let system = null;

async function loadSystem(force) {
  if (system && !force) return renderLive();
  $("list").innerHTML = `<p class="empty">Interrogation du système Blink…</p>`;
  $("count").textContent = "";
  try {
    system = await (await fetch("/api/system")).json();
  } catch (error) {
    system = { error: String(error) };
  }
  renderLive();
}

// Un calcul lancé par les boucles de fond, hors de cette page : le
// téléchargement et l'assemblage publient leur avancement dans un fichier, seul
// moyen pour la page d'apprendre que la machine travaille. Tant qu'il tourne,
// le bouton reste inactif : un second calcul attendrait le même verrou, sans
// rien avancer.
let travailEnCours = false;
let actualisationLocale = false;

function montrerTravail(travail) {
  if (actualisationLocale) return;    // notre propre barre parle déjà
  const actif = !!(travail && travail.quoi);
  if (!actif) {
    if (travailEnCours) { $("work").classList.remove("on"); load(); }
    travailEnCours = false;
    $("refresh").disabled = false;
    return;
  }
  travailEnCours = true;
  $("refresh").disabled = true;
  $("work").classList.add("on");
  const total = travail.total || 0;
  const fait = travail.fait || 0;
  if (total) {
    $("bar").max = total;
    $("bar").value = fait;
    $("phase").textContent =
      `${travail.quoi} ${Math.min(fait + 1, total)}/${total}`;
  } else {
    $("bar").removeAttribute("value");
    $("phase").textContent = travail.quoi;
  }
}

// Une version publiée plus récente que celle qui tourne : le bouton apparaît,
// et il fait tout, du téléchargement à la relance. Pendant l'opération le
// serveur s'arrête et revient : la page attend son retour, puis se recharge.
function montrerMaj(neuve) {
  const bouton = $("update");
  bouton.hidden = !(neuve && neuve.version);
  if (bouton.hidden || bouton.dataset.encours) return;
  bouton.textContent = `Installer ${neuve.version}`;
  bouton.title = `Version ${neuve.version} publiée. Le téléchargement, `
               + `l'arrêt et la relance sont automatiques.`;
}

$("update").onclick = async () => {
  const bouton = $("update");
  bouton.dataset.encours = "1";
  bouton.disabled = true;
  bouton.textContent = "Mise à jour…";
  const reponse = await fetch("/api/update", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  const resultat = await reponse.json();
  if (resultat.error) {
    alert(resultat.error);
    bouton.disabled = false;
    delete bouton.dataset.encours;
    return;
  }
  $("phase").textContent =
    `Mise à jour vers ${resultat.version} : téléchargement, puis relance…`;
  $("bar").removeAttribute("value");
  $("work").classList.add("on");
  $("refresh").disabled = true;
  // Le serveur va disparaître puis revenir sous sa nouvelle version. On teste
  // sa présence, et c'est son retour qui sert de fin de course.
  let parti = false;
  const attente = setInterval(async () => {
    try {
      await fetch("/api/status", { cache: "no-store" });
      if (parti) location.reload();
    } catch (erreur) {
      parti = true;      // il s'est arrêté : la relance suit
    }
  }, 2000);
  setTimeout(() => clearInterval(attente), 900000);
};

async function heuresDePassage() {
  let etat = {};
  try {
    etat = await (await fetch("/api/passages")).json();
  } catch (erreur) { return; }
  montrerTravail(etat.travail);
  montrerMaj(etat.maj);
  const vus = etat.passages || {};
  // Des clips sont arrivés depuis que la page a été chargée : on le dit, et
  // c'est à vous de cliquer sur Actualiser. La liste ne se réorganise pas sous
  // les yeux de qui est en train de la lire.
  const arrives = (data && data.clips)
    ? Math.max(0, (etat.clips || 0) - data.clips.length) : 0;
  // Une seule heure, la plus récente des trois. Le détail du verbe le plus en
  // retard alourdissait la ligne pour un cas rare.
  const dates = ["watch", "download", "merge"].filter((cle) => vus[cle]);
  if (!dates.length) return;

  const instant = (cle) => new Date(vus[cle]).getTime();
  const plusRecent = dates.reduce((a, b) => (instant(a) > instant(b) ? a : b));
  // Choix mémorisé d'un affichage à l'autre : une préférence qu'il faudrait
  // recocher à chaque ouverture n'en serait pas une. Pendant un calcul, on ne
  // recharge pas : la liste changerait sous les yeux à chaque vidéo assemblée.
  if (arrives && $("auto").checked && !travailEnCours) {
    load();
    return;
  }
  const nouveaux = arrives
    ? ` · ${arrives} nouveau${arrives > 1 ? "x" : ""} clip${arrives > 1 ? "s" : ""}, cliquez sur Actualiser`
    : "";
  $("passages").textContent =
    `actualisé ${vus[plusRecent].slice(11, 16)}` + nouveaux;
}

function renderLive() {
  // Reconstruire la grille remplace tout son HTML, direct en cours compris :
  // la balise <video> et son AbortController survivraient, orphelins, sous
  // un DOM tout neuf qui ne les référence plus (vu en vrai : un clip qui
  // arrive en tâche de fond suffit à déclencher ce rafraîchissement pendant
  // qu'un direct tourne, qui semble alors s'arrêter sans jamais reprendre).
  // Un direct actif gèle donc la grille jusqu'à ce qu'il s'arrête.
  if (Object.keys(MSE_ABORT).length) return;
  if (!system) return loadSystem(false);
  if (system.error) {
    $("list").innerHTML = `<p class="empty">${system.error}</p>`;
    return;
  }
  const cameras = system.systems.reduce((n, s) => n + s.cameras.length, 0);
  const armed = system.systems.reduce(
    (n, s) => n + s.cameras.filter((c) => c.armed).length, 0);
  $("count").textContent = `${cameras} caméra(s) · ${armed} armée(s)`;

  $("list").innerHTML = system.systems.map((s) => `
    <h2>
      ${s.name}
      <span class="sub tiny">${[s.module,
        s.module_firmware ? "micrologiciel " + s.module_firmware : null,
        s.module_serial].filter(Boolean).join(" · ")}</span>
      <button class="act ${s.armed ? "in" : "out"}"
              onclick="setArmed('system', '${s.name}', ${!s.armed})">
        ${s.armed ? "Système armé" : "Système désarmé"}
      </button>
    </h2>
    <div class="grid wide">${s.cameras.map((c) => cameraCard(c, s.armed)).join("")}</div>
  `).join("");
}

function cameraCard(c, systemArmed) {
  // Une mesure vieille de plus d'une heure est datée, et une caméra hors
  // ligne est signalée comme telle : sa dernière température connue peut
  // remonter à plusieurs semaines.
  const vieille = c.age_seconds !== null && c.age_seconds > 3600;
  const num = (v) => v !== null && v !== undefined;
  const releve = [
    c.battery ? `batterie ${c.battery}${num(c.battery_signal) ? ` (${c.battery_signal})` : ""}` : null,
    num(c.temperature) ? `${c.temperature.toFixed(1).replace(".", ",")} °C` : null,
    num(c.wifi) ? `Wi-Fi ${c.wifi} dBm` : null,
    num(c.lfr) ? `liaison module ${c.lfr}` : null,
  ].filter(Boolean).join(" · ");
  const date = c.measured_at
    ? (c.measured_at.includes("à") ? `relevé du ${c.measured_at}`
                                   : `relevé à ${c.measured_at}`)
    : null;
  const details = [
    c.offline ? "HORS LIGNE" : null,
    releve || null,
    date,
    c.armed && !systemArmed ? "sans effet, système désarmé" : null,
  ].filter(Boolean).join(" · ");
  return `<div class="card ${c.offline ? "out" : ""}">
    <div class="live" id="live-${cssId(c.name)}">${repos(c.name, "Voir en direct")}</div>
    <div class="meta">
      <div>
        <div class="time">${c.name}</div>
        <div class="sub">${details || "—"}</div>
        <div class="sub tiny">${[c.model,
          c.firmware ? "micrologiciel " + c.firmware : null, c.serial,
          c.clips_source ? "clips : " + c.clips_source : "aucun clip récupéré",
        ].filter(Boolean).join(" · ")}</div>
      </div>
      <button class="act ${c.armed ? "in" : "out"}"
              onclick="setArmed('camera', '${c.name}', ${!c.armed})">
        ${c.armed ? "Détection active" : "Détection coupée"}
      </button>
    </div>
  </div>`;
}

const cssId = (name) => name.replace(/[^\\w-]/g, "_");

function watch(name) {
  const box = $("live-" + cssId(name));
  // La balise <img> tient le flux : la retirer ferme la connexion, ce qui
  // rend la caméra au bout de la chaîne. Rien d'autre à arrêter.
  box.innerHTML =
    `<img src="/live/${encodeURIComponent(name)}" alt="direct ${name}">
     <p class="hint overlay" id="hint-${cssId(name)}">Réveil de la caméra…</p>
     <button class="watch stop" onclick="stopWatch('${name}')">Arrêter</button>`;

  // La première image met une dizaine de secondes : la caméra doit se
  // réveiller, ouvrir sa session, puis ffmpeg doit identifier le flux. Sans
  // ce message l'attente ressemble exactement à une panne, cadre noir compris.
  // L'événement « load » ne convient pas ici : sur un flux multipart il ne se
  // déclenche qu'à la fin, donc on surveille l'arrivée du premier pixel.
  const img = box.querySelector("img");
  const hint = $("hint-" + cssId(name));
  const started = Date.now();
  const timer = setInterval(() => {
    if (!document.body.contains(img)) return clearInterval(timer);
    if (img.naturalWidth > 0) {
      hint.remove();
      clearInterval(timer);
    } else if (Date.now() - started > 75000) {
      clearInterval(timer);
      failWatch(name, "Aucune image reçue. La caméra n'a pas répondu.");
    } else {
      const s = Math.round((Date.now() - started) / 1000);
      hint.textContent = s > 20
        ? `Réveil de la caméra… ${s} s (une caméra sur batterie est plus lente)`
        : `Réveil de la caméra… ${s} s`;
    }
  }, 500);
  img.onerror = async () => {
    clearInterval(timer);
    let message = "Le flux a été refusé par le serveur.";
    try {
      const info = await (await fetch("/api/live-error")).json();
      if (info.camera === name && info.message) message = info.message;
      else message = "Flux refusé. Un direct précédent finit peut-être de se "
                   + "fermer : réessayez dans quelques secondes.";
    } catch (error) { /* on garde le message générique */ }
    failWatch(name, message);
  };
}

// Un direct qui échoue doit rendre son bouton d'origine : laisser « Arrêter »
// laisserait croire qu'un flux tourne, et il n'y aurait plus aucun moyen de
// relancer. Retirer la balise <img> ferme au passage la connexion restée
// ouverte côté serveur.
function failWatch(name, message) {
  const box = $("live-" + cssId(name));
  box.innerHTML = repos(name, "Réessayer") + `<p class="hint overlay">${message}</p>`;
}

function stopWatch(name) {
  // La case peut avoir disparu sous nos pieds (actualisation de la vue
  // pendant le direct) : la remise au repos est cosmétique, mais couper les
  // flux ci-dessous ne doit jamais en dépendre.
  const box = $("live-" + cssId(name));
  if (box) box.innerHTML = repos(name, "Voir en direct");
  const controller = MSE_ABORT[name];
  if (controller) { controller.abort(); delete MSE_ABORT[name]; }
}

// --- MSE/fMP4 : remux sans réencodage, <video> décodé par le navigateur ---
// Contrairement à <img>, un fetch() ne s'arrête pas tout seul quand on jette
// la balise : il faut son propre AbortController, gardé ici par caméra pour
// que stopWatch() puisse le couper.
const MSE_ABORT = {};
// Blink referme parfois la session en cours de route, sans rapport avec ce
// projet (vu en vrai : entre quelques images et ~1 Mo transmis, puis la
// connexion vers son relais s'interrompt en plein paquet - cause identifiée
// côté blinkpy, voir blink_engine.py). Une reprise manuelle marche presque
// toujours : on l'automatise. Le compteur d'échecs ne grimpe que sur une
// reprise qui n'aura livré aucune image ; dès qu'une image arrive, il
// retombe à zéro, pour ne pas abandonner un direct qui fonctionne juste par
// à-coups. MSE_BUDGET_TOTAL_MS borne quand même la durée totale : un onglet
// oublié ouvert ne doit pas relancer la caméra indéfiniment. Le délai entre
// deux tentatives n'est pas cosmétique : Blink n'accepte qu'une seule
// session de direct par compte à la fois et met du temps à libérer la
// précédente côté serveur ; une reprise trop rapide se heurte à cette
// session pas encore relâchée, pas à un vrai problème.
const MSE_MAX_ECHECS_A_VIDE = 5;
const MSE_DELAI_RECONNEXION_MS = 3000;
const MSE_BUDGET_TOTAL_MS = 10 * 60 * 1000;

function attendreOuAbandon(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException("Aborted", "AbortError")); return; }
    const id = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(id);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });
}

// Un cycle connexion -> flux -> fin. Renvoie si au moins une image est
// arrivée (utilisé par watchMse pour décider de réessayer ou d'abandonner).
async function connecterMse(name, video, signal, texteAttente, t0) {
  const box = $("live-" + cssId(name));
  if (box && !$("hint-" + cssId(name))) {
    box.insertAdjacentHTML(
      "beforeend",
      `<p class="hint overlay" id="hint-${cssId(name)}">${texteAttente}</p>`
    );
  }
  const hint = $("hint-" + cssId(name));

  const mediaSource = new MediaSource();
  const url = URL.createObjectURL(mediaSource);
  video.src = url;
  let recu = false;
  try {
    await new Promise((resolve, reject) => {
      mediaSource.addEventListener("sourceopen", async () => {
        try {
          const response = await fetch(`/live-mse/${encodeURIComponent(name)}`,
                                        { signal });
          if (!response.ok) {
            let message = `Le flux a été refusé par le serveur (${response.status}).`;
            try {
              const info = await (await fetch("/api/live-error")).json();
              if (info.camera === name && info.message) message = info.message;
            } catch (error) { /* on garde le message générique */ }
            throw new Error(message);
          }
          const codec = response.headers.get("X-Codec") || "avc1.42E01E";
          const mimeType = `video/mp4; codecs="${codec}"`;
          if (!MediaSource.isTypeSupported(mimeType)) {
            throw new Error(`Codec non supporté par ce navigateur : ${codec}`);
          }
          const sourceBuffer = mediaSource.addSourceBuffer(mimeType);
          sourceBuffer.mode = "sequence";
          const reader = response.body.getReader();
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            await new Promise((res, rej) => {
              sourceBuffer.addEventListener("updateend", res, { once: true });
              sourceBuffer.addEventListener("error", rej, { once: true });
              sourceBuffer.appendBuffer(value);
            });
            if (!recu) {
              recu = true;
              if (hint) hint.remove();
              if (window.__mseMetric == null) window.__mseMetric = performance.now() - t0;
              // L'attribut autoplay seul ne suffit pas toujours à démarrer
              // la lecture sur une balise <video> dont on change juste le
              // src : on le force dès qu'assez de données sont arrivées.
              video.play().catch(() => {});
            }
          }
          if (mediaSource.readyState === "open") mediaSource.endOfStream();
          resolve();
        } catch (error) {
          reject(error);
        }
      }, { once: true });
    });
  } finally {
    URL.revokeObjectURL(url);
  }
  return recu;
}

async function watchMse(name) {
  const box = $("live-" + cssId(name));
  box.innerHTML =
    `<video autoplay muted playsinline></video>
     <button class="watch stop" onclick="stopWatch('${name}')">Arrêter</button>`;
  const video = box.querySelector("video");
  const t0 = performance.now();
  window.__mseMetric = null;

  const controller = new AbortController();
  MSE_ABORT[name] = controller;

  let echecsAVide = 0;
  let derniereErreur = null;
  while (echecsAVide < MSE_MAX_ECHECS_A_VIDE
         && performance.now() - t0 < MSE_BUDGET_TOTAL_MS) {
    const texte = echecsAVide === 0 && derniereErreur === null
      ? "Réveil de la caméra… (MSE)" : "Reconnexion…";
    try {
      const recu = await connecterMse(name, video, controller.signal, texte, t0);
      derniereErreur = null;
      echecsAVide = recu ? 0 : echecsAVide + 1;
    } catch (error) {
      if (error.name === "AbortError") { delete MSE_ABORT[name]; return; }
      derniereErreur = error;
      echecsAVide++;
    }
    if (controller.signal.aborted || echecsAVide >= MSE_MAX_ECHECS_A_VIDE) break;
    try {
      await attendreOuAbandon(MSE_DELAI_RECONNEXION_MS, controller.signal);
    } catch (error) {
      break;  // arrêt demandé pendant l'attente
    }
  }
  delete MSE_ABORT[name];
  if (controller.signal.aborted) return;
  if (derniereErreur) {
    failWatch(name, String(derniereErreur.message || derniereErreur));
  } else {
    // Budget total écoulé pendant que ça fonctionnait : pas un échec, on
    // ramène juste au repos plutôt que d'afficher une erreur trompeuse.
    stopWatch(name);
  }
}

// L'état de repos d'un cadre : la dernière image connue de la caméra, et le
// bouton par-dessus. Arrêter un direct ramène ici, donc la vignette revient au
// lieu de laisser un rectangle noir jusqu'au rechargement de la page.
function repos(name, libelle) {
  return `<img class="still" src="/camthumb/${encodeURIComponent(name)}" alt="">
     <button class="watch" onclick="watchMse('${name}')">${libelle}</button>`;
}

async function setArmed(scope, name, armed) {
  $("count").textContent = "Envoi de la commande…";
  const answer = await fetch("/api/arm", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, name, armed }),
  });
  const result = await answer.json();
  if (result.error) { alert(result.error); return loadSystem(true); }
  system = result;
  renderLive();
}

function renderClips() {
  const clips = visible();
  // Pas de décompte ici : les clips sont sous les yeux, et trois nombres de
  // plus en haut de page ne disent rien qu'on cherchait.
  $("count").textContent = "";

  if (!clips.length) {
    // Distinguer « rien ne correspond au filtre » de « rien n'a jamais été
    // récupéré » : dans le second cas, la cause la plus fréquente est l'absence
    // de clé USB sur le module, les enregistrements partant alors dans le cloud
    // de l'abonnement Blink, que cet outil ne lit pas. « Lancez blink2video
    // download » n'a plus sa place ici : avec la composition par défaut
    // (start), le téléchargement tourne déjà en arrière-plan, et le dire
    // dessus n'aidait qu'à confondre un premier utilisateur pile au moment
    // où la page est encore vide (vu en vrai, essai à froid).
    $("list").innerHTML = data.clips.length
      ? `<p class="empty">Aucun clip ne correspond à ce filtre.</p>`
      : `<p class="empty">Aucun clip récupéré pour l'instant.<br>
           Le téléchargement tourne déjà en arrière-plan (clé USB toutes les
           10 min, cloud toutes les minutes) : les clips apparaîtront ici
           sans rien faire. Vérifiez qu'une clé USB est branchée sur le
           module : sans elle, les enregistrements ne vont que dans le cloud
           de l'abonnement Blink, que cet outil ne lit pas.</p>`;
    return;
  }
  const days = [...new Set(clips.map((c) => c.day))];
  const fenetre = data.window_days
    ? `<p class="window">${data.window_days} derniers jours affichés
         (${data.clips.length} sur ${data.total_known} clip(s) connus) ·
         <a href="#" onclick="afficherTout(); return false;">afficher tout l'historique</a></p>`
    : "";
  $("list").innerHTML = fenetre + days.map((day) => `
    <h2>${day}</h2>
    <div class="grid">${clips.filter((c) => c.day === day).map(card).join("")}</div>
  `).join("");
}

function renderVideos(kind) {
  const items = (videos[kind] || [])
    .filter((v) => !$("camera").value || v.camera === $("camera").value);
  const total = items.reduce((sum, v) => sum + v.duration, 0);
  $("count").textContent = items.length
    ? `${items.length} vidéo(s) · ${duration(total)} au total`
    : "";
  if (!items.length) {
    $("list").innerHTML =
      `<p class="empty">Aucune vidéo assemblée. Lancez une actualisation.</p>`;
    return;
  }
  const cameras = [...new Set(items.map((v) => v.camera))];
  $("list").innerHTML = cameras.map((camera) => `
    <h2>${camera}</h2>
    <div class="grid wide">
      ${items.filter((v) => v.camera === camera).map(videoCard).join("")}
    </div>
  `).join("");
}

function videoCard(v) {
  const url = `${v.kind}/${encodeURI(v.path)}`;
  return `<div class="card">
    <video preload="none" controls playsinline
           poster="/thumb/${url}" src="/media/${url}"></video>
    <div class="meta">
      <div>
        <div class="time">${v.label}</div>
        <div class="sub">${duration(v.duration)}</div>
      </div>
      <a class="act" href="/media/${url}" download>Télécharger</a>
    </div>
  </div>`;
}

function card(c) {
  const [an, mois, jour] = c.day.split("-");
  const ligne = [c.camera, duration(c.duration), `${jour}/${mois}/${an}`, c.time,
                 c.model].filter(Boolean).join(" · ");
  return `<div class="card ${c.excluded ? "out" : ""}">
    <video preload="none" controls playsinline
           poster="/thumb/clip/${encodeURI(c.identity)}"
           src="/media/clip/${encodeURI(c.identity)}"></video>
    <div class="meta">
      <div class="time line">${ligne}</div>
      <button class="act ${c.excluded ? "in" : "out"}"
              onclick="toggle('${c.identity}', ${!c.excluded})">
        ${c.excluded ? "Reprendre" : "Écarter"}
      </button>
    </div>
  </div>`;
}

// Le stock de clips grossit chaque jour : /api/clips ne renvoie par défaut
// que les DEFAULT_WINDOW_DAYS derniers jours (voir serve.py). Ce drapeau
// mémorise un choix explicite d'afficher tout l'historique le temps de la
// session ; il repart à faux au prochain chargement de la page.
let verToutHistorique = false;

async function load() {
  const [answer, videoAnswer] = await Promise.all([
    fetch(`/api/clips${verToutHistorique ? "?all=1" : ""}`), fetch("/api/videos"),
  ]);
  data = await answer.json();
  videos = await videoAnswer.json();
  if (data.error) { $("log").style.display = "block"; $("log").textContent = data.error; return; }
  // Le modèle accompagne le nom ici, une fois, plutôt que sur chaque vignette.
  fill($("camera"), data.cameras, "toutes caméras",
       (nom) => [nom, (data.models || {})[nom]].filter(Boolean).join(" · "));
  fill($("day"), data.days, "tous les jours");
  render();
}

async function afficherTout() {
  verToutHistorique = true;
  $("list").innerHTML = `<p class="empty">Chargement de l'historique complet…</p>`;
  await load();
}

async function toggle(identity, excluded) {
  const answer = await fetch("/api/toggle", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identity, excluded }),
  });
  const result = await answer.json();
  if (result.error) { alert(result.error); return; }
  // La décision est connue : on retourne la vignette tout de suite plutôt que
  // de recharger la liste entière, qui relit le registre et refait défiler la
  // page. La reconstruction des journalières se poursuit en arrière-plan.
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.excluded = excluded;
  render();
}

// --- connexion Blink -------------------------------------------------------
// Le serveur garde la session Blink ouverte entre les deux requêtes : la page
// n'a qu'à poser les questions dans l'ordre où il les réclame.
let authResolve = null;

function showAuth(stage, message) {
  $("authError").textContent = message || "";
  const code = stage === "2fa";
  $("authCreds").hidden = code;
  $("authCode").hidden = !code;
  $("authTitle").textContent = code ? "Vérification en deux étapes" : "Connexion Blink";
  $("authHint").textContent = code
    ? "Blink vient d'envoyer un code. Saisissez-le pour terminer la connexion."
    : "Le mot de passe sert uniquement à ouvrir la session ; seuls les jetons sont enregistrés.";
  $("authOk").textContent = code ? "Valider" : "Se connecter";
  if (!$("auth").open) $("auth").showModal();
  (code ? $("code") : $("user")).focus();
}

function authenticate() {
  return new Promise((resolve) => {
    authResolve = resolve;
    showAuth("creds", "");
  });
}

$("passToggle").onclick = () => {
  const masque = $("pass").type === "password";
  $("pass").type = masque ? "text" : "password";
  $("passToggle").textContent = masque ? "Masquer" : "Afficher";
  $("passToggle").setAttribute(
    "aria-label", masque ? "Masquer le mot de passe" : "Afficher le mot de passe");
};

$("authCancel").onclick = () => {
  $("auth").close();
  if (authResolve) { authResolve(false); authResolve = null; }
};

$("authOk").onclick = async () => {
  const code = !$("authCode").hidden;
  $("authOk").disabled = true;
  $("authError").textContent = "Connexion en cours…";
  const body = code
    ? { code: $("code").value }
    : { username: $("user").value, password: $("pass").value };
  let result;
  try {
    const answer = await fetch(code ? "/api/2fa" : "/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    result = await answer.json();
  } catch (error) {
    result = { status: "error", message: String(error) };
  }
  $("authOk").disabled = false;
  $("pass").value = "";
  if (result.status === "ok") {
    $("auth").close();
    $("code").value = "";
    // ?login=1 n'a servi qu'à ouvrir ce dialogue au premier lancement ; le
    // laisser dans l'adresse referait apparaître la connexion à chaque
    // actualisation ou depuis un signet, alors que la session est valide.
    if (new URLSearchParams(location.search).get("login") === "1") {
      history.replaceState(null, "", location.pathname);
    }
    if (authResolve) { authResolve(true); authResolve = null; }
  } else if (result.status === "2fa") {
    $("code").value = "";
    showAuth("2fa", "");
  } else {
    showAuth(code ? "2fa" : "creds", result.message || "Échec de la connexion.");
  }
};

$("refresh").onclick = async () => {
  const status = await (await fetch("/api/status")).json();
  if (!status.authenticated && !(await authenticate())) return;

  const button = $("refresh");
  button.disabled = true;
  actualisationLocale = true;
  $("log").style.display = "block";
  $("log").textContent = "";
  $("work").classList.add("on");
  let label = "Démarrage…";
  $("phase").textContent = label;
  $("bar").removeAttribute("value");   // barre indéterminée tant qu'on ne sait pas

  const source = new EventSource("/api/refresh");
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.phase) {
      label = event.phase;
      $("phase").textContent = label;
      $("bar").removeAttribute("value");
    }
    if (event.progress) {
      // done est fractionnaire : la partie entière compte les clips terminés,
      // la décimale l'avancement dans le clip en cours. La barre est donc
      // continue au lieu de sauter d'un cran par clip.
      const p = event.progress;
      $("bar").max = p.total;
      $("bar").value = p.done;
      const current = Math.min(Math.floor(p.done) + 1, p.total);
      $("phase").textContent =
        `${label} ${current}/${p.total} (${Math.round((p.done / p.total) * 100)} %)`;
    }
    if (event.line !== undefined) {
      $("log").textContent += event.line + "\\n";
      $("log").scrollTop = $("log").scrollHeight;
    }
    if (event.done) {
      // Sans close(), EventSource se reconnecte tout seul et relancerait
      // l'actualisation en boucle.
      source.close();
      $("work").classList.remove("on");
      button.disabled = false;
      actualisationLocale = false;
      if (!event.ok) $("phase").textContent = "Terminé avec des erreurs";
      load();
    }
  };
  source.onerror = () => {
    source.close();
    $("work").classList.remove("on");
    button.disabled = false;
    actualisationLocale = false;
    $("log").textContent += "\\nConnexion interrompue.\\n";
  };
};

for (const id of ["view", "camera", "day", "showOut"]) $(id).onchange = render;
// Seule cette ligne de texte se met à jour d'elle-même : elle sert précisément
// à repérer une boucle arrêtée, ce qu'on ne verrait pas en regardant des clips
// qui, eux, ne changent plus.
// Une minute au repos, trois secondes pendant un calcul : c'est le seul moment
// où quelque chose bouge assez vite pour qu'on ait envie de le suivre.
(function veiller() {
  setTimeout(async () => {
    await heuresDePassage();
    veiller();
  }, travailEnCours ? 3000 : 60000);
})();
$("auto").checked = localStorage.getItem("auto") === "1";
$("auto").onchange = () => {
  localStorage.setItem("auto", $("auto").checked ? "1" : "0");
  heuresDePassage();
};
$("view").value = "clips";   // au démarrage on montre les clips, pas d'appel réseau
load();
// E-01 : blink2video ouvre cette page avec ?login=1 quand aucune session
// valide n'a été trouvée, pour que la fenêtre de connexion soit le premier
// écran utile plutôt qu'un bouton à découvrir.
if (new URLSearchParams(location.search).get("login") === "1") authenticate();
</script>
</body>
</html>
"""

# La page est un gabarit constant, plein d'accolades CSS et JavaScript :
# impossible d'en faire une f-string. Une substitution unique au chargement
# suffit, et laisse le gabarit lisible.
PAGE = PAGE.replace("__VERSION__", runtime.VERSION)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="blink2video serve",
        description="Interface locale pour visionner les clips Blink, en écarter "
                    "et en reprendre."
    )
    parser.add_argument("--input", type=Path, default=md.DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=md.DEFAULT_OUTPUT)
    parser.add_argument("--weekly-output", type=Path, default=md.DEFAULT_WEEKLY)
    parser.add_argument("--monthly-output", type=Path, default=md.DEFAULT_MONTHLY)
    parser.add_argument("--normalized-output", type=Path, default=md.DEFAULT_NORMALIZED)
    parser.add_argument("--excluded-output", type=Path, default=md.DEFAULT_EXCLUDED)
    parser.add_argument("--timezone", default="Europe/Paris")
    parser.add_argument("--hub", default="Maison", help="nom du Sync Module Blink")
    parser.add_argument(
        "--thumbs", type=Path, default=BASE_DIR / ".blink_thumbs",
        help="cache des vignettes ; jetable, refabriqué à la demande",
    )
    parser.add_argument("--port", type=runtime.port_valide, default=8765)
    parser.add_argument(
        # Un serveur n'ouvre pas de fenêtre de lui-même : c'est l'usage, et
        # celui-ci passe l'essentiel de sa vie lancé au démarrage de session, où
        # surgir dans le navigateur serait déplacé. On le demande donc.
        "--open-browser", action="store_true",
        help="ouvrir la page dans le navigateur au démarrage"
    )
    return parser.parse_args()


def veiller_sur_les_versions() -> None:
    """Tient à jour, en fond, la connaissance de la dernière version publiée.

    Un fil séparé plutôt qu'un appel dans la page : GitHub peut mettre dix
    secondes à répondre, ou ne pas répondre du tout, et rien de tout cela ne
    doit se voir depuis l'interface. Une visite par jour de fonctionnement
    suffit à repérer une publication."""
    def veille():
        while True:
            try:
                maj.disponible()
            except Exception:      # une panne de réseau n'arrête pas le serveur
                pass
            time.sleep(maj.FRAICHEUR)

    threading.Thread(target=veille, daemon=True).start()


def main() -> int:
    args = parse_args()
    Handler.paths = {
        "input": args.input.resolve(),
        "normalized": args.normalized_output.resolve(),
        "excluded": args.excluded_output.resolve(),
        "thumbs": args.thumbs.resolve(),
        "daily": args.output.resolve(),
        "weekly": args.weekly_output.resolve(),
        "monthly": args.monthly_output.resolve(),
    }
    Handler.hub = args.hub
    try:
        Handler.ffmpeg = md.find_ffmpeg()
        Handler.timezone = ZoneInfo(args.timezone)
        collect(Handler.paths, Handler.timezone, Handler.ffmpeg)
    except (RuntimeError, ZoneInfoNotFoundError) as error:
        print(f"Erreur : {error}")
        return 1

    # 127.0.0.1 et pas 0.0.0.0 : cet outil déplace des fichiers, il n'a rien à
    # faire sur le réseau local.
    class Server(http.server.ThreadingHTTPServer):
        # http.server active allow_reuse_address, dont la sémantique diffère
        # sous Windows : plusieurs serveurs peuvent s'y lier au même port sans
        # la moindre erreur, et lequel reçoit les connexions est indéterminé.
        # Une instance oubliée continue alors de servir sa page en mémoire, et
        # l'on croit ses modifications sans effet. On préfère donc un refus
        # franc ici, en gardant le comportement Unix habituel ailleurs (où
        # l'option sert à relancer sans attendre la fin du TIME_WAIT).
        allow_reuse_address = os.name != "nt"

    try:
        server = Server(("127.0.0.1", args.port), Handler)
    except OSError as error:
        print(f"Impossible d'écouter sur le port {args.port} : {error}")
        print("Un autre « blink2video serve » tourne sans doute déjà. Arrêtez-le, "
              "choisissez un autre port avec --port.")
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Interface disponible sur {url}   (Ctrl+C pour arrêter)")
    veiller_sur_les_versions()
    if args.open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
