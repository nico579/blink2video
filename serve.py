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
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import http.server
import ipaddress
import json
import mimetypes
import os
import queue
import re
import subprocess
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
import blink_webrtc
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
# /api/attente-module (plus bas) : combien de temps un client peut attendre
# une confirmation que MODULE_SLOT est libre avant d'être informé que ça
# prend plus longtemps que prévu. Un peu au-delà du plafond interne de
# _stop_stream (asyncio.wait_for(feed, timeout=20)) : pas de raison de
# couper avant que le nettoyage serveur lui-même abandonne.
ATTENTE_MODULE_MAX_SECONDS = 25

# Reglage de la page web (webrtc par defaut) depuis le 2026-09-03 - a
# remplace la variable d'environnement BLINK_DIRECT_WEBRTC, experimentale,
# une fois WebRTC valide en usage reel (BACKLOG.md). Lu une fois au
# demarrage, comme tous les autres reglages (port, fuseau...) : un
# changement redemarre deja le serveur (/api/reglages cote JS). Retombe sur
# MSE si aiortc n'est pas installe, meme "webrtc" choisi.
WEBRTC_ACTIF = (runtime.lire_reglages()["live_protocol"] == "webrtc") and blink_webrtc.DISPONIBLE


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


ETIQUETTES_SOURCE = {"usb": "local", "cloud": "cloud"}

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


def suppression_auto_choices(entrees: dict) -> list:
    """Caméras réglables, indexées par leur identité persistée et non leur nom."""
    choices = {}
    for entry in entrees.values():
        if not isinstance(entry, dict) or not entry.get("camera"):
            continue
        key = blink_registre.camera_setting_key_from_entry(entry)
        choice = choices.setdefault(key, {
            "key": key,
            "name": str(entry.get("camera") or "camera").strip() or "camera",
            "sources": set(),
            "hubs": set(),
        })
        choice["sources"].add(str(entry.get("source") or "usb"))
        if entry.get("hub"):
            choice["hubs"].add(str(entry["hub"]))
    result = []
    for choice in choices.values():
        result.append({
            "key": choice["key"],
            "name": choice["name"],
            "detail": " · ".join([
                " + ".join(ETIQUETTES_SOURCE.get(s, s)
                           for s in sorted(choice["sources"])),
                ", ".join(sorted(choice["hubs"])),
            ]).strip(" ·"),
        })
    return sorted(result, key=lambda item: (item["name"].casefold(), item["key"]))


def suppression_auto_keys() -> set:
    """Ignore les anciens noms ambigus jusqu'à leur migration explicite."""
    return {
        value for value in runtime.lire_suppression_auto()
        if value.startswith("camera-v2-")
    }


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


def camera_key(sync, name: str, camera) -> str:
    """Identifiant opaque et stable d'une caméra pour l'interface web."""
    attributes = getattr(camera, "attributes", None) or {}
    network_id = (
        getattr(camera, "network_id", None)
        or getattr(sync, "network_id", None)
        or ""
    )
    device_id = ""
    for value in (
        getattr(camera, "device_id", None),
        getattr(camera, "camera_id", None),
        attributes.get("device_id"),
        attributes.get("camera_id"),
        attributes.get("id"),
    ):
        if value not in (None, ""):
            device_id = str(value)
            break
    material = (
        ["device", str(network_id), device_id]
        if device_id
        else [
            "legacy", str(network_id), str(getattr(sync, "sync_id", "")),
            str(name).strip().casefold(),
        ]
    )
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"camera-{digest[:24]}"


def system_key(name: str, sync) -> str:
    material = [
        str(getattr(sync, "network_id", "")),
        str(getattr(sync, "sync_id", "")),
        str(name).strip().casefold(),
    ]
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"system-{digest[:24]}"


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
            "cameraKey": blink_registre.camera_setting_key_from_entry(entry),
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
        "suppressionAuto": sorted(suppression_auto_keys()),
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
LAST_LIVE_ERROR_LOCK = threading.Lock()


def _memoriser_erreur_direct(camera: str, message: str, status: int) -> None:
    """Publie d'un seul bloc l'échec que le navigateur viendra relire."""
    with LAST_LIVE_ERROR_LOCK:
        LAST_LIVE_ERROR.clear()
        LAST_LIVE_ERROR.update({
            "camera": camera, "message": message, "status": status,
        })


def _effacer_erreur_direct() -> None:
    """Une nouvelle tentative acceptée rend tout ancien échec caduc."""
    with LAST_LIVE_ERROR_LOCK:
        LAST_LIVE_ERROR.clear()


def _derniere_erreur_direct() -> dict:
    """Copie cohérente pour le thread HTTP qui sert /api/live-error."""
    with LAST_LIVE_ERROR_LOCK:
        return dict(LAST_LIVE_ERROR)


def _texte_stderr_ffmpeg(errors: list) -> str:
    """Normalise stderr, que subprocess livre en bytes dans ce pipeline.

    _drainer_stderr alimente `errors` ligne par ligne (diagnostic temporaire),
    plus un seul bloc comme avant : on rejoint tout, ça couvre les deux
    formes."""
    raw = b"".join(e for e in errors if isinstance(e, bytes))
    return raw.decode("utf-8", "replace").strip()[:300]


def _journal_direct(name: str, message: str) -> None:
    """Un journal indisponible ne doit jamais interrompre un direct.

    Commun a MSE et WebRTC (renomme le 2026-09-03 : le prefixe "direct-mse"
    laissait croire a une bascule vers MSE meme sur une ligne concernant
    WebRTC - source de confusion reelle, cf. BACKLOG.md). Le protocole en
    cause reste lisible dans le texte de chaque message ("webrtc"/"MSE"
    explicite), jamais dans ce prefixe generique. Ecrit aussi dans
    direct.log (runtime.ajouter_ligne) : le lancement normal tourne sous
    pythonw (autostart), donc sans console ou lire un print()."""
    horodatage = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    ligne = f"[direct] {horodatage} {name} : {message}"
    try:
        print(ligne, flush=True)
    except Exception:
        pass
    runtime.ajouter_ligne("direct.log", ligne)


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
        detail = str(error).strip()
        suffixe = f" : {detail}" if detail else ""
        return f"fin de flux : {type(error).__name__}{suffixe}"

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
            finished = threading.Event()

            async def run():
                try:
                    if self.blink is None:
                        self.session = blink_auth.session_http()
                        self.blink = await blink_auth.connect_saved(self.session)
                    if self.blink is None:
                        raise RuntimeError(
                            "Session Blink absente ou expirée. Reconnectez-vous "
                            "depuis le bouton Actualiser."
                        )
                    return await coroutine_factory(self.blink)
                finally:
                    finished.set()

            future = asyncio.run_coroutine_threadsafe(run(), loop)
            try:
                return future.result(timeout)
            except concurrent.futures.TimeoutError:
                # result(timeout) ne borne que l'attente du thread appelant :
                # sans annulation, la coroutine continue sur la boucle Blink et
                # peut armer une caméra ou supprimer un clip après que l'API a
                # déjà annoncé un échec. La conserver permet au moins d'arrêter
                # tout ce qui n'a pas encore été envoyé au service distant.
                future.cancel()
                try:
                    future.result(timeout=5)
                except (concurrent.futures.CancelledError,
                        concurrent.futures.TimeoutError):
                    pass
                # Le Future concurrent passe à « cancelled » dès que la
                # demande est transmise à la boucle, avant que le finally de
                # la coroutine ait nécessairement rendu ses ressources. Une
                # barrière distincte évite de libérer self.lock trop tôt.
                finished.wait(timeout=5)
                raise

    def forget(self):
        """Oublie la session courante : la prochaine demande se reconnectera."""
        with self.lock:
            self.blink = None
            if self.session is not None and self.loop is not None:
                asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
            self.session = None

    def find_camera(self, blink, identity: str):
        by_key = []
        by_name = []
        for sync in blink.sync.values():
            for camera_name, camera in (getattr(sync, "cameras", None) or {}).items():
                if camera_key(sync, camera_name, camera) == identity:
                    by_key.append((sync, camera))
                if camera_name.strip() == identity.strip():
                    by_name.append((sync, camera))
        if len(by_key) == 1:
            return by_key[0]
        if len(by_key) > 1:
            raise RuntimeError("Identifiant de caméra dupliqué dans le compte Blink.")
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise RuntimeError(
                f"Nom de caméra ambigu : {identity}. Utilisez son identifiant stable."
            )
        raise RuntimeError(f"Caméra inconnue : {identity}")

    def find_system(self, blink, identity: str):
        by_name = []
        for sync_name, sync in blink.sync.items():
            if system_key(sync_name, sync) == identity:
                return sync
            if sync_name.strip() == identity.strip():
                by_name.append(sync)
        if len(by_name) == 1:
            return by_name[0]
        if len(by_name) > 1:
            raise RuntimeError(f"Nom de système ambigu : {identity}")
        raise RuntimeError(f"Système inconnu : {identity}")

    def find_sync_module(self, blink, entry: dict):
        """Résout le module d'un clip par ses IDs persistés.

        Le support XR construit le vrai Sync Module depuis le homescreen : il
        peut donc être absent de ``blink.sync``, qui contient encore, selon les
        comptes, une Mini ou une Doorbell portant l'ID du périphérique. Le nom
        de caméra n'est conservé qu'en repli pour les anciennes entrées du
        registre dépourvues d'identifiants.
        """
        network_id = str(entry.get("network_id") or "")
        sync_id = str(entry.get("sync_id") or "")
        modules = [sync for _name, sync in blink_models.select_sync_modules(blink, None)]
        if network_id or sync_id:
            matches = [
                sync for sync in modules
                if (not network_id
                    or str(getattr(sync, "network_id", "")) == network_id)
                and (not sync_id
                     or str(getattr(sync, "sync_id", "")) == sync_id)
            ]
            if len(matches) == 1:
                return matches[0]
            if not matches:
                raise RuntimeError(
                    f"Sync Module introuvable (réseau {network_id or '?'}, "
                    f"module {sync_id or '?'})."
                )
            raise RuntimeError(
                f"Sync Module ambigu (réseau {network_id or '?'}, "
                f"module {sync_id or '?'})."
            )

        camera = str(entry.get("camera") or "").strip()
        sync, _camera = self.find_camera(blink, camera)
        return sync


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
    lui, la boîte peut s'ouvrir derrière le navigateur.

    Le propriétaire doit être réellement affiché (Show(), Opacity=0, hors
    écran) plutôt que juste construit avec WindowState='Minimized' sans
    jamais l'afficher : cette seconde forme, essayée d'abord, faisait
    entrer ShowDialog() dans un blocage silencieux (aucune erreur, aucune
    boîte visible, le processus PowerShell restait vivant indéfiniment -
    constaté en conditions réelles). -STA est déjà le mode par défaut de
    powershell.exe (vérifié : GetApartmentState() répond STA sans l'option),
    gardé explicite pour ne pas en dépendre silencieusement."""
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$proprietaire = New-Object System.Windows.Forms.Form; "
        "$proprietaire.ShowInTaskbar = $false; "
        "$proprietaire.Opacity = 0; "
        "$proprietaire.StartPosition = 'Manual'; "
        "$proprietaire.Location = New-Object System.Drawing.Point(-2000, -2000); "
        "$proprietaire.Size = New-Object System.Drawing.Size(1, 1); "
        "$proprietaire.Show(); "
        "$proprietaire.TopMost = $true; "
        "$dialogue = New-Object System.Windows.Forms.FolderBrowserDialog; "
        f"$dialogue.SelectedPath = '{initial.replace(chr(39), chr(39) * 2)}'; "
        "if ($dialogue.ShowDialog($proprietaire) -eq "
        "[System.Windows.Forms.DialogResult]::OK) "
        "{ [Console]::Out.Write($dialogue.SelectedPath) }"
    )
    resultat = runtime.lancer(
        ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
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
    hub: str | None = None
    ffmpeg: str = ""
    # Serveur temporaire du tout premier démarrage : les réglages sont
    # enregistrés sans lancer lui-même un restart. Le parent ``start`` attend
    # leur marqueur, arrête ce serveur, puis seulement alors crée les workers.
    initial_setup: bool = False
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
        # Host est fourni par le client et se forge avec curl : il ne constitue
        # pas une frontière réseau. Hors conteneur, seule une vraie adresse
        # cliente de boucle locale est admise. Le compose officiel passe par le
        # pont Docker ; son opt-in explicite reste sûr tant que le port hôte est
        # publié sur 127.0.0.1, comme dans docker-compose.yml.
        client = str(getattr(self, "client_address", ("127.0.0.1", 0))[0])
        try:
            boucle_locale = ipaddress.ip_address(client).is_loopback
        except ValueError:
            boucle_locale = False
        proxy_local = os.environ.get("BLINK_TRUSTED_LOOPBACK_PROXY") == "1"
        if not boucle_locale and not proxy_local:
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
        if self.headers.get("X-Blink-Token") == TOKEN:
            return True
        # EventSource, <video> et <img> ne permettent pas d'ajouter un en-tête
        # personnalisé. Leur URL porte donc le même secret ; no-referrer et la
        # politique same-origin ci-dessous empêchent sa fuite vers un tiers.
        valeurs = parse_qs(urlparse(self.path).query).get("token") or []
        return any(value == TOKEN for value in valeurs)

    def end_headers(self) -> None:
        # cadre 'none' : même une page de ce site ne doit pas pouvoir
        # s'afficher dans un <iframe>, dernier rempart contre le
        # détournement de clic (cliquer sur un bouton qu'on croit ailleurs).
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; "
            f"script-src 'nonce-{SCRIPT_NONCE}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
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
        """Détache une commande capable d'arrêter CE processus, puis confirme
        au navigateur qu'elle a bien été créée. Factorise /api/reglages et
        /api/stop, qui partagent exactement ce besoin.

        wfile.write()/flush() ne garantissent que la remise à l'OS, pas la
        livraison réelle jusqu'au navigateur : un taskkill trop rapproché
        peut aborter la connexion avant que la pile réseau ait fini de
        transmettre, la fenêtre étant plus large sur une machine chargée ou
        lente (rapporté par un utilisateur sous Windows 7/Python 3.8 :
        JSON.parse en échec côté page, disparaissant une fois la
        surveillance caméra arrêtée - moins de contention). Une courte
        pause après le flush laisse le temps à la pile réseau de vraiment
        vider son tampon avant la mise à mort."""
        # Vérifier que le relais a réellement pu être créé AVANT d'annoncer
        # l'acceptation. Le finaliseur attend ensuite brièvement avant l'arrêt,
        # ce qui laisse à cette réponse le temps de parvenir au navigateur sans
        # devoir espérer qu'un sleep exécuté après le flush soit suffisant.
        commande = [*commande_restart, "--delai", "0.75"]
        try:
            runtime.demarrer(
                runtime.self_command(*commande), cwd=str(runtime.app_dir()),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT, start_new_session=(os.name != "nt"))
        except OSError as erreur:
            self.send_json({"error": f"Impossible de lancer l'arrêt : {erreur}"}, 500)
            return
        self.send_json({"ok": True, "accepted": True})

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
        candidates = raw.get(camera_name.strip(), [])
        if isinstance(candidates, dict):
            candidates = [candidates]
        attributes = getattr(camera, "attributes", None) or {}
        network_id = str(getattr(camera, "network_id", "") or "")
        device_id = str(
            getattr(camera, "device_id", None)
            or getattr(camera, "camera_id", None)
            or attributes.get("device_id")
            or attributes.get("camera_id")
            or attributes.get("id")
            or ""
        )
        info = next(
            (item for item in candidates
             if device_id and str(item.get("id") or item.get("device_id")
                                  or item.get("camera_id") or "") == device_id),
            None,
        )
        if info is None:
            info = next(
                (item for item in candidates
                 if network_id and str(item.get("network_id")
                                       or item.get("network") or "") == network_id),
                candidates[0] if candidates else {},
            )
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
            "battery": attributes.get("battery"),
            "battery_signal": signals.get("battery"),
            "voltage": attributes.get("battery_voltage"),
            # Blink rapporte des degrés Fahrenheit ; blinkpy expose la
            # conversion, autant l'utiliser plutôt que de la refaire ici.
            "temperature": camera.temperature_c,
            "wifi": attributes.get("wifi_strength"),
            "lfr": signals.get("lfr"),
            "firmware": attributes.get("version"),
            "kind": attributes.get("type"),
            "model": model_name(attributes.get("type")),
            "serial": info.get("serial") or attributes.get("serial"),
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
                # _blink.refresh(force=True) enchainait aussi, par caméra,
                # get_camera_info() + update() (sync_module.py refresh()),
                # plus update_local_storage_manifest()/check_new_videos() -
                # rien de tout cela n'est lu plus bas : cet affichage ne
                # vient que de get_homescreen() (nom/batterie/temperature/
                # statut) et de network_info par module (armement). Verifie
                # dans le code source de blinkpy avant de couper (pas
                # suppose) : sync.cameras est peuple une seule fois, a la
                # connexion initiale (update_cameras(), appele par start(),
                # jamais par refresh()) - ses identifiants (device_id/
                # network_id, utilises plus bas pour rapprocher chaque
                # caméra de son entree dans l'ecran d'accueil) sont donc
                # deja stables ici, pas besoin de les rafraichir a chaque
                # appel. Mesure en reel (2026-09-03) : la sequence complete
                # (1 + 1 par module + 1 par camera, en serie) faisait durer
                # "Interrogation du systeme Blink..." bien plus que
                # necessaire pour cette page precise.
                await _blink.get_homescreen()
                for sync in _blink.sync.values():
                    await sync.get_network_info()
                # Les attributs de blinkpy ne disent pas *quand* une mesure a
                # été prise. L'écran d'accueil, lui, porte un `status` et un
                # `updated_at` par appareil : sans eux, la température d'une
                # caméra hors de portée s'affiche comme si elle était actuelle
                # alors qu'elle peut dater de plusieurs semaines.
                raw = {}
                home = getattr(_blink, "homescreen", None) or {}
                for group in ("cameras", "owls", "doorbells"):
                    for item in home.get(group) or []:
                        raw.setdefault(str(item.get("name") or "").strip(), []).append(item)

                modules = {str(m.get("name") or "").strip(): m
                           for m in (home.get("sync_modules") or [])}
                systems = []
                for name, sync in _blink.sync.items():
                    module = modules.get(str(sync.name or "").strip()) or {}
                    if not module and modules:
                        module = list(modules.values())[0]
                    systems.append({
                        "name": name.strip(),
                        "key": system_key(name, sync),
                        "armed": bool(sync.arm),
                        "module": MODULE_MODELS.get(module.get("type"))
                                  or (f"module « {module.get('type')} »"
                                      if module.get("type") else None),
                        "module_firmware": module.get("fw_version"),
                        "module_serial": module.get("serial"),
                        "cameras": [
                            dict(self.describe_camera(camera_name, camera, raw),
                                 key=camera_key(sync, camera_name, camera),
                                 clips_source=venues.get(camera_name.strip()))
                            for camera_name, camera in sync.cameras.items()
                        ],
                    })
                return {"systems": systems, "passages": runtime.passages()}
            return run()

        state = BLINK.call(read, timeout=60)
        remember_cameras(self.paths, state.get("systems") or [])
        state["webrtc"] = WEBRTC_ACTIF
        return state

    def set_armed(self, scope: str, identity: str, armed: bool) -> None:
        def apply(blink):
            async def run(_blink=blink):
                if scope == "system":
                    sync = BLINK.find_system(_blink, identity)
                    await sync.async_arm(armed)
                    return
                _, camera = BLINK.find_camera(_blink, identity)
                await camera.async_arm(armed)
            return run()

        BLINK.call(apply, timeout=60)

    def reveiller_camera(self, identity: str) -> None:
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
                _, camera = BLINK.find_camera(_blink, identity)
                await camera.snap_picture()
            return run()

        BLINK.call(demander, timeout=130)

    def send_camera_thumb(self, identity: str) -> None:
        """Sert la dernière vignette connue d'une caméra.

        Elle remplace le cadre noir avant qu'on lance un direct : on voit
        d'emblée ce que regarde la caméra, y compris pour celles qui sont hors
        de portée et dont le direct échouera. C'est l'image que Blink garde de
        son côté, pas une capture neuve : la demander réveillerait la caméra et
        userait sa batterie à chaque affichage de la page.

        Récupérée une seule fois, puis servie telle quelle : seul « Actualiser »
        la renouvelle."""
        cached = (self.paths["thumbs"] / "cameras" / f"{safe_file(identity)}.jpg")
        if not (cached.is_file() and cached.stat().st_size > 0):
            def fetch(blink):
                async def run(_blink=blink):
                    _, camera = BLINK.find_camera(_blink, identity)
                    response = await camera.get_media()
                    if response is None or response.status != 200:
                        return b""
                    return await response.read()
                return run()
            try:
                body = BLINK.call(fetch, timeout=45)
            except Exception as error:
                print(f"[vignette] {identity} : {type(error).__name__}: {error}", flush=True)
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

    def send_attente_module(self) -> None:
        """Attend que MODULE_SLOT soit réellement libre, sans le retenir.

        Ne déclenche rien : watchLive() (serve_app.js) a déjà demandé
        l'arrêt de la session active (stopWatch(), pc.close()/fetch abort)
        avant cet appel - la lui redemander ici serait redondant. Le rôle
        de cette route est seulement de dire QUAND ce nettoyage, déjà en
        cours, arrive à son terme, plutôt que de laisser le client deviner
        un délai (10s à l'aveugle, remplacé par ceci le 2026-09-03 -
        BACKLOG.md : mesuré 8,8s puis 1,65s sur deux bascules réelles,
        bien trop variable pour un délai fixe). Ce qui prend réellement du
        temps échappe à ce serveur : blinkpy (LiveStreamAPI.poll(),
        livestream.py) n'envoie la commande "done" à Blink qu'une fois vu
        la fin de la connexion TCP vers le relais Blink lui-même - déjà
        attendu en entier par _stop_stream avant que MODULE_SLOT soit
        rendu, donc rien à accélérer ici, seulement à rendre visible."""
        libre = MODULE_SLOT.acquire(blocking=True, timeout=ATTENTE_MODULE_MAX_SECONDS)
        if libre:
            MODULE_SLOT.release()
            self.send_json({"libre": True})
        else:
            self.send_json({"libre": False, "error": _slot_occupe_message()})

    def send_offer_webrtc(self, name: str, payload: dict) -> None:
        """Négocie un direct WebRTC (offre/réponse SDP) : voir blink_webrtc.py.

        Contrairement à /live-mse, cette requête se termine dès que la
        réponse SDP est envoyée : le direct continue ensuite sur BLINK.loop,
        jusqu'à ce que connectionstatechange signale sa fin (fermeture
        d'onglet, échec ICE...), qui rend alors MODULE_SLOT/le verrou disque
        exactement comme le fait le bloc finally de send_live_mse. `nettoye`
        garantit que cette libération n'a lieu qu'une fois, que la négociation
        échoue avant même d'atteindre aiortc ou après (connectionstatechange
        peut alors, lui aussi, finir par passer à "failed")."""
        offer_sdp = str(payload.get("sdp") or "")
        offer_type = str(payload.get("type") or "")
        if not offer_sdp or not offer_type:
            self.send_json({"error": "offre SDP incomplète"}, 400)
            return

        if not MODULE_SLOT.acquire(blocking=False):
            message = _slot_occupe_message()
            _memoriser_erreur_direct(name, message, 409)
            self.send_json({"error": message}, 409)
            return

        holder: dict = {"nettoye": False}

        async def _fermer() -> None:
            if holder["nettoye"]:
                return
            holder["nettoye"] = True
            stream = holder.get("stream")
            if stream is not None:
                verdict = await _stop_stream(stream, holder.get("feed"))
                _journal_direct(name, verdict)
            verrou = holder.get("lock")
            if verrou is not None and holder.get("lock_entered"):
                try:
                    verrou.__exit__(None, None, None)
                except Exception as error:
                    _journal_direct(
                        name, f"échec de libération du verrou disque, "
                              f"{type(error).__name__}: {error}"
                    )
            try:
                _slot_rendu()
            finally:
                MODULE_SLOT.release()

        try:
            _slot_pris("direct WebRTC", name)
            _effacer_erreur_direct()
            holder["lock"] = blink_engine.hub_lock("direct")
            holder["lock"].__enter__()
            holder["lock_entered"] = True

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
                    holder["stream"] = stream
                    await stream.start()
                    holder["feed"] = asyncio.ensure_future(stream.feed())
                    _, answer_sdp, answer_type = await blink_webrtc.negocier(
                        stream.url, offer_sdp, offer_type, _fermer
                    )
                    return answer_sdp, answer_type
                return run()

            answer_sdp, answer_type = BLINK.call(start, timeout=45)
            _journal_direct(name, "direct WebRTC négocié")
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) \
                else f"{type(error).__name__}: {error}"
            _memoriser_erreur_direct(name, message, 503)
            _journal_direct(name, f"échec (webrtc), {message}")
            try:
                BLINK.call(lambda _b: _fermer(), timeout=45)
            except Exception as error:
                _journal_direct(
                    name, f"échec de nettoyage après échec webrtc, "
                          f"{type(error).__name__}: {error}"
                )
            self.send_json({"error": message}, 503)
            return

        self.send_json({"sdp": answer_sdp, "type": answer_type})

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
            message = _slot_occupe_message()
            _memoriser_erreur_direct(name, message, 409)
            # Le détail peut contenir un nom de caméra non latin-1 ou un saut
            # de ligne. Il reste disponible via /api/live-error ; la ligne de
            # statut HTTP, elle, doit toujours rester ASCII et bien formée.
            self.send_error(409, "Live stream busy")
            return

        holder: dict = {}
        errors: list = []
        erreur_direct = None
        reponse_commencee = False
        journaux_nettoyage: list = []
        try:
            _slot_pris("direct MSE", name)
            # Une tentative réellement admise remplace l'ancien diagnostic.
            # En particulier, un 409 ne doit jamais faire relire au navigateur
            # le 503 d'une tentative précédente de la même caméra.
            _effacer_erreur_direct()
            holder["lock"] = blink_engine.hub_lock("direct")
            holder["lock"].__enter__()
            holder["lock_entered"] = True

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
                    # init_livestream() a déjà créé une commande distante. Si
                    # start() échoue, le finally doit tout de même retrouver ce
                    # flux et envoyer stop/done à Blink.
                    holder["stream"] = stream
                    await stream.start()
                    holder["feed"] = asyncio.ensure_future(stream.feed())
                    return stream.url
                return run()

            url = BLINK.call(start, timeout=45)
            _journal_direct(name, f"flux Blink ouvert sur {url}")

            process = runtime.demarrer(
                # loglevel "info" temporaire (diagnostic partage relais/ffmpeg
                # en cours, cf. direct.log) : à repasser à "error" une fois
                # l'écart Terrasse1/Salon expliqué.
                [self.ffmpeg, "-hide_banner", "-loglevel", "info",
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
                 #
                 # Essai en cours (direct.log, 2026-09-02) : à 5000000, ce
                 # plafond est systématiquement consommé en quasi-totalité
                 # (~5-6 s) sur Salon ET Terrasse1, deux caméras sans rapport
                 # avec le cas lent d'origine (jardin) : pas juste utilisé
                 # quand nécessaire, toujours épuisé. Redescendu à 1500000
                 # (3x l'ancien seuil MJPEG, 1/3 de l'actuel) pour voir si
                 # jardin tient toujours sans « dimensions not set ». À
                 # remonter si jardin échoue avec cette valeur.
                 "-analyzeduration", "1500000", "-probesize", "1500000",
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

            def _drainer_stderr() -> None:
                # Une ligne à la fois (pas un seul read() bloquant jusqu'à la
                # fin) : diagnostic temporaire pour dater chaque étape interne
                # de ffmpeg (détection du flux, image-clé...) pendant qu'on
                # cherche pourquoi Terrasse1 (12 s) dépasse Salon (8 s) entre
                # le premier octet du relais et le segment initial. errors
                # reste alimentée pour _texte_stderr_ffmpeg (reason/diagnostic
                # de fin), inchangé par ailleurs.
                for ligne_brute in iter(process.stderr.readline, b""):
                    errors.append(ligne_brute)
                    texte = ligne_brute.decode("utf-8", "replace").rstrip()
                    if texte:
                        _journal_direct(name, f"ffmpeg : {texte}")

            holder["drain"] = threading.Thread(target=_drainer_stderr, daemon=True)
            holder["drain"].start()

            lecteur = LecteurTube(process.stdout)
            first = read_mp4_init_segment(lecteur, LIVE_FIRST_FRAME_SECONDS)
            _journal_direct(name, f"segment initial {len(first)} octets")
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
                trace = _texte_stderr_ffmpeg(errors)
                if trace:
                    reason = f"{reason} | ffmpeg : {trace}"
                raise RuntimeError(reason)
            codec_str = h264_mime_codec_from_moov(first)

            # À partir de ce point, une seconde ligne de statut HTTP ne peut
            # plus être envoyée proprement, même si end_headers() échoue.
            reponse_commencee = True
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Codec", codec_str)
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            _journal_direct(name, f"en-tetes envoyes, codec {codec_str}")

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
            erreur_direct = str(error) if isinstance(error, RuntimeError) \
                else f"{type(error).__name__}: {error}"
        finally:
            process = holder.get("process")
            if process is not None:
                try:
                    process.terminate()
                except Exception as error:
                    journaux_nettoyage.append(
                        f"echec de terminate ffmpeg, {type(error).__name__}: {error}"
                    )
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except Exception as error:
                        journaux_nettoyage.append(
                            f"echec de kill ffmpeg, {type(error).__name__}: {error}"
                        )
                except Exception as error:
                    journaux_nettoyage.append(
                        f"echec d'attente ffmpeg, {type(error).__name__}: {error}"
                    )
            stream = holder.get("stream")
            if stream is not None:
                try:
                    verdict = BLINK.call(
                        lambda _b: _stop_stream(stream, holder.get("feed")),
                        timeout=45,
                    )
                except Exception as error:
                    verdict = f"echec de fermeture, {type(error).__name__}: {error}"
                journaux_nettoyage.append(verdict)
            verrou = holder.get("lock")
            if verrou is not None and holder.get("lock_entered"):
                try:
                    verrou.__exit__(None, None, None)
                except Exception as error:
                    journaux_nettoyage.append(
                        f"echec de liberation du verrou disque, "
                        f"{type(error).__name__}: {error}"
                    )
            # Publier l'échec avant de rendre le jeton empêche une nouvelle
            # tentative d'effacer LAST_LIVE_ERROR puis de voir cet ancien
            # diagnostic réapparaître sous ses pieds. La réponse HTTP reste,
            # elle, différée jusqu'après la libération du jeton.
            if erreur_direct is not None:
                try:
                    _memoriser_erreur_direct(name, erreur_direct, 503)
                except Exception as error:
                    journaux_nettoyage.append(
                        f"diagnostic d'echec indisponible, "
                        f"{type(error).__name__}: {error}"
                    )
            # Ces deux libérations mémoire sont la dernière ceinture : aucune
            # erreur de fermeture de ffmpeg/Blink/verrou disque, ni aucun
            # diagnostic, ne doit pouvoir les sauter.
            try:
                _slot_rendu()
            finally:
                MODULE_SLOT.release()

            # Diagnostic seulement après avoir rendu toutes les ressources :
            # sa lecture attend un fil et manipulait auparavant bytes comme str,
            # ce qui pouvait lever ici et laisser le module occupé à jamais.
            sent = holder.get("sent")
            if sent is not None:
                try:
                    detail = ""
                    if not sent:
                        drain = holder.get("drain")
                        if drain is not None:
                            drain.join(timeout=3)
                        detail = " | ffmpeg : " + (
                            _texte_stderr_ffmpeg(errors) or "rien"
                        )
                    journaux_nettoyage.append(
                        f"termine, {sent} octets transmis{detail}"
                    )
                except Exception as error:
                    journaux_nettoyage.append(
                        f"diagnostic final indisponible, {type(error).__name__}: {error}"
                    )
            for journal in journaux_nettoyage:
                _journal_direct(name, journal)

        if erreur_direct is not None:
            # Le client ne voit ce 503 qu'une fois ffmpeg arrêté, la session
            # Blink fermée et les verrous rendus. Sa prochaine tentative ne
            # peut donc plus consommer ses reprises sur nos propres 409.
            _journal_direct(name, f"echec, {erreur_direct}")
            if not reponse_commencee:
                try:
                    self.send_error(503, "Live stream unavailable")
                except Exception:
                    pass

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
        if route not in ("/", "/favicon.ico") and not self.jeton_valide():
            self.send_error(403)
            return
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
            self.send_json(_derniere_erreur_direct())
            return

        if route == "/api/system":
            try:
                self.send_json(self.system_state())
            except RuntimeError as error:
                self.send_json({"error": str(error)}, 503)
            return

        if route == "/api/attente-module":
            self.send_attente_module()
            return

        if route == "/api/videos":
            self.send_json(collect_videos(self.paths, self.ffmpeg))
            return

        if route == "/api/status":
            self.send_json({
                "authenticated": (runtime.app_dir() / "blink_auth.json").is_file(),
                "initial_setup": self.initial_setup,
            })
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
                {**runtime.lire_reglages(), "storage_dir": runtime.lire_dossier_stockage(),
                 "initial_setup": self.initial_setup})
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
            entrees = read_entries(self.paths)
            choices = suppression_auto_choices(entrees)
            actives = suppression_auto_keys()

            # Migration sûre de l'ancien fichier qui ne contenait que des
            # noms : un nom unique peut être relié sans ambiguïté à sa clé.
            # Les homonymes restent volontairement désactivés ; mieux vaut
            # demander un nouveau choix que supprimer sur la mauvaise caméra.
            anciennes = {
                value for value in runtime.lire_suppression_auto()
                if not value.startswith("camera-v2-")
            }
            by_name = {}
            for choice in choices:
                by_name.setdefault(choice["name"], []).append(choice["key"])
            migrated = set(actives)
            ignored = []
            for name in anciennes:
                keys = by_name.get(name, [])
                if len(keys) == 1:
                    migrated.add(keys[0])
                else:
                    ignored.append(name)
            if anciennes:
                runtime.ecrire_suppression_auto(migrated)
                actives = migrated
            self.send_json({"cameras": choices, "actives": sorted(actives),
                            "legacy_ignored": sorted(ignored)})
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
                            "maj": maj.disponible(reseau=False)})
            return

        if route == "/api/travail":
            # Sonde fréquente et volontairement minuscule : ne pas relire ici
            # tout le registre des clips comme /api/passages. Sur un vieux PC
            # Windows 7, parser ce JSON croissant toutes les trois secondes
            # coûterait bien plus que la lecture du seul état de progression.
            self.send_json({"travail": runtime.travail_affichable()})
            return

        if route == "/api/refresh":
            if self.initial_setup:
                # Défense côté serveur, pas seulement dialogue modal : même
                # une requête manuelle ne peut rapatrier un clip avant la
                # validation du dossier et du fuseau.
                self.send_json({"error": "Validez d'abord les réglages initiaux."}, 409)
                return
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
                # Le sous-processus ``download`` prend lui-même le verrou de
                # fichier du hub pendant son inventaire puis ses transferts.
                # Le prendre aussi ici, dans le parent, le rendait impossible à
                # réacquérir : chaque clic « Actualiser » sautait alors toute la
                # partie USB comme si un autre programme occupait le module.
                # MODULE_SLOT reste la protection mémoire contre un direct lancé
                # par ce serveur ; le verrou enfant couvre les autres processus.
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
        hub_args = ["--hub", self.hub] if self.hub else []
        etapes = [("Téléchargement", "phase.step_download",
                  runtime.self_command("download", *hub_args)),
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

        if route.startswith("/live-webrtc/"):
            self.send_offer_webrtc(unquote(route[len("/live-webrtc/"):]), payload)
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
                            sync = BLINK.find_sync_module(blink, entree)
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

                slot_pris = False
                try:
                    # Même ressource physique que le direct et le downloader :
                    # lire puis régénérer un manifeste pendant l'une de ces
                    # opérations produit « System is busy » ou un lot partiel.
                    if not MODULE_SLOT.acquire(blocking=False):
                        raise blink_engine.BusyError(_slot_occupe_message())
                    slot_pris = True
                    _slot_pris("suppression manuelle")
                    with blink_engine.hub_lock("suppression manuelle"):
                        BLINK.call(operation, timeout=30 + 90 * max(1, nb_cameras_usb))
                except Exception as error:
                    for identity, _, _ in cibles:
                        resultats.setdefault(identity, f"echec: {type(error).__name__}")
                finally:
                    if slot_pris:
                        _slot_rendu()
                        MODULE_SLOT.release()

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
            live_protocol = str(payload.get("live_protocol", "webrtc"))
            if live_protocol not in runtime.PROTOCOLES_LIVE_VALIDES:
                self.send_json(
                    {"error": f"Protocole de direct inconnu : « {live_protocol} »."}, 400)
                return
            try:
                # Le changement de racine prépare et copie d'abord session et
                # réglages, puis publie atomiquement son pointeur. Les nouvelles
                # valeurs sont écrites ensuite dans cette racine devenue active.
                with runtime.verrou_configuration():
                    runtime.ecrire_dossier_stockage(storage_dir)
                    runtime.ecrire_reglages(
                        usb_minutes, cloud_minutes, port, timestamp, timezone_str,
                        merge_jour, merge_semaine, merge_mois, download_auto, live_protocol)
                    if self.initial_setup:
                        runtime.marquer_configuration_initiale()
            except runtime.BusyError as erreur:
                self.send_json(
                    {"error": f"Une modification des réglages est déjà en cours : {erreur}"},
                    409)
                return
            except OSError as erreur:
                self.send_json(
                    {"error": f"Impossible d'enregistrer les réglages : {erreur}"}, 500)
                return
            if self.initial_setup:
                # Le parent ``start`` voit maintenant le marqueur, laisse à
                # cette réponse le temps d'arriver, puis remplace ce serveur
                # seul par la composition complète. Lui demander un restart
                # ici créerait une course avec le verrou ``start`` qu'il tient
                # justement pendant tout le parcours initial.
                self.send_json({"ok": True, "accepted": True,
                                "initial_setup": True})
                return
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
            autorisees = {
                choice["key"]
                for choice in suppression_auto_choices(read_entries(self.paths))
            }
            if not camera or camera not in autorisees:
                self.send_json({"error": "Identifiant de caméra inconnu."}, 400)
                return
            # Pas de redémarrage : un_passage() (blink_engine.py) relit ce
            # fichier à chaque tour, même principe que runtime.lire_langue().
            cameras = suppression_auto_keys()
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
__CSS__
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
    <button class="danger" id="applyButton" hidden></button>
    <button class="primary" id="refresh" data-i18n="btn.refresh">↻ Actualiser</button>
    <button id="reglagesButton" data-i18n="btn.reglages" data-i18n-title="btn.reglages.title" title="Réglages">⚙ Réglages…</button>
  </div>
  <span class="langGroup" title="Langue / Language">
    <button class="btn-lang" data-lang-btn="fr">FR</button>
    <button class="btn-lang" data-lang-btn="en">EN</button>
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
    <button class="btn-lang" data-lang-btn="fr">FR</button>
    <button class="btn-lang" data-lang-btn="en">EN</button>
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
  <p id="initialSetupHint" class="sub" hidden data-i18n="reglages.initial.hint">
    Vérifiez notamment le dossier des données et le fuseau horaire. Aucun clip
    ne sera téléchargé avant que vous ayez appliqué ces réglages.
  </p>
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
      <label for="usbMinutes" data-i18n="reglages.usb">Stockage local (minutes)</label>
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
    <div class="champCadence">
      <label for="liveProtocol" data-i18n="reglages.liveProtocol">Protocole du direct</label>
      <select id="liveProtocol">
        <option value="webrtc" data-i18n="reglages.liveProtocol.webrtc">WebRTC (rapide)</option>
        <option value="mse" data-i18n="reglages.liveProtocol.mse">MSE (compatible)</option>
      </select>
    </div>
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
            title="Une fois un clip téléchargé avec succès, il est supprimé de sa source (stockage local USB/microSD ou cloud de l'abonnement selon la caméra).">Suppression automatique après téléchargement</legend>
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
<script nonce="__SCRIPT_NONCE__">
__APP_JS__
</script>
</body>
</html>
"""

# CSS et JS vivent dans leurs propres fichiers (serve_style.css,
# serve_app.js) : un .py qui les contenait en dur n'avait ni coloration ni
# lint corrects pour l'un ou l'autre. Splicés ici avant les substitutions
# suivantes pour que __TOKEN__ (dans serve_app.js) en profite comme le reste
# du gabarit. runtime.resource_dir() plutôt que Path(__file__).parent : seul
# le premier reste correct une fois le bundle figé (voir assets/, même
# convention pour le favicon).
PAGE = PAGE.replace(
    "__CSS__",
    (runtime.resource_dir() / "serve_style.css").read_text(encoding="utf-8"),
)
PAGE = PAGE.replace(
    "__APP_JS__",
    (runtime.resource_dir() / "serve_app.js").read_text(encoding="utf-8"),
)

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
SCRIPT_NONCE = uuid.uuid4().hex
PAGE = PAGE.replace("__SCRIPT_NONCE__", SCRIPT_NONCE)


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
    parser.add_argument(
        "--hub", help="nom du Sync Module Blink ; tous les modules si omis",
    )
    parser.add_argument(
        "--thumbs", type=Path, default=BASE_DIR / ".blink_thumbs",
        help="cache des vignettes ; jetable, refabriqué à la demande",
    )
    parser.add_argument("--port", type=runtime.port_valide, default=8765)
    parser.add_argument("--initial-setup", action="store_true",
                        help=argparse.SUPPRESS)
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
    Handler.initial_setup = args.initial_setup
    try:
        Handler.ffmpeg = md.find_ffmpeg()
        Handler.timezone = ZoneInfo(args.timezone)
        collect(Handler.paths, Handler.timezone, Handler.ffmpeg)
    except (RuntimeError, ZoneInfoNotFoundError) as error:
        print(f"Erreur : {error}")
        return 1

    # 127.0.0.1 et pas 0.0.0.0 par défaut : le tableau de bord n'a aucune
    # authentification, il n'a rien à faire sur le réseau local sans un
    # choix explicite. BLINK_BIND permet d'y déroger pour deux usages :
    # accès direct depuis d'autres machines du LAN (à réserver à un réseau
    # domestique de confiance, jamais exposé sur internet), ou conteneur
    # Docker où 127.0.0.1 désignerait la boucle locale du conteneur,
    # injoignable depuis l'hôte même avec le port publié (le réseau en pont
    # route vers l'interface du conteneur, pas sa boucle locale) : la
    # frontière de sécurité reste alors posée par la publication du port
    # elle-même (voir docker-compose.yml, 127.0.0.1: en dur).
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

        def handle_error(self, request, client_address):
            # Comportement par défaut de socketserver : traceback.print_exc()
            # sur stderr seulement - invisible depuis console=False (revue du
            # 27/08 : un changement de dossier de stockage ne redémarrait
            # plus du tout, sans la moindre trace nulle part pour dire
            # pourquoi). Consigné en plus dans un fichier, jamais à la place :
            # reste utile aussi quand une vraie console existe.
            import traceback
            try:
                with (runtime.app_dir() / "serve_erreurs.log").open(
                        "a", encoding="utf-8") as journal:
                    journal.write(f"\n--- {dt.datetime.now().isoformat()} "
                                  f"{client_address} ---\n")
                    traceback.print_exc(file=journal)
            except OSError:
                pass
            super().handle_error(request, client_address)

    try:
        server = Server((bind, args.port), Handler)
    except OSError as error:
        print(f"Impossible d'écouter sur le port {args.port} : {error}")
        print("Un autre « blink2video serve » tourne sans doute déjà. Arrêtez-le, "
              "choisissez un autre port avec --port.")
        return 1
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Interface disponible sur {url}   (Ctrl+C pour arrêter)")
    if bind not in ("127.0.0.1", "localhost"):
        # webbrowser.open() ci-dessous garde volontairement 127.0.0.1 (le
        # navigateur ouvert est celui de cette machine), mais quelqu'un qui a
        # positionné BLINK_BIND pour l'accès LAN doit voir, sans avoir à
        # relire la doc, qu'il vient d'ouvrir l'interface sans authentification
        # au reste du réseau.
        print(f"Attention : BLINK_BIND={bind} - interface aussi joignable "
              f"depuis le reste du réseau sur le port {args.port}, sans "
              f"authentification. À réserver à un réseau de confiance.")
    # Le serveur de configuration initiale ne doit créer aucun travail de
    # fond avant validation. Même la veille de version, sans rapport avec les
    # clips, attend donc le vrai démarrage pour garder ce mode strictement
    # limité au formulaire.
    if not args.initial_setup:
        veiller_sur_les_versions()
    if args.open_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()

    def surveiller_arret_demande():
        # Un arrêt externe (tray, stop) pose ce drapeau plutôt que de tuer le
        # processus directement : shutdown() laisse toute requête déjà en
        # cours se terminer normalement (revue du 27/08, arrêt coopératif).
        while not runtime.arret_demande():
            time.sleep(1)
        server.shutdown()

    fil_arret = threading.Thread(target=surveiller_arret_demande, daemon=True)
    fil_arret.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
