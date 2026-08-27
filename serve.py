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

from __future__ import annotations  # Python 3.8 (build Windows 7) : les annotations "X | None" ne s'évaluent qu'à l'écriture des chaînes, jamais à l'exécution.

import argparse
import asyncio
import datetime as dt
import email.utils
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
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python 3.8 (build Windows 7, voir build-win7.yml) : pas de zoneinfo en stdlib.
    from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

import autostart
import blink_auth
import blink_engine
import blink_models
import blink_registre
import maj
import merge_daily as md
import watch


BASE_DIR = runtime.app_dir()

# Un identifiant de clip est un chemin relatif « caméra/mois/fichier.mp4 ».
# Tout ce qui arrive du navigateur est confronté au registre avant d'ouvrir
# quoi que ce soit : aucun chemin fabriqué à la main n'est servi.
IDENTITY = re.compile(r"^[\w.\- ]+(/[\w.\- ]+)*\.mp4$")

# Avancement annoncé par blink2video.py et merge_daily.py, et titres de phase émis
# par blink_engine.py (« === STOCKAGE LOCAL === », « === CLOUD DE L'ABONNEMENT === »).
PROGRESS = re.compile(r"\[(\d+)/(\d+)\]")
# Ligne d'avancement à l'intérieur d'un clip : « [3/24] 45% », rien d'autre.
INNER = re.compile(r"^\s*\[\d+/\d+\]\s+(\d+)%\s*$")
HEADING = re.compile(r"^=== (.+?) ===$")

# Limite le nombre d'extractions de vignettes simultanées (voir send_thumb) :
# le navigateur les réclame toutes d'un coup à chaque changement de filtre, un
# nombre fixe évite de saturer la machine pour autant de ffmpeg à la fois.
# Fixé à 2 à l'origine, beaucoup trop bas pour une machine multi-cœurs
# actuelle : passer d'un préréglage large (mois, déjà en cache) à un plus
# étroit mais couvrant des clips jamais vus (aujourd'hui, cette semaine)
# mettait chaque vignette manquante en attente derrière seulement deux
# extractions à la fois, le spinner natif du lecteur tournant sur chacune le
# temps de son tour (constaté en réel, 2026-08-27). Une extraction reste bien
# plus légère qu'un encodage complet (une seule image, pas de vidéo entière) :
# le nombre de cœurs est un plafond raisonnable, pas un risque comparable.
THUMB_SLOTS = threading.Semaphore(min(8, os.cpu_count() or 4))

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

# Aucune vignette n'est redemandée d'elle-même : elle est récupérée une fois,
# puis conservée jusqu'à ce qu'on clique sur Actualiser. Une image qui change
# seule sous les yeux, sans qu'on l'ait demandé, n'est pas un service rendu.


# md.safe_name directement : safe_file() était une troisième copie du même
# nettoyage, dérivée des deux autres (revue de code du 0eab463, bug #5).
safe_file = md.safe_name


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

# Préréglages du filtre de plage (bouton de découpage rapide de la page
# clips), en heures plutôt qu'en jours : « aujourd'hui » vise les 24 dernières
# heures glissantes, pas depuis minuit, pour rester utile à toute heure de la
# journée. « month » reprend DEFAULT_WINDOW_DAYS : même bouton que la fenêtre
# par défaut, pas une deuxième valeur à tenir synchronisée. « 2months » suit
# la limite de rétention du stockage cloud Blink aux États-Unis, au-delà de
# laquelle les clips y sont supprimés (signalé sur Reddit, 2026-08-27).
RANGE_PRESETS_HOURS = {
    "today": 24,
    "week": 7 * 24,
    "month": DEFAULT_WINDOW_DAYS * 24,
    "2months": 60 * 24,
}

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
            depuis: "dt.datetime | None" = None,
            jusqua: "dt.datetime | None" = None) -> dict:
    """Inventorie les clips connus du registre de téléchargement.

    Contrairement à merge_daily.load_groups, les clips écartés sont conservés :
    c'est précisément ce qu'on veut pouvoir revoir et reprendre.

    `depuis`/`jusqua` bornent la fenêtre renvoyée, chacun optionnel : le stock
    grossit chaque jour, et sans borne basse la page finirait par transmettre
    et dessiner un nombre de vignettes sans rapport avec ce qu'on regarde
    réellement. L'historique complet reste accessible explicitement (voir
    DEFAULT_WINDOW_DAYS). Les deux sont des horodatages complets, pas de
    simples dates : le filtre de plage personnalisée de la page cible une
    période à l'heure près, pour retrouver un incident précis sans défiler
    tout l'historique d'une caméra toujours armée (signalé sur Reddit,
    2026-08-27)."""
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
        if depuis is not None and local < depuis:
            continue
        if jusqua is not None and local > jusqua:
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
            "sourceDeleted": bool(entry.get("source_deleted")),
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
        # Permet à la page de dire « X clips sur Y connus » et de proposer
        # explicitement de charger le reste : quelle plage précise est active
        # (préréglage ou personnalisée) est déjà su côté page, elle seule l'a
        # demandée, inutile de la lui redécrire ici.
        "filtered": depuis is not None or jusqua is not None,
        "total_known": total,
        # La galerie ne propose pas la case Supprimer pour une caméra déjà en
        # suppression automatique (issue GitHub #1) : redondant, le clip sera
        # de toute façon retiré de sa source au prochain téléchargement réussi.
        "suppressionAuto": sorted(runtime.lire_suppression_auto()),
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
                    self.session = blink_auth.session_http()
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
            async with blink_auth.session_http_temporaire() as session:
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


def _choisir_dossier_windows(initial: str) -> str:
    """Sélecteur de dossier natif via PowerShell/WinForms, chaîne vide si
    annulé.

    tkinter dépend de Tcl/Tk, pas forcément embarqué correctement dans un
    bundle PyInstaller (constaté en conditions réelles sur l'édition
    Windows 7 : « Sélecteur de dossier indisponible »). System.Windows.Forms
    fait partie de .NET Framework, présent par défaut depuis Windows Vista
    (donc Windows 7 aussi), et ne demande rien à empaqueter. Un propriétaire
    invisible et TopMost sert le même rôle que le -topmost de tkinter : sans
    lui, la boîte peut s'ouvrir derrière le navigateur."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$proprietaire = New-Object System.Windows.Forms.Form; "
        "$proprietaire.TopMost = $true; "
        "$proprietaire.WindowState = 'Minimized'; "
        "$proprietaire.ShowInTaskbar = $false; "
        "$dialogue = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$dialogue.SelectedPath = '{initial.replace(chr(39), chr(39) * 2)}'; "
        "if ($dialogue.ShowDialog($proprietaire) -eq "
        "[System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialogue.SelectedPath) }"
    )
    resultat = runtime.lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", check=False,
    )
    if resultat.returncode != 0:
        raise RuntimeError((resultat.stderr or "").strip() or "PowerShell a échoué")
    return resultat.stdout.strip()


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

    # ------------------------------------------------------------ protection

    _HOTES_LOCAUX = ("127.0.0.1", "localhost", "::1")

    def hote_autorise(self) -> bool:
        """Faux si Host (ou Origin, quand le navigateur l'envoie) ne désigne
        pas cette machine.

        L'interface n'a pas d'authentification (40210a6, délibéré : un outil
        personnel, pas un service multi-utilisateur) ; le seul rempart contre
        une page tierce qui actionnerait l'API à l'insu de qui la visite est
        de vérifier d'où vient la requête. Un client HTTP quelconque (tests,
        `curl` local) n'envoie pas Origin : seul Host, toujours présent,
        est alors regardé."""
        hote = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if hote not in self._HOTES_LOCAUX:
            return False
        origine = self.headers.get("Origin")
        if origine and urlparse(origine).hostname not in self._HOTES_LOCAUX:
            return False
        return True

    def jeton_valide(self) -> bool:
        """Le jeton de process (TOKEN) doit accompagner toute requête qui
        change quelque chose.

        Distinct de `hote_autorise()` : Host se falsifie difficilement mais
        se contourne par re-liaison DNS (un domaine public pointé sur
        127.0.0.1) ; un en-tête personnalisé, lui, ne peut être posé que par
        du code qui a lu la page servie ici, ce qu'une origine étrangère ne
        peut pas faire (politique de même origine du navigateur)."""
        return self.headers.get("X-Blink-Token") == TOKEN

    def end_headers(self) -> None:
        # cadre 'none' : même une page de ce site ne doit pas pouvoir
        # s'afficher dans un <iframe>, dernier rempart contre le
        # détournement de clic (cliquer sur un bouton qu'on croit ailleurs).
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    # ------------------------------------------------------------------ envoi

    def send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def repondre_puis_redemarrer(self, commande_restart: list) -> None:
        """Répond {"ok": True}, puis détache une commande qui va arrêter CE
        processus (restart ou stop, tous deux via taskkill /F /T sous
        Windows) - factorise /api/reglages et /api/stop, qui partagent
        exactement ce besoin.

        wfile.write()/flush() ne garantissent que la remise à l'OS, pas la
        livraison réelle jusqu'au navigateur : un taskkill trop rapproché
        peut aborter la connexion avant que la pile réseau ait fini de
        transmettre, la fenêtre étant plus large sur une machine chargée ou
        lente (rapporté par un utilisateur sous Windows 7/Python 3.8 :
        JSON.parse en échec côté page, disparaissant une fois la
        surveillance caméra arrêtée - moins de contention). Une courte
        pause après le flush laisse le temps à la pile réseau de vraiment
        vider son tampon avant la mise à mort."""
        self.send_json({"ok": True})
        time.sleep(0.2)
        runtime.demarrer(
            runtime.self_command(*commande_restart), cwd=str(runtime.app_dir()),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))

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

        # Chaque changement de filtre reconstruit toute la grille (voir
        # renderClips côté page), donc chaque <video poster=...> déjà connue
        # redemande la même image : sans validation, le navigateur la
        # retéléchargeait entièrement à chaque fois, un flash noir le temps
        # que ça revienne pour toutes les vignettes à la fois (constaté en
        # réel, 2026-08-27). La vignette elle-même ne change qu'au prochain
        # re-téléchargement du clip source (voir `fresh` ci-dessus) : une
        # revalidation conditionnelle est donc sûre, pas juste plus rapide.
        derniere_modif = email.utils.formatdate(thumb.stat().st_mtime, usegmt=True)
        depuis = self.headers.get("If-Modified-Since")
        if depuis:
            try:
                pas_change = email.utils.parsedate_to_datetime(depuis) \
                    >= email.utils.parsedate_to_datetime(derniere_modif)
            except (TypeError, ValueError):
                pas_change = False
            if pas_change:
                self.send_response(304)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return

        body = thumb.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Last-Modified", derniere_modif)
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

    def reveiller_camera(self, name: str) -> None:
        """Reveille une camera pour de bon, pas une simple relecture du cache.

        `system_state()`/`/api/system` relit deja le compte a chaque fois,
        mais c'est une lecture passive : les deux GET que blinkpy y fait
        (config, capteurs) renvoient ce que Blink a deja en archive cote
        serveur, jamais une valeur plus fraiche qu'un appareil endormi n'a
        pas encore renvoyee. `snap_picture()` poste une vraie commande, que
        Blink relaie a la camera physique et attend qu'elle confirme avant
        de repondre (jusqu'a 120 s dans le pire cas) - le seul mecanisme qui
        force reellement un reveil, au prix reel d'une photo prise et d'un
        peu de batterie a chaque clic. Voir AUDIT 28.51 (suite) pour la
        recherche qui a mene ici."""
        def demander(blink):
            async def run(_blink=blink):
                _, camera = BLINK.find_camera(_blink, name)
                await camera.snap_picture()
            return run()

        BLINK.call(demander, timeout=130)

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
            if runtime.est_relatif_a(candidate, root.resolve()) and candidate.is_file():
                return candidate
        return None

    # ------------------------------------------------------------------ routes

    def do_GET(self):
        if not self.hote_autorise():
            self.send_error(403)
            return
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

        if route == "/favicon.ico":
            chemin = runtime.resource_dir() / "assets" / "blink2video.ico"
            if not chemin.is_file():
                self.send_error(404)
                return
            corps = chemin.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/x-icon")
            # Statique, ne change jamais en cours de route : autant laisser le
            # navigateur cesser de le redemander à chaque page.
            self.send_header("Cache-Control", "public, max-age=604800")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)
            return

        if route.startswith("/camthumb/"):
            # Pas de contrôle du nom ici : il coûterait un rafraîchissement
            # complet du compte à chaque vignette. C'est find_camera, plus bas,
            # qui refuse un nom inconnu, et safe_file qui assainit le nom de
            # fichier du cache.
            self.send_camera_thumb(unquote(route[len("/camthumb/"):]))
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

        if route == "/api/autostart":
            self.send_json({"actif": autostart.est_installe()})
            return

        if route == "/api/choisir-dossier":
            # Le navigateur ne peut pas rendre un chemin absolu (File System
            # Access API : juste un handle, par conception, pour la vie
            # privée du web). Serveur et page étant sur la même machine ici
            # (outil local, pas un service distant), une boîte de dialogue
            # native comble ce manque. PowerShell/WinForms sous Windows (voir
            # _choisir_dossier_windows) ; tkinter en repli ailleurs - importé
            # localement pour ne jamais peser sur les environnements sans
            # affichage (CI Linux headless), qui n'empruntent jamais cette
            # route.
            try:
                if os.name == "nt":
                    choisi = _choisir_dossier_windows(runtime.lire_dossier_stockage())
                else:
                    import tkinter
                    from tkinter import filedialog
                    racine = tkinter.Tk()
                    racine.withdraw()
                    racine.attributes("-topmost", True)
                    choisi = filedialog.askdirectory(initialdir=runtime.lire_dossier_stockage())
                    racine.destroy()
            except Exception as error:
                self.send_json({"error": str(error)}, 500)
                return
            self.send_json({"path": choisi or ""})
            return

        if route == "/api/reglages":
            self.send_json(
                {**runtime.lire_reglages(), "storage_dir": runtime.lire_dossier_stockage()})
            return

        if route == "/api/sourdine":
            # Les caméras connues viennent du registre des clips ET du
            # dernier état de watch.py (fichier local, aucun appel réseau à
            # Blink ici) : une caméra durablement hors de portée peut n'avoir
            # jamais produit de clip et resterait sinon absente de la liste,
            # donc impossible à mettre en sourdine depuis cette page (cas
            # vécu : « Portail »).
            etat = md.load_json(watch.WATCH_STATE, {})
            cameras = sorted(set(provenances(read_entries(self.paths)))
                              | set(etat.get("cameras") or {}))
            self.send_json({"cameras": cameras, "ignored": sorted(etat.get("ignored") or [])})
            return

        if route == "/api/suppression-auto":
            # Toutes les caméras connues, qu'elles viennent de la clé ou du
            # cloud de l'abonnement : selon la source du clip téléchargé,
            # blink_engine.py supprime du Sync Module ou du cloud (issue
            # GitHub #1, voir runtime.lire_suppression_auto()).
            cameras = sorted(provenances(read_entries(self.paths)))
            self.send_json({"cameras": cameras,
                             "actives": sorted(runtime.lire_suppression_auto())})
            return

        if route == "/api/clips":
            # ?all=1 lève explicitement toute borne : l'historique complet
            # reste à un clic, jamais perdu, seulement pas chargé d'office
            # (voir DEFAULT_WINDOW_DAYS). ?preset=today|week|month|2months
            # couvre les boutons de découpage rapide de la page (RANGE_
            # PRESETS_HOURS) ; ?depuis/?jusqua (ISO, sans fuseau : lus dans le
            # fuseau réglé, comme l'horodatage de chaque clip) couvrent sa
            # plage personnalisée à l'heure près. Sans aucun paramètre, la
            # fenêtre reste celle d'avant ces boutons : les DEFAULT_WINDOW_DAYS
            # derniers jours, exactement ce que rend aussi ?preset=month.
            query = parse_qs(urlparse(self.path).query)
            depuis = jusqua = None
            if query.get("all", ["0"])[0] != "1":
                maintenant = dt.datetime.now(self.timezone)
                preset = (query.get("preset") or [""])[0]
                depuis_brut = (query.get("depuis") or [""])[0]
                jusqua_brut = (query.get("jusqua") or [""])[0]
                if preset in RANGE_PRESETS_HOURS:
                    depuis = maintenant - dt.timedelta(hours=RANGE_PRESETS_HOURS[preset])
                elif depuis_brut or jusqua_brut:
                    try:
                        if depuis_brut:
                            depuis = dt.datetime.fromisoformat(depuis_brut) \
                                .replace(tzinfo=self.timezone)
                        if jusqua_brut:
                            jusqua = dt.datetime.fromisoformat(jusqua_brut) \
                                .replace(tzinfo=self.timezone)
                    except ValueError:
                        self.send_json({"error": "Plage de dates invalide."}, 400)
                        return
                else:
                    depuis = maintenant - dt.timedelta(hours=RANGE_PRESETS_HOURS["month"])
            try:
                self.send_json(collect(self.paths, self.timezone, self.ffmpeg,
                                       depuis, jusqua))
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

        Deux self_command distincts (« download » puis « merge »), pas un
        verbe unique qui les enchaînerait : ce sont les deux seules mains de
        l'outil, et les appeler l'un après l'autre dit exactement ce qui se
        passe. Voir run_refresh()."""
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
        etapes = [("Téléchargement", "phase.step_download",
                  runtime.self_command("download", "--hub", self.hub)),
                  ("Fusion", "phase.step_merge", runtime.self_command("merge"))]
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
        for phase, cle, command in etapes:
            self.send_event({"phase": phase, "phase_key": cle, "line": f"$ {phase.lower()}"})
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
                # Ces deux titres sont les seuls que blink_engine.py émette
                # sous cette forme (voir traiter_cloud/un_passage) : une clé
                # stable permet à la page de les traduire, le nom du hub
                # (donnée de l'utilisateur, jamais traduisible) passant à part.
                if titre == "CLOUD DE L'ABONNEMENT":
                    event["phase_key"] = "phase.cloud_section"
                elif titre.startswith("STOCKAGE LOCAL : "):
                    event["phase_key"] = "phase.usb_section"
                    event["phase_hub"] = titre[len("STOCKAGE LOCAL : "):].strip()
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
        if not self.hote_autorise() or not self.jeton_valide():
            self.send_error(403)
            return
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

        if route == "/api/reveiller":
            name = str(payload.get("name", "")).strip()
            try:
                self.reveiller_camera(name)
                self.send_json(self.system_state())
            except Exception as error:
                # Delai large (jusqu'a 130 s, voir reveiller_camera) : outre
                # RuntimeError (camera inconnue), un timeout cote blinkpy ou
                # un refus reseau doivent aussi remonter un message lisible
                # plutot qu'une erreur 500 muette.
                self.send_json({"error": f"{type(error).__name__}: {error}"}, 503)
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

        if route == "/api/appliquer-selection":
            # Remplace les anciens /api/toggle et /api/supprimer-source (un
            # clip à la fois) : un lot entier en un seul appel, pour que la
            # suppression USB ne paie qu'une fois par Sync Module concerné le
            # délai de régénération du manifeste (jusqu'à une minute, voir
            # AUDIT 28.73/28.75), pas une fois par clip.
            exclure = [str(x) for x in (payload.get("exclure") or [])
                       if IDENTITY.match(str(x))]
            inclure = [str(x) for x in (payload.get("inclure") or [])
                       if IDENTITY.match(str(x))]
            supprimer = [str(x) for x in (payload.get("supprimer") or [])
                         if IDENTITY.match(str(x))]

            entrees = read_entries(self.paths)

            def trouver_entree(identity):
                return next(
                    (e for e in entrees.values()
                     if isinstance(e, dict) and e.get("path") == identity), None)

            if exclure or inclure:
                def travailler_registre():
                    # Une seule décision à la fois par sens (écarter/réintégrer) :
                    # le registre est un fichier, deux écritures concurrentes en
                    # perdraient une. set_excluded accepte déjà une liste entière.
                    with REGISTRE:
                        for liste, cible in ((exclure, True), (inclure, False)):
                            if not liste:
                                continue
                            try:
                                md.set_excluded(
                                    self.paths["input"], self.paths["normalized"],
                                    self.paths["excluded"],
                                    [str(self.paths["input"] / i) for i in liste], cible)
                            except RuntimeError as error:
                                print(f"Écarter (lot) : {error}")
                    # Une seule reconstruction par (caméra, jour) touché, même si
                    # plusieurs clips de ce jour ont changé de statut ensemble.
                    jours = set()
                    for identity in exclure + inclure:
                        entree = trouver_entree(identity)
                        camera = str((entree or {}).get("camera") or "").strip()
                        try:
                            jour = md.parse_created_at(
                                str((entree or {}).get("created_at"))
                            ).astimezone(self.timezone).date().isoformat()
                        except (TypeError, ValueError):
                            jour = None
                        if camera and jour:
                            jours.add((camera, jour))
                    # Un seul réassemblage à la fois : deux assemblages
                    # simultanés de la même journée écriraient le même fichier.
                    with REASSEMBLAGE:
                        for camera, jour in jours:
                            runtime.lancer(
                                runtime.self_command("merge", "--camera", camera,
                                                      "--date", jour),
                                cwd=str(runtime.app_dir()), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                check=False,
                            )

                threading.Thread(target=travailler_registre, daemon=True).start()

            resultats = {}
            if supprimer:
                # L'identifiant distant est encodé dans le nom de fichier depuis
                # blink_models.target_path() ({date}_{camera}_{id}_{empreinte}
                # .mp4) ; remote_id (registre) sert de repli pour les entrées
                # plus anciennes, d'avant cette convention.
                cibles = []
                for identity in supprimer:
                    entree = trouver_entree(identity)
                    if entree is None:
                        resultats[identity] = "inconnu"
                        continue
                    correspondance = re.search(r"_(\d+)_[0-9a-f]{12}\.mp4$", identity)
                    id_distant = correspondance.group(1) if correspondance else str(
                        entree.get("remote_id") or "")
                    if not id_distant:
                        resultats[identity] = "identifiant_introuvable"
                        continue
                    cibles.append((identity, entree, id_distant))

                nb_cameras_usb = len({
                    str(e.get("camera") or "").strip() for _, e, _ in cibles
                    if str(e.get("source") or "usb") != "cloud"
                })

                # Sync Module -> ids actuellement presents, remplis pendant
                # operation() : la lecture du manifeste (payee de toute facon
                # pour les clips vises) profite aussi a tout AUTRE clip connu
                # du meme module, jamais visé par cette suppression.
                ids_presents_par_module = {}

                async def operation(blink):
                    manifestes = {}
                    for identity, entree, id_distant in cibles:
                        source = str(entree.get("source") or "usb")
                        camera = str(entree.get("camera") or "").strip()
                        try:
                            if source == "cloud":
                                clip = blink_models.CloudClip({
                                    "id": int(id_distant), "device_name": camera,
                                    "created_at": entree.get("created_at"),
                                })
                                resultats[identity] = (
                                    "supprime" if await clip.delete_video(blink)
                                    else "echec")
                                continue
                            sync, _ = BLINK.find_camera(blink, camera)
                            # Une seule lecture de manifeste par Sync Module,
                            # même si plusieurs clips ciblés lui appartiennent.
                            cle = id(sync)
                            if cle not in manifestes:
                                manifestes[cle] = await blink_models.read_local_manifest(sync)
                                ids_presents_par_module[str(getattr(sync, "sync_id", ""))] = {
                                    str(c.id) for c in manifestes[cle]}
                            cible_clip = next(
                                (c for c in manifestes[cle] if str(c.id) == id_distant),
                                None)
                            if cible_clip is None:
                                resultats[identity] = "deja_absent"
                            else:
                                resultats[identity] = (
                                    "supprime" if await cible_clip.delete_video(blink)
                                    else "echec")
                        except Exception as error:
                            resultats[identity] = f"echec: {type(error).__name__}"

                try:
                    BLINK.call(operation, timeout=30 + 90 * max(1, nb_cameras_usb))
                except Exception as error:
                    for identity, _, _ in cibles:
                        resultats.setdefault(identity, f"echec: {type(error).__name__}")

                # Marqué dans le registre (issue GitHub #1, AUDIT 28.76/28.77) :
                # la galerie sait déjà, sans appel réseau supplémentaire, qu'il
                # n'y a plus rien à supprimer là-bas. "deja_absent" compte
                # aussi : c'est exactement l'état que la case doit refléter.
                marques = {identity for identity, statut in resultats.items()
                           if statut in ("supprime", "deja_absent")}
                if marques or ids_presents_par_module:
                    etat = blink_registre.load_download_state(self.paths["input"])
                    for entree in etat["clips"].values():
                        if not isinstance(entree, dict) or entree.get("source_deleted"):
                            continue
                        if entree.get("path") in marques:
                            entree["source_deleted"] = True
                            continue
                        # Reste des clips USB du même Sync Module : la lecture
                        # du manifeste, déjà payée ci-dessus, dit aussi qu'ils
                        # n'y sont plus, sans requête de plus.
                        ids_presents = ids_presents_par_module.get(
                            str(entree.get("sync_id") or ""))
                        if ids_presents is None or str(entree.get("source") or "usb") != "usb":
                            continue
                        correspondance = re.search(
                            r"_(\d+)_[0-9a-f]{12}\.mp4$", str(entree.get("path") or ""))
                        id_connu = correspondance.group(1) if correspondance else str(
                            entree.get("remote_id") or "")
                        if id_connu and id_connu not in ids_presents:
                            entree["source_deleted"] = True
                    blink_registre.save_download_state(self.paths["input"], etat)

            self.send_json({"ok": True, "resultats": resultats})
            return

        if route == "/api/autostart":
            code = autostart.appliquer("on" if payload.get("actif") else "off")
            if code != 0:
                self.send_json(
                    {"error": "Échec de la modification du démarrage automatique."}, 500)
                return
            # Relu plutôt que renvoyé tel quel : si le mécanisme de la
            # plateforme n'a pas vraiment pris (droits, service absent...),
            # l'interface montre l'état réel, pas ce qui a juste été demandé.
            self.send_json({"actif": autostart.est_installe()})
            return

        if route == "/api/reglages":
            try:
                usb_minutes = int(payload.get("usb_minutes"))
                cloud_minutes = int(payload.get("cloud_minutes"))
                port = int(payload.get("port"))
                if usb_minutes < 1 or cloud_minutes < 1:
                    raise ValueError
                if not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                self.send_json(
                    {"error": "Les cadences doivent être des nombres de minutes d'au "
                              "moins 1, et le port un nombre entre 1 et 65535."}, 400)
                return
            storage_dir = str(payload.get("storage_dir", "")).strip()
            if storage_dir:
                # Vérifié en écrivant pour de vrai plutôt que par simple
                # inspection : un chemin qui a l'air valide peut être en
                # lecture seule, sur un disque non monté, etc. Mieux vaut le
                # découvrir ici, avant d'enregistrer quoi que ce soit, qu'au
                # prochain démarrage, en cascade et sans page pour le dire.
                try:
                    candidat = Path(storage_dir).expanduser()
                    candidat.mkdir(parents=True, exist_ok=True)
                    sonde = candidat / ".blink_ecriture_test"
                    sonde.write_text("", encoding="utf-8")
                    sonde.unlink()
                except OSError as error:
                    self.send_json({"error": f"Dossier de stockage inaccessible : {error}"},
                                    400)
                    return
            timezone_str = str(payload.get("timezone", "")).strip()
            try:
                ZoneInfo(timezone_str)
            except (ZoneInfoNotFoundError, ValueError):
                self.send_json({"error": f"Fuseau horaire inconnu : « {timezone_str} »."}, 400)
                return
            timestamp = bool(payload.get("timestamp", False))
            merge_jour = bool(payload.get("merge_jour", True))
            merge_semaine = bool(payload.get("merge_semaine", True))
            merge_mois = bool(payload.get("merge_mois", True))
            download_auto = bool(payload.get("download_auto", True))
            runtime.ecrire_reglages(usb_minutes, cloud_minutes, port, timestamp, timezone_str,
                                    merge_jour, merge_semaine, merge_mois, download_auto)
            runtime.ecrire_dossier_stockage(storage_dir)
            # Comme /api/update : ce processus fait partie de ce que « restart »
            # va arrêter. Le verbe diffère de « update » puisqu'aucune nouvelle
            # version n'est en jeu, seuls les réglages ont changé - mais
            # « restart » relance « start » à neuf, donc les relit.
            self.repondre_puis_redemarrer(["restart"])
            return

        if route == "/api/sourdine":
            camera = str(payload.get("camera", "")).strip()
            ignored = bool(payload.get("ignored"))
            if not camera:
                self.send_json({"error": "Nom de caméra manquant."}, 400)
                return
            # watch relit son état à chaque passage de sa propre boucle
            # (voir _controler) : contrairement aux autres réglages, la
            # sourdine n'a pas besoin de redémarrage, seulement de laisser
            # « watch --ignore/--unignore » écrire le fichier partagé. Fait
            # ici en tâche de fond pour que le clic reste immédiat, sur le
            # même principe que le bouton Écarter (28.33).
            option = "--ignore" if ignored else "--unignore"

            def travailler():
                runtime.lancer(
                    runtime.self_command("watch", option, camera),
                    cwd=str(runtime.app_dir()), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )

            threading.Thread(target=travailler, daemon=True).start()
            self.send_json({"ok": True})
            return

        if route == "/api/suppression-auto":
            camera = str(payload.get("camera", "")).strip()
            actif = bool(payload.get("actif"))
            if not camera:
                self.send_json({"error": "Nom de caméra manquant."}, 400)
                return
            # Pas de redémarrage : un_passage() (blink_engine.py) relit ce
            # fichier à chaque tour, même principe que runtime.lire_langue().
            cameras = runtime.lire_suppression_auto()
            if actif:
                cameras.add(camera)
            else:
                cameras.discard(camera)
            runtime.ecrire_suppression_auto(cameras)
            self.send_json({"ok": True})
            return

        if route == "/api/stop":
            # --sans-relance pour que « restart » s'arrête sans revenir,
            # exactement ce qu'un bouton Stop doit faire.
            self.repondre_puis_redemarrer(["restart", "--sans-relance"])
            return

        if route == "/api/lang":
            runtime.ecrire_langue(str(payload.get("lang", "")))
            self.send_json({"ok": True})
            return

        self.send_error(404)


PAGE = """<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>blink2video</title>
<link rel="icon" href="/favicon.ico">
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
  /* Rouge (même teinte que .out) plutôt que la couleur neutre des autres
     boutons : n'apparaît qu'après une case cochée, donc facile à manquer
     sans ce signal, l'action pouvant supprimer des clips de leur source. */
  button.danger { background:var(--out); border-color:#c0432f; color:#fff; }
  label { color:var(--dim); display:flex; align-items:center; gap:7px; cursor:pointer; }
  /* Un display explicite l'emporte sur l'attribut hidden : sans cette règle,
     « voir les clips écartés » restait affiché dans le Direct et les vidéos
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
  .langGroup { display:flex; border:1px solid var(--line); border-radius:7px; overflow:hidden; }
  .btn-lang { background:var(--card); color:var(--dim); border:none; border-radius:0;
              padding:7px 10px; font:inherit; font-weight:600; cursor:pointer; }
  .btn-lang.active { background:#3a5a86; color:#fff; }
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
  /* Second bouton d'un groupe de droite : la marge auto ne se pose qu'une
     fois, sur le premier, sinon flexbox partage l'espace libre entre les
     deux et les separe au lieu de les coller. */
  .act.grouped { margin-left:0; }
  .act:disabled { opacity:.6; cursor:default; }
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
  /* Sans ce couple de règles, .live garde son 16/9 fixe en plein écran et se
     retrouve barrée de bandes noires au lieu de remplir l'écran. */
  .live:fullscreen, .live:-webkit-full-screen { aspect-ratio:auto; }
  .live img, .live video { width:100%; height:100%; object-fit:contain; }
  /* La vignette reste en fond, le bouton se pose dessus. */
  .live img.still { position:absolute; inset:0; opacity:.55; }
  .live .watch { position:relative; }
  .watch { border-radius:7px; padding:8px 14px; }
  .watch.stop { position:absolute; right:10px; bottom:10px; opacity:.85; }
  .watch.expand { position:absolute; right:10px; top:10px; opacity:.85;
                  padding:5px 9px; font-size:16px; line-height:1; }
  .live { flex-direction:column; gap:12px; }
  .live .hint { color:var(--dim); font-size:14px; margin:0;
                text-align:center; padding:0 20px; line-height:1.4; }
  /* Sans décalage, l'astuce se centre au même endroit que le bouton
     « Réessayer » (seul enfant en flux dans .live) et capte ses clics :
     on la remonte au-dessus et on la rend transparente aux événements. */
  .live .hint.overlay { position:absolute; bottom:56px; left:0; right:0;
                         pointer-events:none; }
  dialog { background:var(--card); color:var(--text); border:1px solid var(--line);
           border-radius:12px; padding:24px; width:min(380px, 92vw); position:relative; }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  .langGroupAuth { position:absolute; top:16px; right:16px; }
  dialog h3 { margin:0 0 6px; font-size:16px; }
  dialog p { margin:0 0 18px; color:var(--dim); font-size:13px; }
  /* :not([type=checkbox]) : la meme regle stretchait aussi les cases a
     cocher a 100% de large (visible seulement sur leur zone cliquable, pas
     sur le dessin de la case), ce qui repoussait leur texte tres loin a
     droite avec un retour a la ligne au milieu des mots. */
  dialog input:not([type="checkbox"]) {
    width:100%; font:inherit; color:var(--text); background:var(--bg);
    border:1px solid var(--line); border-radius:7px;
    padding:9px 11px; margin-bottom:12px;
  }
  .champMdp { display:flex; gap:8px; margin-bottom:12px; }
  .champMdp input { margin-bottom:0; }
  .champMdp button { flex:none; padding:0 12px; }
  dialog .row { display:flex; flex-wrap:wrap; gap:10px; justify-content:flex-end;
                margin-top:6px; }
  #authError { color:var(--out); font-size:13px; min-height:18px; margin:0 0 6px; }
  #reglages { width:min(560px, 92vw); }
  #filtre { width:min(420px, 92vw); }
  .filtreResume { color:var(--dim); font-size:13px; }
  /* Du/au sur la même ligne que leur champ, comme les autres champCadence :
     sans cette largeur fixe, un datetime-local à 100% forçait un retour à
     la ligne (règle générale dialog input) et doublait la hauteur du
     panneau pour rien - il ne tenait alors plus en entier à l'écran sans
     défiler (constaté en réel, 2026-08-27). */
  #rangeFrom, #rangeTo { width:190px; flex:none; }
  .presets { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
  .presets button { flex:none; }
  #reglages label, #filtre label { align-items:flex-start; margin-bottom:14px; }
  #reglages label input[type="checkbox"], #filtre label input[type="checkbox"] {
    flex:none; margin:3px 0 0; width:16px; height:16px;
  }
  #outLabel { margin-bottom:20px; }
  #reglages fieldset { border:1px solid var(--line); border-radius:10px;
                        padding:14px 16px 16px; margin:0 0 16px; }
  #reglages legend { padding:0 6px; font-size:13px; color:var(--dim); cursor:default; }
  .champCadence { display:flex; align-items:center; flex-wrap:wrap;
                  justify-content:space-between;
                  gap:10px; margin-bottom:10px; color:var(--dim); font-size:14px; }
  #reglages .champCadence input { width:70px; margin-bottom:0; text-align:right; }
  /* Le fuseau ("Europe/Paris", "America/Los_Angeles"...) ne tient pas dans
     les 70px des champs numériques voisins : élargi et aligné à gauche
     plutôt que de forcer une largeur commune qui tronquerait sa valeur. */
  #reglages #timezone { width:180px; text-align:left; }
  /* USB et Cloud sur une même ligne : deux paires label+champ, pas un
     agencement bord-à-bord comme .champCadence (qui n'en attend qu'une). */
  .champCadenceDouble { display:flex; align-items:center; flex-wrap:wrap;
                         gap:8px 10px; margin-bottom:10px; color:var(--dim); font-size:14px; }
  #reglages .champCadenceDouble input { width:60px; margin-bottom:0; text-align:right; }
  #reglages fieldset p.sub { margin:0; }
  /* Dossier de stockage : tout sur une ligne, l'aide (déplacement des clips,
     valeur vide) en infobulle plutôt qu'en paragraphe pour tenir sans
     ascenseur. cursor:default : ce n'est pas un contrôle cliquable, juste un
     porteur de title. */
  .champDossier { display:flex; align-items:center; gap:8px; margin-bottom:10px;
                   cursor:default; }
  .champDossier label { flex:none; color:var(--dim); font-size:14px; }
  .champDossier input { flex:1; margin-bottom:0; }
  .champDossier button { flex:none; padding:0 14px; }
  /* Coches d'une même famille (quotidienne/hebdo/mensuelle, sourdine par
     caméra) : en ligne plutôt qu'empilées, repli à la ligne si trop
     nombreuses (nombre de caméras variable, contrairement aux trois cases
     d'archivage). */
  .ligneCoches { display:flex; flex-wrap:wrap; gap:6px 20px; }
  .ligneCoches label { margin-bottom:0; }
  #reglages .row-boutons { margin-top:0; justify-content:space-between; }
  #reglages .row-boutons button { flex:none; white-space:nowrap; padding:9px 12px; }
  #stopButton { border-color:var(--out); color:#ffb3ab; }
</style>
</head>
<body>
<header>
  <h1>blink2video<span class="v">__VERSION__</span></h1>
  <select id="view">
    <option value="live" data-i18n="view.live">Direct</option>
    <option value="clips" data-i18n="view.clips">Clips</option>
    <option value="daily" data-i18n="view.daily">Journalières</option>
    <option value="weekly" data-i18n="view.weekly">Hebdomadaires</option>
    <option value="monthly" data-i18n="view.monthly">Mensuelles</option>
  </select>
  <button id="filtreButton" data-i18n="filtre.button" data-i18n-title="filtre.button.title"
          title="Filtrer">🔍 Filtre</button>
  <span class="filtreResume" id="filtreResume"></span>
  <span class="filtreResume" id="filtreCompte"></span>
  <span class="count" id="count"></span>
  <!-- Tout ce qui concerne la mise à jour tient ensemble, à droite : l'heure du
       dernier passage, la coche qui recharge seule, et le bouton. -->
  <div class="maj">
    <button id="update" hidden></button>
    <span class="sub tiny" id="passages"></span>
    <button class="danger" id="applyButton" hidden onclick="appliquerSelection()"></button>
    <button class="primary" id="refresh" data-i18n="btn.refresh">↻ Actualiser</button>
    <button id="reglagesButton" data-i18n="btn.reglages" data-i18n-title="btn.reglages.title" title="Réglages">⚙ Réglages…</button>
  </div>
  <span class="langGroup" title="Langue / Language">
    <button class="btn-lang" data-lang-btn="fr" onclick="setLang('fr', true)">FR</button>
    <button class="btn-lang" data-lang-btn="en" onclick="setLang('en', true)">EN</button>
  </span>
  <div id="work"><span id="phase"></span><progress id="bar"></progress></div>
</header>
<main><div id="list"></div><pre id="log"></pre></main>

<dialog id="auth">
  <!-- Boîte modale : le fond assombri masque et rend inatteignables les
       boutons FR/EN de l'en-tête tant qu'elle est ouverte (showModal()).
       Doublon minimal ici, seul moyen de changer de langue avant de se
       connecter. -->
  <span class="langGroup langGroupAuth" title="Langue / Language">
    <button class="btn-lang" data-lang-btn="fr" onclick="setLang('fr', true)">FR</button>
    <button class="btn-lang" data-lang-btn="en" onclick="setLang('en', true)">EN</button>
  </span>
  <h3 id="authTitle" data-i18n="auth.title">Connexion Blink</h3>
  <p id="authHint" data-i18n="auth.hint">Le mot de passe sert uniquement à ouvrir la session ; seuls
     les jetons sont enregistrés, jamais le mot de passe.</p>
  <p id="authError"></p>
  <div id="authCreds">
    <input id="user" type="email" data-i18n-placeholder="auth.email" placeholder="Adresse e-mail" autocomplete="username">
    <div class="champMdp">
      <input id="pass" type="password" data-i18n-placeholder="auth.password" placeholder="Mot de passe"
             autocomplete="current-password">
      <button type="button" id="passToggle" data-i18n="auth.show"
              aria-label="Afficher le mot de passe">Afficher</button>
    </div>
  </div>
  <div id="authCode" hidden>
    <input id="code" inputmode="numeric" data-i18n-placeholder="auth.code" placeholder="Code reçu par SMS ou e-mail"
           autocomplete="one-time-code">
  </div>
  <div class="row">
    <button id="authCancel" data-i18n="auth.cancel">Annuler</button>
    <button class="primary" id="authOk" data-i18n="auth.ok">Se connecter</button>
  </div>
</dialog>

<dialog id="reglages">
  <h3 data-i18n="reglages.title">Réglages</h3>
  <label id="autostartLabel"
         data-i18n-title="reglages.autostart.title"
         title="Démarre le serveur web et le traitement des clips à l'ouverture
                 de session, en arrière-plan — n'ouvre pas cette page toute seule">
    <input type="checkbox" id="autostart"> <span data-i18n="reglages.autostart">Démarrage de la surveillance à
    l'ouverture de session</span>
  </label>
  <label id="autoLabel" data-i18n-title="reglages.auto.title" title="Recharger la liste dès que des clips arrivent">
    <input type="checkbox" id="auto"> <span data-i18n="reglages.auto">Actualisation automatique de la page</span>
  </label>
  <div class="champCadence">
    <label for="port" data-i18n="reglages.serveur">Port du serveur</label>
    <input type="number" id="port" min="1" max="65535" step="1">
  </div>
  <div class="champDossier" data-i18n-title="reglages.storageDir.hint"
       title="Ne déplace pas les clips ni la session Blink déjà présents à l'ancien emplacement : à faire vous-même si vous changez ce chemin. Vide = emplacement par défaut, celui de l'exécutable.">
    <label for="storageDir" data-i18n="reglages.storageDir">Dossier des données</label>
    <input type="text" id="storageDir" data-i18n-placeholder="reglages.storageDir.placeholder"
           placeholder="C:/chemin/vers/le/dossier">
    <button type="button" id="storageDirBrowse" data-i18n="reglages.storageDir.browse">Parcourir…</button>
  </div>
  <fieldset>
    <legend data-i18n="reglages.cadence">Cadence de lecture des caméras</legend>
    <label id="downloadAutoLabel" data-i18n-title="reglages.downloadAuto.hint"
           title="Décochée, aucun clip n'est plus récupéré ni stocké : utile pour ne garder que le direct. Les cadences ci-dessous n'ont alors plus d'effet.">
      <input type="checkbox" id="downloadAuto">
      <span data-i18n="reglages.downloadAuto">Télécharger les clips automatiquement</span>
    </label>
    <div class="champCadenceDouble">
      <label for="usbMinutes" data-i18n="reglages.usb">USB (minutes)</label>
      <input type="number" id="usbMinutes" min="1" step="1">
      <label for="cloudMinutes" data-i18n="reglages.cloud">Cloud (minutes)</label>
      <input type="number" id="cloudMinutes" min="1" step="1">
    </div>
  </fieldset>
  <fieldset>
    <legend data-i18n="reglages.video">Vidéo</legend>
    <label id="timestampLabel">
      <input type="checkbox" id="timestamp"> <span data-i18n="reglages.timestamp">Incruster la date et l'heure
      dans l'image</span>
    </label>
    <div class="champCadence">
      <label for="timezone" data-i18n="reglages.timezone">Fuseau horaire</label>
      <input type="text" id="timezone" list="fuseauxCourants" placeholder="Europe/Paris">
    </div>
    <datalist id="fuseauxCourants">
      <option value="Europe/Paris">
      <option value="Europe/London">
      <option value="Europe/Brussels">
      <option value="Europe/Madrid">
      <option value="Europe/Berlin">
      <option value="America/Montreal">
      <option value="America/New_York">
      <option value="America/Chicago">
      <option value="America/Denver">
      <option value="America/Los_Angeles">
      <option value="Africa/Casablanca">
      <option value="Africa/Abidjan">
      <option value="Indian/Reunion">
      <option value="Asia/Tokyo">
      <option value="Australia/Sydney">
      <option value="UTC">
    </datalist>
  </fieldset>
  <fieldset>
    <legend data-i18n="reglages.archivage"
            data-i18n-title="reglages.archivage.hint"
            title="Hebdomadaire et mensuelle sont assemblées à partir de la quotidienne : décocher « Quotidienne » désactive aussi les deux autres.">Création des vidéos temporelles par caméra</legend>
    <div class="ligneCoches">
      <label id="mergeJourLabel">
        <input type="checkbox" id="mergeJour"> <span data-i18n="reglages.mergeJour">Quotidienne</span>
      </label>
      <label id="mergeSemaineLabel">
        <input type="checkbox" id="mergeSemaine"> <span data-i18n="reglages.mergeSemaine">Hebdomadaire</span>
      </label>
      <label id="mergeMoisLabel">
        <input type="checkbox" id="mergeMois"> <span data-i18n="reglages.mergeMois">Mensuelle</span>
      </label>
    </div>
  </fieldset>
  <fieldset>
    <legend data-i18n="reglages.alertes">Mise en sourdine des alertes</legend>
    <div id="sourdineListe" class="ligneCoches sub tiny" data-i18n="sourdine.loading">Chargement…</div>
  </fieldset>
  <fieldset>
    <legend data-i18n="reglages.suppressionAuto" data-i18n-title="suppressionAuto.hint"
            title="Une fois un clip téléchargé avec succès, il est supprimé de sa source (clé USB ou cloud de l'abonnement selon la caméra).">Suppression automatique après téléchargement</legend>
    <div id="suppressionAutoListe" class="ligneCoches sub tiny" data-i18n="suppressionAuto.loading">Chargement…</div>
  </fieldset>
  <div class="row row-boutons">
    <button class="primary" id="reglagesApply" data-i18n="reglages.apply"
            data-i18n-title="reglages.hint"
            title="Les réglages ne prennent effet qu'au redémarrage : « Appliquer » enregistre et redémarre. Changer le port redirige cette page vers la nouvelle adresse.">Appliquer</button>
    <button id="stopButton" data-i18n="reglages.stop">Arrêter la surveillance des caméras</button>
    <button id="reglagesClose" data-i18n="reglages.close">Fermer</button>
  </div>
</dialog>

<dialog id="filtre">
  <h3 data-i18n="filtre.title">Filtre</h3>
  <div class="champCadence">
    <label for="camera" data-i18n="filtre.camera">Caméra</label>
    <select id="camera"></select>
  </div>
  <label id="outLabel">
    <input type="checkbox" id="showOut"> <span data-i18n="reglages.showOut">Voir les clips écartés</span>
  </label>
  <div id="periodeSection">
    <p class="sub tiny" data-i18n="range.title">Période</p>
    <div class="presets">
      <button type="button" data-preset="today" data-i18n="range.today">Aujourd'hui (24 h)</button>
      <button type="button" data-preset="week" data-i18n="range.week">Cette semaine (7 j)</button>
      <button type="button" data-preset="month" data-i18n="range.month">Ce mois-ci</button>
      <button type="button" data-preset="2months" data-i18n="range.2months">2 derniers mois</button>
      <button type="button" data-preset="all" data-i18n="range.all">Tout l'historique</button>
    </div>
    <p class="sub tiny" data-i18n="range.custom.hint">Ou une plage précise, à l'heure près :</p>
    <div class="champCadence">
      <label for="rangeFrom" data-i18n="range.from">Du</label>
      <input type="datetime-local" id="rangeFrom">
    </div>
    <div class="champCadence">
      <label for="rangeTo" data-i18n="range.to">au</label>
      <input type="datetime-local" id="rangeTo">
    </div>
  </div>
  <div class="row row-boutons">
    <button class="primary" id="filtreApply" data-i18n="range.apply">Filtrer</button>
    <button id="filtreClose" data-i18n="reglages.close">Fermer</button>
  </div>
</dialog>
<script>
// Jeton anti-CSRF (voir Handler.jeton_valide côté serveur, 28.60) : posé sur
// toute requête qui modifie quelque chose. Fait une fois ici, avant tout
// autre script, pour qu'aucun fetch() plus bas n'ait à s'en soucier.
const BLINK_TOKEN = "__TOKEN__";
const _fetchNatif = window.fetch;
window.fetch = (entree, options) => {
  options = options || {};
  const methode = (options.method || "GET").toUpperCase();
  if (methode !== "GET" && methode !== "HEAD") {
    options = { ...options,
               headers: { ...(options.headers || {}), "X-Blink-Token": BLINK_TOKEN } };
  }
  return _fetchNatif(entree, options);
};

let data = { clips: [], cameras: [], days: [] };
let videos = { daily: [], weekly: [], monthly: [] };
const $ = (id) => document.getElementById(id);

// ── i18n ─────────────────────────────────────────────────────────────────
// Même pattern que gui/app.js de lidar2map : dico inline par locale + attribut
// data-i18n sur les nœuds statiques, t()/tf() appelés directement dans le
// texte généré en JS. Zéro dépendance. Le FR en dur dans le HTML reste le
// repli si une clé manque : pas de page cassée. Détection : navigator.language
// au premier chargement ; override manuel persisté en localStorage (page web
// ordinaire servie par serve.py, pas de webview packagée à contourner ici).
const I18N = {
  fr: {
    "view.live": "Direct", "view.clips": "Clips", "view.daily": "Journalières",
    "view.weekly": "Hebdomadaires", "view.monthly": "Mensuelles",
    "filter.allcameras": "toutes caméras",
    "btn.refresh": "↻ Actualiser", "btn.reglages": "⚙ Réglages…", "btn.reglages.title": "Réglages",
    "update.installing": "Installer {version}",
    "update.title": "Version {version} publiée. Le téléchargement, l'arrêt et la relance sont automatiques.",
    "update.updating": "Mise à jour…",
    "update.progress": "Mise à jour vers {version} : téléchargement, puis relance…",
    "passages.updated": "actualisé {heure}",
    "passages.new.one": " · {n} nouveau clip, cliquez sur Actualiser",
    "passages.new.many": " · {n} nouveaux clips, cliquez sur Actualiser",
    "auth.title": "Connexion Blink", "auth.title.2fa": "Vérification en deux étapes",
    "auth.hint": "Le mot de passe sert uniquement à ouvrir la session ; seuls les jetons sont enregistrés, jamais le mot de passe.",
    "auth.hint.2fa": "Blink vient d'envoyer un code. Saisissez-le pour terminer la connexion.",
    "auth.email": "Adresse e-mail", "auth.password": "Mot de passe",
    "auth.show": "Afficher", "auth.hide": "Masquer",
    "auth.show.aria": "Afficher le mot de passe", "auth.hide.aria": "Masquer le mot de passe",
    "auth.code": "Code reçu par SMS ou e-mail",
    "auth.cancel": "Annuler", "auth.ok": "Se connecter", "auth.validate": "Valider",
    "auth.connecting": "Connexion en cours…", "auth.failed": "Échec de la connexion.",
    "reglages.title": "Réglages",
    "reglages.autostart": "Démarrage de la surveillance à l'ouverture de session",
    "reglages.autostart.title": "Démarre le serveur web et le traitement des clips à l'ouverture de session, en arrière-plan — n'ouvre pas cette page toute seule",
    "reglages.auto": "Actualisation automatique de la page",
    "reglages.auto.title": "Recharger la liste dès que des clips arrivent",
    "reglages.showOut": "Voir les clips écartés",
    "reglages.serveur": "Port du serveur",
    "reglages.storageDir": "Dossier des données",
    "reglages.storageDir.placeholder": "C:/chemin/vers/le/dossier",
    "reglages.storageDir.hint": "Ne déplace pas les clips ni la session Blink déjà présents à l'ancien emplacement : à faire vous-même si vous changez ce chemin. Vide = emplacement par défaut, celui de l'exécutable.",
    "reglages.storageDir.browse": "Parcourir…",
    "reglages.storageDir.browse.unavailable": "Sélecteur de dossier indisponible sur cette machine : saisissez le chemin directement.",
    "reglages.cadence": "Cadence de lecture des caméras",
    "reglages.usb": "USB (minutes)", "reglages.cloud": "Cloud (minutes)",
    "reglages.video": "Vidéo", "reglages.timestamp": "Incruster la date et l'heure dans l'image",
    "reglages.timezone": "Fuseau horaire",
    "reglages.archivage": "Création des vidéos temporelles par caméra",
    "reglages.downloadAuto": "Télécharger les clips automatiquement",
    "reglages.downloadAuto.hint": "Décochée, aucun clip n'est plus récupéré ni stocké : utile pour ne garder que le direct. Les cadences ci-dessous n'ont alors plus d'effet.",
    "reglages.mergeJour": "Quotidienne",
    "reglages.mergeSemaine": "Hebdomadaire", "reglages.mergeMois": "Mensuelle",
    "reglages.archivage.hint": "Hebdomadaire et mensuelle sont assemblées à partir de la quotidienne : décocher « Quotidienne » désactive aussi les deux autres.",
    "reglages.alertes": "Mise en sourdine des alertes",
    "reglages.suppressionAuto": "Suppression automatique après téléchargement",
    "reglages.hint": "Les réglages ne prennent effet qu'au redémarrage : « Appliquer » enregistre et redémarre. Changer le port redirige cette page vers la nouvelle adresse.",
    "reglages.apply": "Appliquer", "reglages.restarting": "Redémarrage…",
    "reglages.restarting.settings": "Redémarrage avec les nouveaux réglages…",
    "reglages.portchange": "Port changé : redirection vers {url} dès l'arrêt confirmé…",
    "reglages.stop": "Arrêter la surveillance des caméras", "reglages.close": "Fermer",
    "reglages.error.cadence": "Les cadences doivent valoir au moins 1 minute.",
    "reglages.error.port": "Le port doit être compris entre 1 et 65535.",
    "reglages.error.timezone": "Le fuseau horaire ne peut pas être vide.",
    "stop.stopping": "Arrêt…",
    "stop.stopped": "blink2video est arrêté. Relancez l'application pour reprendre.",
    "sourdine.loading": "Chargement…", "sourdine.unavailable": "Liste des caméras indisponible.",
    "sourdine.none": "Aucune caméra connue pour l'instant.",
    "suppressionAuto.loading": "Chargement…",
    "suppressionAuto.unavailable": "Liste des caméras indisponible.",
    "suppressionAuto.none": "Aucune caméra connue pour l'instant.",
    "suppressionAuto.hint": "Une fois un clip téléchargé avec succès, il est supprimé de sa source (clé USB ou cloud de l'abonnement selon la caméra).",
    "phase.download_clips": "Téléchargement des clips",
    "phase.prepare_clips": "Préparation des clips",
    "phase.assemble_videos": "Assemblage des vidéos",
    "phase.update_download": "Téléchargement de la mise à jour ({mo} Mo)",
    "phase.update_install": "Installation de la mise à jour",
    "phase.step_download": "Téléchargement", "phase.step_merge": "Fusion",
    "phase.cloud_section": "Cloud de l'abonnement",
    "phase.usb_section": "Stockage local : {hub}",
    "live.querying": "Interrogation du système Blink…",
    "live.count": "{n} caméra(s) · {m} armée(s)",
    "system.armed": "Système armé", "system.disarmed": "Système désarmé",
    "camera.offline": "HORS LIGNE", "camera.noeffect": "sans effet, système désarmé",
    "camera.detection.on": "Détection active", "camera.detection.off": "Détection coupée",
    "camera.wake": "Réveiller", "camera.waking": "Réveil…",
    "camera.wake.title": "Réveille la caméra maintenant (prend une photo). Consomme un peu de batterie, jusqu'à 2 minutes.",
    "camera.battery": "batterie {v}", "camera.wifi": "Wi-Fi {v} dBm",
    "camera.lfr": "liaison module {v}", "camera.measured.at": "relevé à {v}",
    "camera.measured.on": "relevé du {v}", "camera.firmware": "micrologiciel {v}",
    "camera.noclips": "aucun clip récupéré", "camera.clipssource": "clips : {v}",
    "camera.none": "—",
    "watch.live": "Voir en direct", "watch.retry": "Réessayer", "watch.stop": "Arrêter",
    "watch.waking": "Réveil de la caméra…", "watch.waking.seconds": "Réveil de la caméra… {s} s",
    "watch.waking.slow": "Réveil de la caméra… {s} s (une caméra sur batterie est plus lente)",
    "watch.waking.mse": "Réveil de la caméra… (MSE)", "watch.reconnecting": "Reconnexion…",
    "live.fullscreen.title": "Agrandir en plein écran",
    "live.fullscreen.title.exit": "Quitter le plein écran",
    "watch.noimage": "Aucune image reçue. La caméra n'a pas répondu.",
    "watch.refused": "Le flux a été refusé par le serveur.",
    "watch.refused.code": "Le flux a été refusé par le serveur ({code}).",
    "watch.refused.retry": "Flux refusé. Un direct précédent finit peut-être de se fermer : réessayez dans quelques secondes.",
    "watch.codec.unsupported": "Codec non supporté par ce navigateur : {codec}",
    "command.sending": "Envoi de la commande…",
    "clips.none.filtered": "Aucun clip ne correspond à ce filtre.",
    "clips.none.ever": "Aucun clip récupéré pour l'instant.<br>Le téléchargement tourne déjà en arrière-plan (clé USB toutes les 10 min, cloud toutes les minutes) : les clips apparaîtront ici sans rien faire. Vérifiez qu'une clé USB est branchée sur le module : sans elle, les enregistrements ne vont que dans le cloud de l'abonnement Blink, que cet outil ne lit pas.",
    "clips.window": "{m}/{total} clips",
    "range.title": "Période",
    "range.today": "Aujourd'hui (24 h)", "range.week": "Cette semaine (7 j)",
    "range.month": "Ce mois-ci", "range.2months": "2 derniers mois",
    "range.all": "Tout l'historique", "range.custom": "Période personnalisée",
    "range.custom.depuis": "depuis {v}", "range.custom.jusqua": "jusqu'au {v}",
    "range.custom.hint": "Ou une plage précise, à l'heure près :",
    "filtre.button": "🔍 Filtre", "filtre.button.title": "Filtrer",
    "filtre.title": "Filtre", "filtre.camera": "Caméra",
    "range.from": "Du", "range.to": "au", "range.apply": "Filtrer",
    "videos.count": "{n} vidéo(s) · {duree} au total",
    "videos.none": "Aucune vidéo assemblée. Lancez une actualisation.",
    "videos.download": "Télécharger",
    "clip.resume": "Reprendre", "clip.discard": "Écarter",
    "clip.discard.title": "Retirer ce clip des vidéos assemblées (quotidienne, hebdomadaire, mensuelle). La copie téléchargée reste sur le disque.",
    "clip.resume.title": "Réinclure ce clip dans les prochains assemblages.",
    "clip.deleteSource": "Supprimer",
    "clip.deleteSource.pending": "Suppression…",
    "clip.deleteSource.title": "Supprimer ce clip de sa source (clé USB ou cloud de l'abonnement). La copie déjà téléchargée ici n'est pas touchée. Peut prendre jusqu'à une minute pour l'USB.",
    "selection.apply": "✓ Appliquer ({n})",
    "selection.confirm.suppression": "{n} clip(s) vont être supprimés de leur source (clé USB ou cloud de l'abonnement). Les copies déjà téléchargées ne sont pas touchées. Continuer ?",
    "selection.partial": "{n} suppression(s) ont échoué ou n'ont rien trouvé à supprimer (déjà retiré ailleurs). Le reste de la sélection a été appliqué.",
    "refresh.starting": "Démarrage…", "refresh.errors": "Terminé avec des erreurs",
    "refresh.disconnected": "\\nConnexion interrompue.\\n",
  },
  en: {
    "view.live": "Live", "view.clips": "Clips", "view.daily": "Daily",
    "view.weekly": "Weekly", "view.monthly": "Monthly",
    "filter.allcameras": "all cameras",
    "btn.refresh": "↻ Refresh", "btn.reglages": "⚙ Settings…", "btn.reglages.title": "Settings",
    "update.installing": "Install {version}",
    "update.title": "Version {version} published. Download, stop and restart are automatic.",
    "update.updating": "Updating…",
    "update.progress": "Updating to {version}: downloading, then restarting…",
    "passages.updated": "updated {heure}",
    "passages.new.one": " · {n} new clip, click Refresh",
    "passages.new.many": " · {n} new clips, click Refresh",
    "auth.title": "Blink login", "auth.title.2fa": "Two-step verification",
    "auth.hint": "The password is only used to open the session; only the tokens are stored, never the password.",
    "auth.hint.2fa": "Blink just sent a code. Enter it to finish logging in.",
    "auth.email": "Email address", "auth.password": "Password",
    "auth.show": "Show", "auth.hide": "Hide",
    "auth.show.aria": "Show password", "auth.hide.aria": "Hide password",
    "auth.code": "Code received by SMS or email",
    "auth.cancel": "Cancel", "auth.ok": "Log in", "auth.validate": "Confirm",
    "auth.connecting": "Signing in…", "auth.failed": "Login failed.",
    "reglages.title": "Settings",
    "reglages.autostart": "Start monitoring at login",
    "reglages.autostart.title": "Starts the web server and clip processing at login, in the background — does not open this page by itself",
    "reglages.auto": "Automatic page refresh",
    "reglages.auto.title": "Reload the list as soon as clips arrive",
    "reglages.showOut": "Show discarded clips",
    "reglages.serveur": "Server port",
    "reglages.storageDir": "Data folder",
    "reglages.storageDir.placeholder": "C:/path/to/the/folder",
    "reglages.storageDir.hint": "Does not move clips or the Blink session already present at the old location: do it yourself if you change this path. Empty = default location, next to the executable.",
    "reglages.storageDir.browse": "Browse…",
    "reglages.storageDir.browse.unavailable": "Folder picker unavailable on this machine: type the path directly.",
    "reglages.cadence": "Camera polling interval",
    "reglages.usb": "USB (minutes)", "reglages.cloud": "Cloud (minutes)",
    "reglages.video": "Video", "reglages.timestamp": "Burn the date and time into the image",
    "reglages.timezone": "Time zone",
    "reglages.archivage": "Per-camera time-based video creation",
    "reglages.downloadAuto": "Download clips automatically",
    "reglages.downloadAuto.hint": "Unchecked, no clip is fetched or stored anymore: useful to keep only the live view. The cadences below then have no effect.",
    "reglages.mergeJour": "Daily",
    "reglages.mergeSemaine": "Weekly", "reglages.mergeMois": "Monthly",
    "reglages.archivage.hint": "Weekly and Monthly are assembled from the Daily: unchecking \u201cDaily\u201d also disables the other two.",
    "reglages.alertes": "Mute alerts",
    "reglages.suppressionAuto": "Automatic deletion after download",
    "reglages.hint": "Settings only take effect on restart: \u201cApply\u201d saves and restarts. Changing the port redirects this page to the new address.",
    "reglages.apply": "Apply", "reglages.restarting": "Restarting…",
    "reglages.restarting.settings": "Restarting with the new settings…",
    "reglages.portchange": "Port changed: redirecting to {url} once the shutdown is confirmed…",
    "reglages.stop": "Stop camera monitoring", "reglages.close": "Close",
    "reglages.error.cadence": "Intervals must be at least 1 minute.",
    "reglages.error.port": "The port must be between 1 and 65535.",
    "reglages.error.timezone": "The time zone cannot be empty.",
    "stop.stopping": "Stopping…",
    "stop.stopped": "blink2video is stopped. Restart the application to resume.",
    "sourdine.loading": "Loading…", "sourdine.unavailable": "Camera list unavailable.",
    "sourdine.none": "No known camera yet.",
    "suppressionAuto.loading": "Loading…",
    "suppressionAuto.unavailable": "Camera list unavailable.",
    "suppressionAuto.none": "No known camera yet.",
    "suppressionAuto.hint": "Once a clip is successfully downloaded, it is deleted from its source (USB drive or subscription cloud, depending on the camera).",
    "phase.download_clips": "Downloading clips",
    "phase.prepare_clips": "Preparing clips",
    "phase.assemble_videos": "Assembling videos",
    "phase.update_download": "Downloading the update ({mo} MB)",
    "phase.update_install": "Installing the update",
    "phase.step_download": "Downloading", "phase.step_merge": "Merging",
    "phase.cloud_section": "Subscription cloud",
    "phase.usb_section": "Local storage: {hub}",
    "live.querying": "Querying the Blink system…",
    "live.count": "{n} camera(s) · {m} armed",
    "system.armed": "System armed", "system.disarmed": "System disarmed",
    "camera.offline": "OFFLINE", "camera.noeffect": "no effect, system disarmed",
    "camera.detection.on": "Detection on", "camera.detection.off": "Detection off",
    "camera.wake": "Wake", "camera.waking": "Waking…",
    "camera.wake.title": "Wakes the camera now (takes a photo). Uses a bit of battery, up to 2 minutes.",
    "camera.battery": "battery {v}", "camera.wifi": "Wi-Fi {v} dBm",
    "camera.lfr": "module link {v}", "camera.measured.at": "measured at {v}",
    "camera.measured.on": "measured on {v}", "camera.firmware": "firmware {v}",
    "camera.noclips": "no clip retrieved", "camera.clipssource": "clips: {v}",
    "camera.none": "—",
    "watch.live": "View live", "watch.retry": "Retry", "watch.stop": "Stop",
    "watch.waking": "Waking the camera…", "watch.waking.seconds": "Waking the camera… {s} s",
    "watch.waking.slow": "Waking the camera… {s} s (a battery camera is slower)",
    "watch.waking.mse": "Waking the camera… (MSE)", "watch.reconnecting": "Reconnecting…",
    "live.fullscreen.title": "Expand fullscreen",
    "live.fullscreen.title.exit": "Exit fullscreen",
    "watch.noimage": "No image received. The camera did not respond.",
    "watch.refused": "The stream was refused by the server.",
    "watch.refused.code": "The stream was refused by the server ({code}).",
    "watch.refused.retry": "Stream refused. A previous live view may still be closing: try again in a few seconds.",
    "watch.codec.unsupported": "Codec not supported by this browser: {codec}",
    "command.sending": "Sending command…",
    "clips.none.filtered": "No clip matches this filter.",
    "clips.none.ever": "No clip retrieved yet.<br>Download is already running in the background (USB every 10 min, cloud every minute): clips will appear here on their own. Check that a USB drive is plugged into the module: without it, recordings only go to the Blink subscription cloud, which this tool does not read.",
    "clips.window": "{m}/{total} clips",
    "range.title": "Period",
    "range.today": "Today (24h)", "range.week": "This week (7d)",
    "range.month": "This month", "range.2months": "Last 2 months",
    "range.all": "All history", "range.custom": "Custom period",
    "range.custom.depuis": "from {v}", "range.custom.jusqua": "until {v}",
    "range.custom.hint": "Or a precise range, down to the hour:",
    "filtre.button": "🔍 Filter", "filtre.button.title": "Filter",
    "filtre.title": "Filter", "filtre.camera": "Camera",
    "range.from": "From", "range.to": "to", "range.apply": "Filter",
    "videos.count": "{n} video(s) · {duree} total",
    "videos.none": "No assembled video. Run a refresh.",
    "videos.download": "Download",
    "clip.resume": "Resume", "clip.discard": "Discard",
    "clip.discard.title": "Remove this clip from the assembled videos (daily, weekly, monthly). The downloaded copy stays on disk.",
    "clip.resume.title": "Include this clip in future assemblies again.",
    "clip.deleteSource": "Delete",
    "clip.deleteSource.pending": "Deleting…",
    "clip.deleteSource.title": "Delete this clip from its source (USB drive or subscription cloud). The copy already downloaded here is not affected. Can take up to a minute for USB.",
    "selection.apply": "✓ Apply ({n})",
    "selection.confirm.suppression": "{n} clip(s) will be deleted from their source (USB drive or subscription cloud). Copies already downloaded here are not affected. Continue?",
    "selection.partial": "{n} deletion(s) failed or found nothing to delete (already removed elsewhere). The rest of the selection was applied.",
    "refresh.starting": "Starting…", "refresh.errors": "Finished with errors",
    "refresh.disconnected": "\\nConnection lost.\\n",
  },
};
let _lang = "fr";
function t(k) { return (I18N[_lang] && I18N[_lang][k]) || I18N.fr[k] || k; }
function tf(k, v) {
  let s = t(k);
  for (const p in (v || {})) s = s.split("{" + p + "}").join(v[p]);
  return s;
}
function detectLang() {
  return (navigator.language || "en").toLowerCase().startsWith("fr") ? "fr" : "en";
}
function applyI18n() {
  document.documentElement.lang = _lang;
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const v = t(el.dataset.i18n); if (v) el.textContent = v;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    const v = t(el.dataset.i18nPlaceholder); if (v) el.placeholder = v;
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    const v = t(el.dataset.i18nTitle); if (v) el.title = v;
  });
  document.querySelectorAll("[data-lang-btn]").forEach((b) =>
    b.classList.toggle("active", b.dataset.langBtn === _lang));
}
function setLang(code, persist) {
  _lang = code === "en" ? "en" : "fr";
  applyI18n();
  // Le bouton « afficher/masquer » le mot de passe suit son propre état
  // (masqué ou non), qu'applyI18n ne connaît pas : ré-appliqué ici plutôt
  // que par data-i18n, qui écraserait « Masquer » par « Afficher » si le
  // mot de passe était déjà visible au moment du changement de langue.
  const pass = $("pass");
  if (pass) {
    const masque = pass.type === "password";
    $("passToggle").textContent = t(masque ? "auth.show" : "auth.hide");
    $("passToggle").setAttribute("aria-label", t(masque ? "auth.show.aria" : "auth.hide.aria"));
  }
  // Rendu par JS plutôt que par data-i18n : reconstruire pour que la langue
  // s'applique immédiatement, sans attendre le prochain événement qui
  // déclencherait normalement ce rendu. fill() gère un tableau vide sans
  // problème (l'option « tout » reste posée), donc pas de garde ici.
  if (typeof fill === "function") {
    fill($("camera"), data.cameras || [], t("filter.allcameras"),
         (nom) => [nom, (data.models || {})[nom]].filter(Boolean).join(" · "));
  }
  if (typeof render === "function" && data.clips) render();
  if (typeof renderLive === "function" && system) renderLive();
  // Un direct actif gèle la grille (voir renderLive()) : le bouton plein
  // écran déjà posé survit donc au changement de langue sans se refaire,
  // et doit être retraduit ici plutôt que de rester dans l'ancienne langue.
  if (typeof syncExpandButtons === "function") syncExpandButtons();
  // #sourdineListe porte data-i18n="sourdine.loading" en repli HTML :
  // applyI18n() vient d'écraser ses cases à cocher réelles par ce texte de
  // chargement si le panneau est ouvert pendant la bascule de langue.
  // Reconstruire immédiatement plutôt que de laisser la liste figée ainsi
  // jusqu'à la prochaine ouverture du panneau.
  if (typeof chargerSourdine === "function" && $("reglages")?.open) chargerSourdine();
  if (typeof chargerSuppressionAuto === "function" && $("reglages")?.open) chargerSuppressionAuto();
  if (persist) localStorage.setItem("lang", _lang);
  // Envoyé à chaque appel, pas seulement un choix explicite (persist) :
  // le menu du systray (tray.py) lit cette valeur pour s'afficher dans la
  // même langue que la page, y compris quand elle vient de detectLang()
  // et n'a jamais été choisie à la main. Best-effort, jamais bloquant : une
  // page ouverte hors-ligne ou un onglet d'arrière-plan ne doit pas faire
  // échouer l'affichage.
  fetch("/api/lang", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ lang: _lang }) }).catch(() => {});
}

function fill(select, values, all, label) {
  const kept = select.value;
  select.innerHTML = `<option value="">${all}</option>` +
    values.map((v) => `<option value="${v}">${label ? label(v) : v}</option>`).join("");
  if (values.includes(kept)) select.value = kept;
}

function visible() {
  return data.clips.filter((c) =>
    (!$("camera").value || c.camera === $("camera").value) &&
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
  $("outLabel").hidden = !clips;
  // Même périmètre que la caméra avant la refonte : Clips, Journalières,
  // Hebdomadaires, Mensuelles s'y filtrent toutes, seul Direct n'en a pas
  // l'usage. La période, elle, ne vaut que pour Clips (/api/clips) : le
  // reste ne la lit jamais.
  $("filtreButton").hidden = kind === "live";
  $("periodeSection").hidden = !clips;
  $("filtreResume").textContent = clips ? resumeFiltre() : "";
  // Le décompte n'a de sens que pour Clips ; renderClips() le repose à
  // chaque rendu, mais quitter cette vue doit l'effacer, pas le laisser
  // périmé derrière une autre vue.
  if (!clips) $("filtreCompte").textContent = "";
  if (kind === "live") return renderLive();
  return clips ? renderClips() : renderVideos(kind);
}

// --- direct et armement ----------------------------------------------------
let system = null;

async function loadSystem(force) {
  if (system && !force) return renderLive();
  // La vue peut avoir changé entre le déclenchement de cet appel et sa
  // résolution (bascule rapide vers Clips, ou déclenchement précoce au
  // chargement avant que la vue par défaut ne soit posée) : ne toucher au
  // DOM que si Direct est encore affiché, sinon la réponse tardive
  // écraserait une liste de clips déjà à l'écran sans que le menu déroulant
  // ne le laisse deviner.
  if ($("view").value === "live") {
    $("list").innerHTML = `<p class="empty">${t("live.querying")}</p>`;
    $("count").textContent = "";
  }
  try {
    system = await (await fetch("/api/system")).json();
  } catch (error) {
    system = { error: String(error) };
  }
  if ($("view").value === "live") renderLive();
}

// Un calcul lancé par les boucles de fond, hors de cette page : le
// téléchargement et l'assemblage publient leur avancement dans un fichier, seul
// moyen pour la page d'apprendre que la machine travaille. Tant qu'il tourne,
// le bouton reste inactif : un second calcul attendrait le même verrou, sans
// rien avancer.
let travailEnCours = false;
let actualisationLocale = false;

// Le serveur ne connaît jamais la langue affichée (choix propre à chaque
// onglet, en localStorage) : un libellé de phase arrive donc toujours en
// français, accompagné d'une clé stable quand une traduction existe. Clé
// absente ou inconnue de ce dictionnaire : le texte reçu reste affiché tel
// quel plutôt qu'une chaîne vide, qui masquerait un travail réellement en
// cours (ex. bug vécu en vrai : « Téléchargement des clips » figé en
// français quelle que soit la langue choisie).
function libellePhase(cle, texteBrut, valeurs) {
  if (!cle || !((I18N[_lang] && I18N[_lang][cle]) || I18N.fr[cle])) return texteBrut;
  return valeurs ? tf(cle, valeurs) : t(cle);
}

function montrerTravail(travail) {
  if (actualisationLocale) return;    // notre propre barre parle déjà
  const actif = !!(travail && travail.quoi);
  if (!actif) {
    if (travailEnCours) { $("work").classList.remove("on"); rechargerEnArrierePlan(); }
    travailEnCours = false;
    $("refresh").disabled = false;
    return;
  }
  travailEnCours = true;
  $("refresh").disabled = true;
  $("work").classList.add("on");
  const total = travail.total || 0;
  const fait = travail.fait || 0;
  const quoi = travail.cle === "phase.update_download"
    ? libellePhase(travail.cle, travail.quoi, { mo: Math.round(total) })
    : libellePhase(travail.cle, travail.quoi);
  if (total) {
    $("bar").max = total;
    $("bar").value = fait;
    $("phase").textContent = `${quoi} ${Math.min(fait + 1, total)}/${total}`;
  } else {
    $("bar").removeAttribute("value");
    $("phase").textContent = quoi;
  }
}

// Une version publiée plus récente que celle qui tourne : le bouton apparaît,
// et il fait tout, du téléchargement à la relance. Pendant l'opération le
// serveur s'arrête et revient : la page attend son retour, puis se recharge.
function montrerMaj(neuve) {
  const bouton = $("update");
  bouton.hidden = !(neuve && neuve.version);
  if (bouton.hidden || bouton.dataset.encours) return;
  bouton.textContent = tf("update.installing", { version: neuve.version });
  bouton.title = tf("update.title", { version: neuve.version });
}

$("update").onclick = async () => {
  const bouton = $("update");
  bouton.dataset.encours = "1";
  bouton.disabled = true;
  bouton.textContent = t("update.updating");
  const reponse = await fetch("/api/update", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: "{}" });
  const resultat = await reponse.json();
  if (resultat.error) {
    alert(resultat.error);
    bouton.disabled = false;
    delete bouton.dataset.encours;
    return;
  }
  $("phase").textContent = tf("update.progress", { version: resultat.version });
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

let dernierRechargementAuto = 0;

// Rechargement déclenché par l'arrière-plan (nouveau clip détecté, ou fin
// d'un calcul) plutôt que par un clic explicite : jamais plus d'une fois
// toutes les 60 secondes, le rythme normal de veille de veiller(). Pendant
// un gros lot en cours de traitement, chaque nouveau clip détecté ou chaque
// bascule de travailEnCours pouvait sinon redéclencher un rechargement
// complet de la grille (toutes les vignettes vidéo détruites et
// reconstruites), perçu comme un clignotement (constaté en réel, 2026-08-27).
function rechargerEnArrierePlan() {
  const maintenant = Date.now();
  if (maintenant - dernierRechargementAuto < 60000) return;
  dernierRechargementAuto = maintenant;
  load();
}

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
  //
  // Face à total_known (le vrai total du registre, jamais borné par le
  // filtre actif), jamais data.clips.length : celui-ci ne compte que ce que
  // le filtre courant affiche (une caméra, une période étroite…), pas tout
  // ce qui est connu. Comparer le total réel à un sous-ensemble filtré
  // annonçait des centaines de « nouveaux » clips qui n'avaient rien de
  // nouveau (constaté en réel, 2026-08-27).
  const arrives = (data && data.total_known !== undefined)
    ? Math.max(0, (etat.clips || 0) - data.total_known) : 0;
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
    rechargerEnArrierePlan();
    return;
  }
  const nouveaux = arrives
    ? tf(arrives > 1 ? "passages.new.many" : "passages.new.one", { n: arrives })
    : "";
  $("passages").textContent =
    tf("passages.updated", { heure: vus[plusRecent].slice(11, 16) }) + nouveaux;
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
  $("count").textContent = tf("live.count", { n: cameras, m: armed });

  $("list").innerHTML = system.systems.map((s) => `
    <h2>
      ${s.name}
      <span class="sub tiny">${[s.module,
        s.module_firmware ? tf("camera.firmware", { v: s.module_firmware }) : null,
        s.module_serial].filter(Boolean).join(" · ")}</span>
      <button class="act ${s.armed ? "in" : "out"}"
              onclick="setArmed('system', '${s.name}', ${!s.armed})">
        ${s.armed ? t("system.armed") : t("system.disarmed")}
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
    c.battery ? tf("camera.battery", { v: c.battery }) + (num(c.battery_signal) ? ` (${c.battery_signal})` : "") : null,
    num(c.temperature) ? `${c.temperature.toFixed(1).replace(".", ",")} °C` : null,
    num(c.wifi) ? tf("camera.wifi", { v: c.wifi }) : null,
    num(c.lfr) ? tf("camera.lfr", { v: c.lfr }) : null,
  ].filter(Boolean).join(" · ");
  const date = c.measured_at
    ? (c.measured_at.includes("à") ? tf("camera.measured.on", { v: c.measured_at })
                                   : tf("camera.measured.at", { v: c.measured_at }))
    : null;
  const details = [
    c.offline ? t("camera.offline") : null,
    releve || null,
    date,
    c.armed && !systemArmed ? t("camera.noeffect") : null,
  ].filter(Boolean).join(" · ");
  return `<div class="card ${c.offline ? "out" : ""}">
    <div class="live" id="live-${cssId(c.name)}">${repos(c.name, t("watch.live"))}</div>
    <div class="meta">
      <div>
        <div class="time">${c.name}</div>
        <div class="sub">${details || t("camera.none")}</div>
        <div class="sub tiny">${[c.model,
          c.firmware ? tf("camera.firmware", { v: c.firmware }) : null, c.serial,
          c.clips_source ? tf("camera.clipssource", { v: c.clips_source }) : t("camera.noclips"),
        ].filter(Boolean).join(" · ")}</div>
      </div>
      <button class="act ${c.armed ? "in" : "out"}"
              onclick="setArmed('camera', '${c.name}', ${!c.armed})">
        ${c.armed ? t("camera.detection.on") : t("camera.detection.off")}
      </button>
      <button class="act grouped" title="${t("camera.wake.title")}"
              onclick="reveillerCamera('${c.name}', event)">${t("camera.wake")}</button>
    </div>
  </div>`;
}

const cssId = (name) => name.replace(/[^\\w-]/g, "_");

// Un direct qui échoue doit rendre son bouton d'origine : laisser « Arrêter »
// laisserait croire qu'un flux tourne, et il n'y aurait plus aucun moyen de
// relancer. Retirer la balise <img> ferme au passage la connexion restée
// ouverte côté serveur.
function failWatch(name, message) {
  const box = $("live-" + cssId(name));
  box.innerHTML = repos(name, t("watch.retry")) + `<p class="hint overlay">${message}</p>`;
}

// Ni <img> (MJPEG) ni la balise <video> du direct MSE ne portent l'attribut
// controls (un scrubber n'aurait aucun sens sur un flux sans fin) : le
// plein écran ne peut donc pas venir gratuitement du navigateur comme pour
// les clips enregistrés. Bouton dédié plutôt qu'un clic sur toute la case :
// essayé d'abord, jugé peu explicite à l'usage.
function toggleFullscreen(name) {
  if (document.fullscreenElement || document.webkitFullscreenElement) {
    (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    return;
  }
  const box = $("live-" + cssId(name));
  (box.requestFullscreen || box.webkitRequestFullscreen).call(box);
}

function stopWatch(name) {
  // La case peut avoir disparu sous nos pieds (actualisation de la vue
  // pendant le direct) : la remise au repos est cosmétique, mais couper les
  // flux ci-dessous ne doit jamais en dépendre.
  const box = $("live-" + cssId(name));
  if (box) box.innerHTML = repos(name, t("watch.live"));
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
            let message = tf("watch.refused.code", { code: response.status });
            try {
              const info = await (await fetch("/api/live-error")).json();
              if (info.camera === name && info.message) message = info.message;
            } catch (error) { /* on garde le message générique */ }
            throw new Error(message);
          }
          const codec = response.headers.get("X-Codec") || "avc1.42E01E";
          const mimeType = `video/mp4; codecs="${codec}"`;
          if (!MediaSource.isTypeSupported(mimeType)) {
            throw new Error(tf("watch.codec.unsupported", { codec }));
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
     <button class="watch stop" data-i18n="watch.stop" onclick="stopWatch('${name}')">${t("watch.stop")}</button>
     ${expandBtn(name)}`;
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
      ? t("watch.waking.mse") : t("watch.reconnecting");
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
     <button class="watch" onclick="watchMse('${name}')">${libelle}</button>
     ${expandBtn(name)}`;
}

// Factorisé : posé à la fois ici (repos, y compris l'état d'échec qui
// réutilise repos()) et dans watchMse() une fois le flux lancé, pour rester
// visible dans tous les états plutôt que d'apparaître seulement en cours de
// lecture.
function expandBtn(name) {
  // Icône seule, jamais de texte : un bouton neuf n'est jamais encore
  // l'élément plein écran courant, donc l'état "entrer" est toujours le bon
  // à la création. syncExpandButtons() corrige l'icône/le libellé ensuite,
  // au changement de langue comme au passage en/hors plein écran.
  return `<button class="watch expand" onclick="toggleFullscreen('${name}')"
                   title="${t("live.fullscreen.title")}"
                   aria-label="${t("live.fullscreen.title")}">⛶</button>`;
}

function syncExpandButtons() {
  const actif = document.fullscreenElement || document.webkitFullscreenElement || null;
  document.querySelectorAll(".watch.expand").forEach((b) => {
    const estActif = b.closest(".live") === actif;
    b.textContent = estActif ? "×" : "⛶";
    const libelle = t(estActif ? "live.fullscreen.title.exit" : "live.fullscreen.title");
    b.title = libelle;
    b.setAttribute("aria-label", libelle);
  });
}
document.addEventListener("fullscreenchange", syncExpandButtons);
document.addEventListener("webkitfullscreenchange", syncExpandButtons);

async function setArmed(scope, name, armed) {
  $("count").textContent = t("command.sending");
  const answer = await fetch("/api/arm", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope, name, armed }),
  });
  const result = await answer.json();
  if (result.error) { alert(result.error); return loadSystem(true); }
  system = result;
  renderLive();
}

// Contrairement a setArmed(), peut prendre jusqu'a 2 minutes (voir
// reveiller_camera cote serveur) : le bouton se desactive pendant l'attente
// plutot que de laisser croire qu'un second clic accelererait quoi que ce
// soit. Le try/finally, pas juste le chemin normal, couvre aussi le cas ou
// renderLive() est gele (direct actif ailleurs, voir 28.20) et ne
// remplace donc jamais ce bouton par un neuf.
async function reveillerCamera(name, event) {
  const bouton = event.target;
  const libelle = bouton.textContent;
  bouton.disabled = true;
  bouton.textContent = t("camera.waking");
  try {
    const answer = await fetch("/api/reveiller", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const result = await answer.json();
    if (result.error) { alert(result.error); return loadSystem(true); }
    system = result;
    renderLive();
  } finally {
    bouton.disabled = false;
    bouton.textContent = libelle;
  }
}

function renderClips() {
  const clips = visible();
  // Pas de décompte ici : les clips sont sous les yeux, et trois nombres de
  // plus en haut de page ne disent rien qu'on cherchait.
  $("count").textContent = "";

  // À côté du résumé du filtre plutôt que dans la liste : un texte qui décrit
  // ce qui est affiché reste avec le reste de ce qui décrit le filtre, pas
  // mélangé aux résultats eux-mêmes qui défilent.
  $("filtreCompte").textContent = data.filtered
    ? tf("clips.window", { m: data.clips.length, total: data.total_known })
    : "";

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
      ? `<p class="empty">${t("clips.none.filtered")}</p>`
      : `<p class="empty">${t("clips.none.ever")}</p>`;
    return;
  }
  const days = [...new Set(clips.map((c) => c.day))];
  $("list").innerHTML = days.map((day) => `
    <h2>${day}</h2>
    <div class="grid">${clips.filter((c) => c.day === day).map(card).join("")}</div>
  `).join("");
}

function renderVideos(kind) {
  const items = (videos[kind] || [])
    .filter((v) => !$("camera").value || v.camera === $("camera").value);
  const total = items.reduce((sum, v) => sum + v.duration, 0);
  $("count").textContent = items.length
    ? tf("videos.count", { n: items.length, duree: duration(total) })
    : "";
  if (!items.length) {
    $("list").innerHTML = `<p class="empty">${t("videos.none")}</p>`;
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
      <a class="act" href="/media/${url}" download>${t("videos.download")}</a>
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
      <label class="act" title="${t("clip.discard.title")}">
        <input type="checkbox" ${c.excludedStaged ? "checked" : ""}
               onchange="stagerExclusion('${c.identity}', this.checked)">
        ${t("clip.discard")}
      </label>
      ${c.sourceDeleted || (data.suppressionAuto || []).includes(c.camera) ? "" : `
      <label class="act" title="${t("clip.deleteSource.title")}">
        <input type="checkbox" ${c.supprimerStaged ? "checked" : ""}
               onchange="stagerSuppression('${c.identity}', this.checked)">
        ${t("clip.deleteSource")}
      </label>`}
    </div>
  </div>`;
}

// Le filtre (caméra + période) survit d'une visite à l'autre : quelqu'un qui
// ne veut voir que les clips de la semaine ne doit pas refaire ce choix à
// chaque ouverture de la page. Défaut « tout l'historique » tant que rien
// n'a jamais été choisi (constaté en réel, 2026-08-27).
const CLE_FILTRE = "blink2video.filtre";

function sauvegarderFiltre() {
  try {
    localStorage.setItem(CLE_FILTRE, JSON.stringify(
      { camera: $("camera").value, plage: plageClips }));
  } catch (erreur) { /* stockage indisponible (navigation privée…) : tant pis */ }
}

function restaurerFiltre() {
  try {
    const brut = localStorage.getItem(CLE_FILTRE);
    return brut ? JSON.parse(brut) : null;
  } catch (erreur) {
    return null;
  }
}

const _filtrePersiste = restaurerFiltre();
let plageClips = _filtrePersiste?.plage || { preset: "all" };
// Choix en cours dans le panneau, appliqué seulement au clic sur Filtrer -
// tant qu'aucun préréglage ni plage personnalisée n'a été retouché dans
// cette ouverture du panneau, Filtrer ne fait que reprendre plageClips.
let plageEnAttente = null;
// La caméra restaurée ne peut être posée qu'une fois la vraie liste connue
// (premier fill(), voir load()) : un <select> vide ignore toute valeur qu'on
// lui donne avant d'avoir ses <option>.
let _filtreCameraAppliquee = false;

function paramsPourPlage() {
  const params = new URLSearchParams();
  if (plageClips.preset === "all") params.set("all", "1");
  else if (plageClips.preset) params.set("preset", plageClips.preset);
  else {
    if (plageClips.depuis) params.set("depuis", plageClips.depuis);
    if (plageClips.jusqua) params.set("jusqua", plageClips.jusqua);
  }
  const s = params.toString();
  return s ? `?${s}` : "";
}

// Deux load() peuvent se chevaucher (clics rapprochés entre préréglages, ou
// un load() de fond pendant qu'un autre tourne encore) : sans annulation de
// celui d'avant, une requête plus lente mais lancée plus tôt (souvent celle
// du chargement initial, sur « ce mois-ci ») pouvait répondre après une plus
// rapide et réappliquer des données périmées par-dessus - la sélection
// paraissait alors bloquée sur ce mois-ci, ou la grille rendait deux fois
// coup sur coup (clignotement). AbortController est le mécanisme standard
// pour ça, pas une invention locale (constaté en réel, 2026-08-27).
let __chargementEnCours = null;

async function load() {
  __chargementEnCours?.abort();
  const controleur = new AbortController();
  __chargementEnCours = controleur;
  let answer, videoAnswer;
  try {
    [answer, videoAnswer] = await Promise.all([
      fetch(`/api/clips${paramsPourPlage()}`, { signal: controleur.signal }),
      fetch("/api/videos", { signal: controleur.signal }),
    ]);
  } catch (erreur) {
    if (erreur.name === "AbortError") return; // une requête plus récente a pris le relais
    throw erreur;
  }
  data = await answer.json();
  videos = await videoAnswer.json();
  if (data.error) { $("log").style.display = "block"; $("log").textContent = data.error; return; }
  // État de la sélection : à zéro à chaque rechargement, il reflète l'état
  // réel qu'on vient de relire, pas une intention encore en attente.
  for (const c of data.clips || []) {
    c.excludedStaged = c.excluded;
    c.supprimerStaged = false;
  }
  // Le modèle accompagne le nom ici, une fois, plutôt que sur chaque vignette.
  fill($("camera"), data.cameras, t("filter.allcameras"),
       (nom) => [nom, (data.models || {})[nom]].filter(Boolean).join(" · "));
  // La caméra restaurée ne peut être posée qu'une fois ; les fois suivantes,
  // fill() a déjà de quoi préserver seul la sélection en cours (voir sa
  // propre note sur `kept`).
  if (!_filtreCameraAppliquee) {
    _filtreCameraAppliquee = true;
    if (_filtrePersiste?.camera && data.cameras.includes(_filtrePersiste.camera)) {
      $("camera").value = _filtrePersiste.camera;
    }
  }
  render();
  majBoutonAppliquer();
}

// Un préréglage suffit à retrouver un incident récent (aujourd'hui, cette
// semaine…) ; la plage personnalisée, elle, vise une période précise à
// l'heure près, pour ne pas défiler tout l'historique d'une caméra toujours
// armée (signalé sur Reddit, 2026-08-27). Ni l'un ni l'autre ne s'applique
// tant que Filtrer n'a pas été cliqué : choisir un préréglage puis se
// raviser pour une caméra ne doit pas déjà avoir rechargé la grille une
// première fois pour rien.
function choisirPreset(nom) {
  plageEnAttente = { preset: nom };
  $("rangeFrom").value = ""; $("rangeTo").value = "";
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.toggle("primary", bouton.dataset.preset === nom);
  }
}

function choisirPlagePersonnalisee() {
  const depuis = $("rangeFrom").value, jusqua = $("rangeTo").value;
  plageEnAttente = (depuis || jusqua) ? { depuis, jusqua } : null;
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.remove("primary");
  }
}

function ouvrirFiltre() {
  plageEnAttente = null;
  for (const bouton of document.querySelectorAll("#filtre .presets button")) {
    bouton.classList.toggle("primary", bouton.dataset.preset === plageClips.preset);
  }
  $("rangeFrom").value = plageClips.depuis || "";
  $("rangeTo").value = plageClips.jusqua || "";
  $("filtre").showModal();
}

async function appliquerFiltre() {
  if (plageEnAttente) plageClips = plageEnAttente;
  sauvegarderFiltre();
  $("filtre").close();
  // Pas de message de chargement intermédiaire : /api/clips répond en
  // quelques millisecondes en local, trop vite pour se lire comme un
  // chargement - seulement comme un clignotement de toute la grille à
  // chaque changement de filtre (constaté en réel, 2026-08-27).
  await load();
}

// Une plage personnalisée affiche les dates réellement choisies plutôt
// qu'un « Période personnalisée » générique : c'est justement pour cibler
// une période précise qu'on l'a choisie, autant la voir sans rouvrir le
// panneau.
function libellePlagePersonnalisee() {
  const formater = (v) => {
    if (!v) return null;
    const [date, heure] = v.split("T");
    const [, mois, jour] = date.split("-");
    return `${jour}/${mois} ${heure || "00:00"}`;
  };
  const depuis = formater(plageClips.depuis);
  const jusqua = formater(plageClips.jusqua);
  if (depuis && jusqua) return `${depuis} → ${jusqua}`;
  if (depuis) return tf("range.custom.depuis", { v: depuis });
  if (jusqua) return tf("range.custom.jusqua", { v: jusqua });
  return t("range.custom");
}

// Résumé affiché dans l'en-tête à côté du bouton Filtre, pour savoir ce qui
// est actif sans rouvrir le panneau. La caméra y figure toujours, y compris
// le défaut silencieux « toutes caméras » - même logique que la période,
// déjà affichée même sur son propre défaut (« tout l'historique ») : les
// deux disent la vérité sur ce qui est montré, jamais seulement ce qui
// restreint quelque chose.
function resumeFiltre() {
  const morceaux = [];
  const option = $("camera").selectedOptions[0];
  morceaux.push(option ? option.textContent : t("filter.allcameras"));
  if ($("view").value === "clips") {
    morceaux.push(plageClips.preset ? t(`range.${plageClips.preset}`) : libellePlagePersonnalisee());
  }
  return morceaux.join(" · ");
}

// Écarter et Supprimer sont des cases, pas des actions immédiates : coup par
// coup, la suppression USB paierait à chaque clic le délai de régénération
// du manifeste par le Sync Module (jusqu'à une minute, voir AUDIT 28.73/75).
// Le bouton Appliquer traite tout le lot en un seul appel, une seule lecture
// de manifeste par Sync Module concerné plutôt qu'une par clip.
function stagerExclusion(identity, coche) {
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.excludedStaged = coche;
  majBoutonAppliquer();
}

function stagerSuppression(identity, coche) {
  const clip = data.clips.find((c) => c.identity === identity);
  if (clip) clip.supprimerStaged = coche;
  majBoutonAppliquer();
}

function calculerSelection() {
  const exclure = [], inclure = [], supprimer = [];
  for (const c of data.clips || []) {
    if (!!c.excludedStaged !== !!c.excluded) (c.excludedStaged ? exclure : inclure).push(c.identity);
    if (c.supprimerStaged && !c.sourceDeleted) supprimer.push(c.identity);
  }
  return { exclure, inclure, supprimer };
}

function majBoutonAppliquer() {
  const bouton = $("applyButton");
  if (!bouton) return;
  const { exclure, inclure, supprimer } = calculerSelection();
  const n = exclure.length + inclure.length + supprimer.length;
  bouton.hidden = n === 0;
  bouton.textContent = tf("selection.apply", { n });
}

async function appliquerSelection() {
  const { exclure, inclure, supprimer } = calculerSelection();
  if (!exclure.length && !inclure.length && !supprimer.length) return;
  if (supprimer.length && !confirm(tf("selection.confirm.suppression", { n: supprimer.length }))) return;
  const bouton = $("applyButton");
  bouton.disabled = true;
  bouton.textContent = t("clip.deleteSource.pending");
  try {
    const reponse = await fetch("/api/appliquer-selection", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exclure, inclure, supprimer }) });
    const resultat = await reponse.json();
    if (resultat.error) { alert(resultat.error); return; }
    // Écarter/Réintégrer : mise à jour optimiste, comme l'ancien toggle()
    // (AUDIT 28.33/28.75). Le registre s'écrit en tâche de fond côté
    // serveur pour ne jamais bloquer la réponse ; la relire tout de suite
    // ici la course parfois, avant que l'écriture n'ait fini (constaté par
    // Nico : « les écartés restent présents » jusqu'à un F5 manuel).
    for (const identity of exclure) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (clip) { clip.excluded = true; clip.excludedStaged = true; }
    }
    for (const identity of inclure) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (clip) { clip.excluded = false; clip.excludedStaged = false; }
    }
    // Supprimer : l'appel était synchrone côté serveur, le résultat est
    // donc fiable tout de suite, pas une supposition.
    let echecs = 0;
    for (const [identity, statut] of Object.entries(resultat.resultats || {})) {
      const clip = data.clips.find((c) => c.identity === identity);
      if (statut === "supprime" || statut === "deja_absent") {
        if (clip) { clip.sourceDeleted = true; clip.supprimerStaged = false; }
      } else {
        echecs++;
      }
    }
    if (echecs) alert(tf("selection.partial", { n: echecs }));
    render();
  } catch (erreur) {
    alert(String(erreur));
  } finally {
    bouton.disabled = false;
    majBoutonAppliquer();
  }
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
  $("authTitle").textContent = code ? t("auth.title.2fa") : t("auth.title");
  $("authHint").textContent = code ? t("auth.hint.2fa") : t("auth.hint");
  $("authOk").textContent = code ? t("auth.validate") : t("auth.ok");
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
  $("passToggle").textContent = t(masque ? "auth.hide" : "auth.show");
  $("passToggle").setAttribute("aria-label", t(masque ? "auth.hide.aria" : "auth.show.aria"));
};

$("authCancel").onclick = () => {
  $("auth").close();
  if (authResolve) { authResolve(false); authResolve = null; }
};

$("authOk").onclick = async () => {
  const code = !$("authCode").hidden;
  $("authOk").disabled = true;
  $("authError").textContent = t("auth.connecting");
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
    showAuth(code ? "2fa" : "creds", result.message || t("auth.failed"));
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
  let label = t("refresh.starting");
  $("phase").textContent = label;
  $("bar").removeAttribute("value");   // barre indéterminée tant qu'on ne sait pas

  const source = new EventSource("/api/refresh");
  source.onmessage = (message) => {
    const event = JSON.parse(message.data);
    if (event.phase) {
      label = event.phase_key === "phase.usb_section"
        ? libellePhase(event.phase_key, event.phase, { hub: event.phase_hub })
        : libellePhase(event.phase_key, event.phase);
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
      if (!event.ok) $("phase").textContent = t("refresh.errors");
      load();
      // « Actualiser » ne rapatriait que les clips : la batterie, la
      // température et le signal de chaque caméra restaient sur leur
      // dernière lecture, parfois vieille de plusieurs jours, tant qu'on
      // n'ouvrait pas soi-même l'onglet Direct (bug vécu en vrai : une
      // caméra affichait une mesure ancienne jusqu'à un rafraîchissement
      // manuel depuis l'appli officielle). loadSystem(true) force le même
      // passage que cette appli fait de son côté (blink.refresh(force=True),
      // qui relit vraiment chaque caméra, pas seulement le résumé du
      // compte - voir system_state() côté serveur).
      loadSystem(true);
    }
  };
  source.onerror = () => {
    source.close();
    $("work").classList.remove("on");
    button.disabled = false;
    actualisationLocale = false;
    $("log").textContent += t("refresh.disconnected");
  };
};

for (const id of ["view", "showOut"]) $(id).onchange = render;
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
// Reflète l'état réel du système (fichier de démarrage présent ou non), pas
// une préférence mémorisée côté page : deux installations d'un même profil
// navigateur ne doivent pas se faire croire l'état de l'autre.
fetch("/api/autostart").then((r) => r.json()).then((etat) => {
  $("autostart").checked = !!etat.actif;
}).catch(() => {});
$("autostart").onchange = async () => {
  const voulu = $("autostart").checked;
  $("autostart").disabled = true;
  try {
    const reponse = await fetch("/api/autostart", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ actif: voulu }),
    });
    const etat = await reponse.json();
    if (etat.error) alert(etat.error);
    $("autostart").checked = !!etat.actif;
  } catch (error) {
    alert(String(error));
    $("autostart").checked = !voulu;
  } finally {
    $("autostart").disabled = false;
  }
};

let portActuel = null;   // relu à chaque ouverture, comparé à l'envoi

$("reglagesButton").onclick = async () => {
  try {
    const reglages = await (await fetch("/api/reglages")).json();
    $("usbMinutes").value = reglages.usb_minutes;
    $("cloudMinutes").value = reglages.cloud_minutes;
    $("port").value = reglages.port;
    portActuel = reglages.port;
    $("storageDir").value = reglages.storage_dir;
    $("timestamp").checked = reglages.timestamp;
    $("timezone").value = reglages.timezone;
    $("mergeJour").checked = reglages.merge_jour;
    $("mergeSemaine").checked = reglages.merge_semaine;
    $("mergeMois").checked = reglages.merge_mois;
    appliquerDependanceMergeJour();
    $("downloadAuto").checked = reglages.download_auto;
    appliquerDependanceDownloadAuto();
  } catch (erreur) { /* les champs gardent leur dernière valeur affichée */ }
  chargerSourdine();
  chargerSuppressionAuto();
  $("reglages").showModal();
};
$("reglagesClose").onclick = () => $("reglages").close();

$("filtreButton").onclick = ouvrirFiltre;
$("filtreClose").onclick = () => $("filtre").close();
$("filtreApply").onclick = appliquerFiltre;
for (const bouton of document.querySelectorAll("#filtre .presets button")) {
  bouton.onclick = () => choisirPreset(bouton.dataset.preset);
}
$("rangeFrom").oninput = choisirPlagePersonnalisee;
$("rangeTo").oninput = choisirPlagePersonnalisee;

// Ouvre le sélecteur natif côté serveur (tkinter : voir /api/choisir-dossier)
// plutôt qu'un <input type="file" webkitdirectory> - celui-ci ne rend qu'un
// nom de dossier relatif au navigateur, jamais un chemin absolu utilisable
// par le serveur (restriction de vie privée du web, pas une limite de ce code).
$("storageDirBrowse").onclick = async () => {
  const bouton = $("storageDirBrowse");
  bouton.disabled = true;
  try {
    const reponse = await fetch("/api/choisir-dossier");
    const resultat = await reponse.json();
    if (resultat.error) {
      alert(t("reglages.storageDir.browse.unavailable"));
    } else if (resultat.path) {
      $("storageDir").value = resultat.path;
    }
  } catch (erreur) {
    alert(t("reglages.storageDir.browse.unavailable"));
  } finally {
    bouton.disabled = false;
  }
};

// Semaine et mois n'ont de sens que si la journalière tourne : décocher
// « jour » les grise et les décoche, plutôt que de laisser espérer un
// agrégat qui ne sera jamais construit faute de base.
function appliquerDependanceMergeJour() {
  const actif = $("mergeJour").checked;
  $("mergeSemaine").disabled = !actif;
  $("mergeMois").disabled = !actif;
  if (!actif) {
    $("mergeSemaine").checked = false;
    $("mergeMois").checked = false;
  }
}
$("mergeJour").onchange = appliquerDependanceMergeJour;

// Les cadences n'ont plus de sens si rien n'est téléchargé : grisées plutôt
// que retirées, pour retrouver la dernière valeur en recochant.
function appliquerDependanceDownloadAuto() {
  const actif = $("downloadAuto").checked;
  $("usbMinutes").disabled = !actif;
  $("cloudMinutes").disabled = !actif;
}
$("downloadAuto").onchange = appliquerDependanceDownloadAuto;

// Séparé du reste du panneau : contrairement aux cadences, au port ou au
// fuseau, la sourdine n'exige pas de redémarrage (watch relit son état à
// chaque passage), donc chaque case s'applique tout de suite, comme le
// bouton Écarter d'un clip.
async function chargerSourdine() {
  const conteneur = $("sourdineListe");
  conteneur.textContent = t("sourdine.loading");
  let etat;
  try {
    etat = await (await fetch("/api/sourdine")).json();
  } catch (erreur) {
    conteneur.textContent = t("sourdine.unavailable");
    return;
  }
  if (!etat.cameras.length) {
    conteneur.textContent = t("sourdine.none");
    return;
  }
  conteneur.innerHTML = "";
  for (const camera of etat.cameras) {
    const label = document.createElement("label");
    const case_ = document.createElement("input");
    case_.type = "checkbox";
    case_.checked = etat.ignored.includes(camera);
    case_.onchange = async () => {
      case_.disabled = true;
      try {
        const reponse = await fetch("/api/sourdine", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ camera, ignored: case_.checked }) });
        const resultat = await reponse.json();
        if (resultat.error) {
          alert(resultat.error);
          case_.checked = !case_.checked;
        }
      } catch (erreur) {
        alert(String(erreur));
        case_.checked = !case_.checked;
      } finally {
        case_.disabled = false;
      }
    };
    label.appendChild(case_);
    label.append(` ${camera}`);
    conteneur.appendChild(label);
  }
}

// Même motif que chargerSourdine() : chaque case s'applique tout de suite,
// pas de redémarrage. Liste distincte (issue GitHub #1) : seules les
// caméras vues sur la clé USB ont un sens ici, le cloud de l'abonnement
// n'est jamais concerné par cette suppression.
async function chargerSuppressionAuto() {
  const conteneur = $("suppressionAutoListe");
  conteneur.textContent = t("suppressionAuto.loading");
  let etat;
  try {
    etat = await (await fetch("/api/suppression-auto")).json();
  } catch (erreur) {
    conteneur.textContent = t("suppressionAuto.unavailable");
    return;
  }
  if (!etat.cameras.length) {
    conteneur.textContent = t("suppressionAuto.none");
    return;
  }
  conteneur.innerHTML = "";
  for (const camera of etat.cameras) {
    const label = document.createElement("label");
    const case_ = document.createElement("input");
    case_.type = "checkbox";
    case_.checked = etat.actives.includes(camera);
    case_.onchange = async () => {
      case_.disabled = true;
      try {
        const reponse = await fetch("/api/suppression-auto", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ camera, actif: case_.checked }) });
        const resultat = await reponse.json();
        if (resultat.error) {
          alert(resultat.error);
          case_.checked = !case_.checked;
        }
      } catch (erreur) {
        alert(String(erreur));
        case_.checked = !case_.checked;
      } finally {
        case_.disabled = false;
      }
    };
    label.appendChild(case_);
    label.append(` ${camera}`);
    conteneur.appendChild(label);
  }
}

// Même déroulé que le bouton de mise à jour : enregistrer, attendre que le
// serveur disparaisse puis revienne, recharger. Le verbe diffère (« restart »
// au lieu de « update ») puisqu'aucune nouvelle version n'est en jeu, mais
// c'est le même arrêt-puis-relance vu de la page. Si le port change, la page
// qui redémarre n'écoute plus à la même adresse : le sondage habituel (même
// origine) ne verrait jamais le retour, il faut viser la nouvelle adresse.
$("reglagesApply").onclick = async () => {
  const usb = parseInt($("usbMinutes").value, 10);
  const cloud = parseInt($("cloudMinutes").value, 10);
  const port = parseInt($("port").value, 10);
  if (!(usb >= 1) || !(cloud >= 1)) {
    alert(t("reglages.error.cadence"));
    return;
  }
  if (!(port >= 1 && port <= 65535)) {
    alert(t("reglages.error.port"));
    return;
  }
  const timezone = $("timezone").value.trim();
  if (!timezone) {
    alert(t("reglages.error.timezone"));
    return;
  }
  const bouton = $("reglagesApply");
  bouton.disabled = true;
  bouton.textContent = t("reglages.restarting");
  try {
    const storageDir = $("storageDir").value.trim();
    const timestamp = $("timestamp").checked;
    const mergeJour = $("mergeJour").checked;
    const mergeSemaine = $("mergeSemaine").checked;
    const mergeMois = $("mergeMois").checked;
    const downloadAuto = $("downloadAuto").checked;
    const reponse = await fetch("/api/reglages", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ usb_minutes: usb, cloud_minutes: cloud, port,
                             storage_dir: storageDir, timestamp, timezone,
                             merge_jour: mergeJour, merge_semaine: mergeSemaine,
                             merge_mois: mergeMois, download_auto: downloadAuto }) });
    const resultat = await reponse.json();
    if (resultat.error) {
      alert(resultat.error);
      bouton.disabled = false;
      bouton.textContent = t("reglages.apply");
      return;
    }
  } catch (erreur) {
    // Une erreur de validation arrive toujours en JSON propre, AVANT que le
    // serveur ne se tue pour redémarrer (voir resultat.error ci-dessus) :
    // si on arrive ici, c'est que la réponse a été coupée par ce
    // redémarrage lui-même, pas que la sauvegarde a échoué. Même principe
    // que le bouton Stop plus bas : on continue comme en cas de succès
    // plutôt que d'alarmer à tort sur une erreur réseau qui ne veut rien
    // dire ici.
  }
  bouton.disabled = false;
  bouton.textContent = t("reglages.apply");
  $("reglages").close();

  if (port !== portActuel) {
    // Un délai fixe se serait trompé de quelques secondes selon la charge
    // de la machine : on attend plutôt la confirmation que l'ancien
    // serveur (cette origine) a bien disparu, comme pour un redémarrage
    // ordinaire, avant de viser la nouvelle adresse.
    const nouvelleAdresse = `http://${location.hostname}:${port}/`;
    $("phase").textContent = tf("reglages.portchange", { url: nouvelleAdresse });
    $("bar").removeAttribute("value");
    $("work").classList.add("on");
    $("refresh").disabled = true;
    let parti = false;
    const attentePort = setInterval(async () => {
      try {
        await fetch("/api/status", { cache: "no-store" });
      } catch (erreur) {
        parti = true;
      }
      if (parti) {
        clearInterval(attentePort);
        // L'ancien a disparu ; le nouveau, déjà en cours de lancement,
        // a besoin d'un instant de plus pour se lier au port.
        setTimeout(() => { location.href = nouvelleAdresse; }, 3000);
      }
    }, 1000);
    setTimeout(() => clearInterval(attentePort), 900000);
    return;
  }

  $("phase").textContent = t("reglages.restarting.settings");
  $("bar").removeAttribute("value");
  $("work").classList.add("on");
  $("refresh").disabled = true;
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

$("stopButton").onclick = async () => {
  const bouton = $("stopButton");
  bouton.disabled = true;
  bouton.textContent = t("stop.stopping");
  try {
    await fetch("/api/stop", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (erreur) { /* la réponse peut ne pas arriver, l'arrêt est déjà lancé */ }
  document.body.innerHTML = `<p class="empty">${t("stop.stopped")}</p>`;
};

// Vue par défaut posée AVANT setLang() : celui-ci appelle render(), qui lit
// $("view").value pour décider quoi peindre. Sans cadre du navigateur pour
// distinguer une option "selected" ici, la valeur par défaut du <select>
// serait la première déclarée (Direct) - render() y déclencherait alors un
// appel réseau vers /api/system dont la réponse, arrivée en retard, écrase
// la liste de clips déjà affichée sans que le menu déroulant ne bouge (bug
// vécu en conditions réelles : la page « repassait en Direct toute seule »).
$("view").value = "clips";
// Override manuel mémorisé prioritaire ; sinon la langue du navigateur, comme
// au premier lancement de lidar2map. Placé ici, en fin de script : setLang()
// appelle render()/renderLive(), qui référencent des `let` déclarés plus haut
// (MSE_ABORT, actualisationLocale...) — appelé trop tôt, avant l'exécution de
// ces déclarations, ça lève ReferenceError (zone morte temporelle), comme vu
// en testant réellement au navigateur.
setLang(localStorage.getItem("lang") || detectLang(), false);

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

# Un jeton par processus (28.60/28.59), pas par requête : engendré une seule
# fois ici, au chargement du module, comme VERSION ci-dessus. uuid4 plutôt que
# secrets pour rester sur la génération de jeton déjà en usage ailleurs
# (runtime.py, verrou disque ; blink_auth.py, fichiers temporaires).
TOKEN = uuid.uuid4().hex
PAGE = PAGE.replace("__TOKEN__", TOKEN)


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

    # 127.0.0.1 et pas 0.0.0.0 par défaut : cet outil déplace des fichiers, il
    # n'a rien à faire sur le réseau local. BLINK_BIND permet d'y déroger,
    # nécessaire dans un conteneur Docker où 127.0.0.1 désignerait la boucle
    # locale du conteneur, injoignable depuis l'hôte même avec le port publié
    # (le réseau en pont route vers l'interface du conteneur, pas sa boucle
    # locale) : la frontière de sécurité reste alors posée par la publication
    # du port elle-même (voir docker-compose.yml, 127.0.0.1: en dur).
    bind = os.environ.get("BLINK_BIND", "127.0.0.1")

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
        server = Server((bind, args.port), Handler)
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
