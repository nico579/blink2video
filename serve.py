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
import re
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Avant tout import de dépendance : c'est ici qu'un environnement isolé
# est préparé et le programme relancé dedans si nécessaire.
import runtime

runtime.bootstrap()

from aiohttp import ClientSession

import blink2video as bk
import merge_daily as md


import runtime

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
LIVE_MAX_SECONDS = 300
# Délai accordé à la première image. Une caméra sur batterie doit se réveiller,
# donc on est patient ; au-delà on considère qu'elle ne répondra pas.
LIVE_FIRST_FRAME_SECONDS = 40
LIVE_BOUNDARY = "blinkframe"

# Durée de validité d'une vignette de caméra. Blink ne la rafraîchit qu'à
# l'occasion d'un enregistrement ou d'une capture manuelle : la redemander plus
# souvent ne montrerait rien de neuf.
CAMERA_THUMB_SECONDS = 600


def safe_file(name: str) -> str:
    """Nom de fichier sûr pour une caméra, dont le nom est libre côté Blink."""
    cleaned = re.sub(r"[^\w.-]+", "_", name.strip(), flags=re.UNICODE).strip("._")
    return cleaned or "camera"


def read_entries(paths: dict) -> dict:
    return md.read_registry(paths["input"] / md.DOWNLOAD_STATE)


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


def collect(paths: dict, timezone: ZoneInfo, ffmpeg: str = "") -> dict:
    """Inventorie les clips connus du registre de téléchargement.

    Contrairement à merge_daily.load_groups, les clips écartés sont conservés :
    c'est précisément ce qu'on veut pouvoir revoir et reprendre."""
    entries = read_entries(paths)
    # Les durées déjà mesurées par merge_daily sont reprises telles quelles ;
    # seules celles des clips écartés, dont l'entrée a été balayée du registre
    # normalisé, restent à mesurer.
    probed = md.load_json(paths["normalized"] / md.NORMALIZED_STATE, {}).get("clips")
    probed = probed if isinstance(probed, dict) else {}
    facts = md.load_json(paths["thumbs"] / CAMERA_FACTS, {})

    clips = []
    for entry in entries.values():
        try:
            identity = entry["path"]
            created = md.parse_created_at(entry["created_at"])
            camera = str(entry.get("camera") or "camera").strip() or "camera"
        except (KeyError, TypeError, ValueError):
            continue

        local = created.astimezone(timezone)
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
        # mesure alors directement, une fois, depuis Blink_Excluded.
        if seconds is None and source and ffmpeg:
            seconds = probe_duration(ffmpeg, paths[source] / identity)

        clips.append({
            "identity": identity,
            "camera": camera,
            "day": local.date().isoformat(),
            "time": local.strftime("%H:%M:%S"),
            "excluded": excluded,
            "source": source,
            "duration": float(seconds or 0.0),
            # Pas de « modèle » : l'API Blink n'expose qu'un nom de code interne
            # (« owl », « catalina ») dont seul le premier est documenté. La
            # définition de l'image, elle, est mesurée et parlante.
            "model": model_name((facts.get(camera) or {}).get("kind")),
        })

    # Du plus récent au plus ancien : c'est ce qu'on vient regarder. Deux tris
    # successifs plutôt qu'une clé inversée d'un bloc, sinon l'ordre des
    # caméras se retournerait aussi ; le second tri est stable, il conserve
    # l'ordre antichronologique établi par le premier.
    clips.sort(key=lambda clip: (clip["day"], clip["time"]), reverse=True)
    clips.sort(key=lambda clip: clip["camera"])
    return {
        "clips": clips,
        "cameras": sorted({clip["camera"] for clip in clips}),
        # Le modèle est propre à la caméra, pas au clip : envoyé une fois ici,
        # et retiré de chaque clip pour qu'aucun affichage ne le répète.
        "models": {nom: modele for nom, modele in (
            (clip["camera"], clip.pop("model", None)) for clip in clips) if modele},
        "days": sorted({clip["day"] for clip in clips}, reverse=True),
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


def read_with_deadline(pipe, seconds: float) -> bytes:
    """Lit un premier bloc sur un tube, ou renonce au bout de `seconds`.

    Une lecture sur tube est bloquante et ne se laisse pas interrompre : on la
    confie donc à un fil, et c'est l'attente de ce fil qui porte le délai."""
    result: list = []
    reader = threading.Thread(target=lambda: result.append(pipe.read(4096)),
                              daemon=True)
    reader.start()
    reader.join(timeout=seconds)
    return result[0] if result else b""


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
                    self.blink = await bk.connect_saved(self.session)
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
                blink = await bk.login(session, username, password, ask_code)
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
                            self.describe_camera(camera_name, camera, raw)
                            for camera_name, camera in sync.cameras.items()
                        ],
                    })
                return {"systems": systems}
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
        userait sa batterie à chaque affichage de la page."""
        cached = (self.paths["thumbs"] / "cameras" / f"{safe_file(name)}.jpg")
        fresh = (
            cached.is_file()
            and cached.stat().st_size > 0
            and time.time() - cached.stat().st_mtime < CAMERA_THUMB_SECONDS
        )
        if not fresh:
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
            self.send_error(409, "Le module est deja occupe (direct ou actualisation)")
            return

        holder: dict = {}
        try:
            # Verrou sur disque en plus du jeton mémoire : la surveillance est un
            # autre processus, elle ne voit pas nos sémaphores. Sans lui, un
            # téléchargement lancé en arrière-plan tomberait sur « System is
            # busy » pendant qu'on diffuse.
            holder["lock"] = bk.hub_lock("direct")
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
                 # aurait plusieurs secondes de retard.
                 "-fflags", "nobuffer", "-flags", "low_delay",
                 "-analyzeduration", "1000000", "-probesize", "500000",
                 "-i", url,
                 "-f", "mpjpeg", "-q:v", "6", "-r", "10",
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
            first = read_with_deadline(process.stdout, LIVE_FIRST_FRAME_SECONDS)
            if not first:
                reason = self.live_failure_reason(holder.get("stream"))
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

            self.wfile.write(first)
            sent = len(first)
            deadline = time.monotonic() + LIVE_MAX_SECONDS
            while time.monotonic() < deadline:
                chunk = process.stdout.read(16384)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    sent += len(chunk)
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    break  # onglet fermé : c'est la fin normale d'un direct
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
            try:
                self.send_json(collect(self.paths, self.timezone, self.ffmpeg))
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
        try:
            # Le téléchargement interroge le Sync Module, comme le direct. Les
            # laisser se chevaucher garantirait un « System is busy » : mieux
            # vaut le dire tout de suite que d'échouer au milieu.
            if not MODULE_SLOT.acquire(blocking=False):
                self.send_event({"line": (
                    "Un direct est en cours. Arrêtez-le avant d'actualiser : le "
                    "module de synchronisation ne traite qu'une commande à la fois."
                )})
                self.send_event({"done": True, "ok": False})
                return
            try:
                # Même verrou de fichier que la surveillance : le téléchargement
                # lancé ici est exactement celui qu'elle fait de son côté, et
                # elle tourne dans un autre processus qui ne voit pas nos
                # sémaphores.
                with bk.hub_lock("actualisation", stale_after=3600):
                    self.run_refresh()
            except bk.BusyError as error:
                self.send_event({"line": f"Module occupé : {error}."})
                self.send_event({"done": True, "ok": False})
            finally:
                MODULE_SLOT.release()
        finally:
            self.lock.release()

    def run_refresh(self) -> None:
        auth = BASE_DIR / "blink_auth.json"
        script, phase = BASE_DIR / "daily.py", "Téléchargement"
        if not auth.is_file():
            # blink2video.py demanderait l'e-mail, le mot de passe et le code 2FA sur
            # l'entrée standard, qui n'existe pas ici : le processus resterait
            # bloqué. On le dit et on se contente de reconstruire.
            self.send_event({"line": (
                f"Session Blink absente ({auth.name}). Lancez « python blink2video.py "
                "login » dans un terminal pour vous connecter. Reconstruction "
                "des vidéos seule."
            )})
            script, phase = BASE_DIR / "merge_daily.py", "Fusion"

        command = (runtime.self_command("all", "--hub", self.hub)
                   if script.name == "daily.py"
                   else runtime.self_command("merge"))

        # PYTHONUNBUFFERED se transmet aux petits-enfants (daily.py lance
        # blink2video.py et merge_daily.py) : sans lui, leur sortie arriverait par
        # blocs de plusieurs kilo-octets et la barre avancerait par à-coups.
        # PYTHONIOENCODING évite les accents mutilés par la console Windows.
        env = dict(__import__("os").environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.send_event({"phase": phase, "line": f"$ {Path(script).name}"})

        process = runtime.demarrer(
            command, cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        alive = True
        for raw in process.stdout:
            line = raw.rstrip("\n")
            event = {"line": line}
            # blink2video.py et merge_daily.py annoncent tous deux leur avancement
            # sous la forme « [3/24] ». Une seule règle de lecture suffit donc,
            # et chaque phase repart naturellement de 1.
            counter = PROGRESS.search(line)
            if counter:
                index, total = int(counter.group(1)), int(counter.group(2))
                # Une ligne « [3/24] 45% » ne dit rien de neuf au journal : elle
                # ne sert qu'à faire avancer la barre entre deux clips. On la
                # retire du texte pour ne pas noyer les messages utiles.
                inner = INNER.match(line)
                fraction = int(inner.group(1)) / 100 if inner else 0.0
                if inner:
                    event.pop("line")
                event["progress"] = {
                    "done": round(index - 1 + fraction, 3), "total": total
                }
            heading = HEADING.match(line)
            if heading:
                # Les titres de daily.py sont en capitales ; on les adoucit sans
                # abîmer un nom propre déjà correctement casé.
                title = heading.group(1).strip()
                event["phase"] = title.capitalize() if title.isupper() else title
            if not self.send_event(event):
                alive = False
                process.terminate()
                break
        process.wait()
        if alive:
            self.send_event({"done": True, "ok": process.returncode == 0})

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

        if route == "/api/toggle":
            identity = str(payload.get("identity", ""))
            excluded = bool(payload.get("excluded"))
            if not IDENTITY.match(identity):
                self.send_json({"error": "identifiant invalide"}, 400)
                return
            # Une seule décision à la fois : le registre est un fichier, deux
            # écritures concurrentes en perdraient une.
            with self.lock:
                try:
                    md.set_excluded(
                        self.paths["input"], self.paths["normalized"],
                        self.paths["excluded"], [str(self.paths["input"] / identity)],
                        excluded,
                    )
                except RuntimeError as error:
                    self.send_json({"error": str(error)}, 500)
                    return
            self.send_json({"ok": True})
            return

        self.send_error(404)


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
  .count { color:var(--dim); margin-left:auto; font-variant-numeric:tabular-nums; }
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
  .live img { width:100%; height:100%; object-fit:contain; }
  /* La vignette reste en fond, le bouton se pose dessus. */
  .live img.still { position:absolute; inset:0; opacity:.55; }
  .live .watch { position:relative; }
  .watch { border-radius:7px; padding:8px 14px; }
  .watch.stop { position:absolute; right:10px; bottom:10px; opacity:.85; }
  .live { flex-direction:column; gap:12px; }
  .live .hint { color:var(--dim); font-size:14px; margin:0;
                text-align:center; padding:0 20px; line-height:1.4; }
  .live .hint.overlay { position:absolute; }
  dialog { background:var(--card); color:var(--text); border:1px solid var(--line);
           border-radius:12px; padding:24px; width:min(380px, 92vw); }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  dialog h3 { margin:0 0 6px; font-size:16px; }
  dialog p { margin:0 0 18px; color:var(--dim); font-size:13px; }
  dialog input { width:100%; font:inherit; color:var(--text); background:var(--bg);
                 border:1px solid var(--line); border-radius:7px;
                 padding:9px 11px; margin-bottom:12px; }
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
  <button class="primary" id="refresh">Actualiser</button>
  <span class="count" id="count"></span>
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
    <input id="pass" type="password" placeholder="Mot de passe"
           autocomplete="current-password">
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

function renderLive() {
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
    <div class="live" id="live-${cssId(c.name)}">
      <img class="still" src="/camthumb/${encodeURIComponent(c.name)}" alt="">
      <button class="watch" onclick="watch('${c.name}')">Voir en direct</button>
    </div>
    <div class="meta">
      <div>
        <div class="time">${c.name}</div>
        <div class="sub">${details || "—"}</div>
        <div class="sub tiny">${[c.model,
          c.firmware ? "micrologiciel " + c.firmware : null, c.serial,
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
  box.innerHTML =
    `<p class="hint">${message}</p>
     <button class="watch" onclick="watch('${name}')">Réessayer</button>`;
}

function stopWatch(name) {
  const box = $("live-" + cssId(name));
  box.innerHTML = `<button class="watch" onclick="watch('${name}')">Voir en direct</button>`;
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
  const out = data.clips.filter((c) => c.excluded).length;
  $("count").textContent =
    `${clips.length} affiché(s) · ${data.clips.length - out} retenu(s) · ${out} écarté(s)`;

  if (!clips.length) {
    // Distinguer « rien ne correspond au filtre » de « rien n'a jamais été
    // récupéré » : dans le second cas, la cause la plus fréquente est l'absence
    // de clé USB sur le module, les enregistrements partant alors dans le cloud
    // de l'abonnement Blink, que cet outil ne lit pas.
    $("list").innerHTML = data.clips.length
      ? `<p class="empty">Aucun clip ne correspond à ce filtre.</p>`
      : `<p class="empty">Aucun clip récupéré pour l'instant.<br>
           Lancez « blink2video download », ou vérifiez qu'une clé USB est
           branchée sur le module : sans elle, les enregistrements ne vont que
           dans le cloud de l'abonnement Blink, que cet outil ne lit pas.</p>`;
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

async function load() {
  const [answer, videoAnswer] = await Promise.all([
    fetch("/api/clips"), fetch("/api/videos"),
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

async function toggle(identity, excluded) {
  const answer = await fetch("/api/toggle", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ identity, excluded }),
  });
  const result = await answer.json();
  if (result.error) { alert(result.error); return; }
  await load();
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
      if (!event.ok) $("phase").textContent = "Terminé avec des erreurs";
      load();
    }
  };
  source.onerror = () => {
    source.close();
    $("work").classList.remove("on");
    button.disabled = false;
    $("log").textContent += "\\nConnexion interrompue.\\n";
  };
};

for (const id of ["view", "camera", "day", "showOut"]) $(id).onchange = render;
$("view").value = "clips";   // au démarrage on montre les clips, pas d'appel réseau
load();
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
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--no-browser", action="store_true", help="ne pas ouvrir le navigateur"
    )
    return parser.parse_args()


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
    if not args.no_browser:
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
