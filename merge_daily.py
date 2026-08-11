"""Fusionne les clips Blink en une vidéo par caméra et par journée locale.

L'horodatage est désormais incrusté directement dans l'image (hardsub, via le
filtre drawtext) plutôt qu'ajouté comme piste de sous-titres "soft" (mov_text).
Ce choix corrige les échecs d'envoi WhatsApp : WhatsApp n'affiche jamais les
pistes de sous-titres embarquées, et l'ancienne piste (une entrée par seconde)
gonflait l'atome moov au point de faire échouer la validation du média côté
mobile sur certains fichiers longs.

Conséquence : chaque groupe est maintenant ré-encodé (plus de -c:v copy), et
la conversion de fuseau horaire reste entièrement gérée côté Python
(zoneinfo) pour rester fiable sur toutes les plateformes. ffmpeg ne reçoit
que des epochs déjà convertis en « horloge murale locale déguisée en UTC »
et tourne avec TZ=UTC0, ce qui évite toute dépendance à une base de fuseaux
horaires IANA côté ffmpeg/OS (peu fiable sous Windows).
"""

import argparse
import datetime as dt
import hashlib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict, namedtuple
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


import runtime

BASE_DIR = runtime.app_dir()
DEFAULT_INPUT = BASE_DIR / "Blink_Clips"
DEFAULT_OUTPUT = BASE_DIR / "Blink_Daily"
DEFAULT_WEEKLY = BASE_DIR / "Blink_Weekly"
DEFAULT_MONTHLY = BASE_DIR / "Blink_Monthly"
DEFAULT_NORMALIZED = BASE_DIR / "Blink_Normalized"
DEFAULT_EXCLUDED = BASE_DIR / "Blink_Excluded"
DOWNLOAD_STATE = ".blink_download_state.json"
MERGE_STATE = ".blink_merge_state.json"
NORMALIZED_STATE = ".blink_normalized.json"
NORMALIZE_VERSION = "normalized-v1-hardsub"
RENDER_VERSION = "daily-v7-normalized"
PERIOD_VERSION = "period-v2-copy-first"

# Nom d'une vidéo journalière : 2026-08-10_jardin.mp4. Les agrégats hebdo et
# mensuel sont construits à partir de ces fichiers (déjà horodatés et
# normalisés), pas à partir des clips bruts.
DAILY_NAME = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})_(?P<camera>.+)\.mp4$")

# Ancien cache de segments, remplacé par Blink_Normalized. Supprimé au premier
# passage : c'était un dossier caché de fichiers jetables, le stock normalisé
# est maintenant une couche nommée du pipeline.
LEGACY_SEGMENT_DIR = ".blink_segments"

ClipInfo = namedtuple(
    "ClipInfo", ["created", "source", "duration", "width", "height", "fps", "has_audio"]
)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "camera"


def valid_mp4(path: Path) -> bool:
    try:
        return path.stat().st_size > 0 and b"ftyp" in path.read_bytes()[:64]
    except OSError:
        return False


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(path)


def find_ffmpeg() -> str:
    """Choisit un ffmpeg capable d'incruster du texte.

    Le premier trouvé ne convient pas forcément : les binaires livrés par
    imageio-ffmpeg pour Linux sont compilés sans libfreetype, donc sans le
    filtre drawtext, alors que ceux de Windows et de macOS l'ont. Un outil dont
    la raison d'être est d'inscrire l'heure dans l'image ne peut pas se
    contenter du premier venu : on essaie les candidats dans l'ordre et on
    retient le premier qui sait le faire.

    À défaut, on renvoie quand même le premier candidat : check_drawtext_available
    produira ensuite un message clair, plutôt qu'un « ffmpeg introuvable »
    trompeur alors qu'il y en a un."""
    candidats = []
    # En bundle, ffmpeg voyage avec l'exécutable : on le prend là en premier,
    # pour ne pas dépendre de ce qui traîne sur la machine cible.
    for motif in ("ffmpeg*.exe", "ffmpeg-*", "ffmpeg"):
        candidats += [str(c) for c in sorted(runtime.resource_dir().glob(motif))
                      if c.is_file()]
    systeme = shutil.which("ffmpeg")
    if systeme:
        candidats.append(systeme)

    vendor = runtime.resource_dir() / "_vendor"
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    try:
        import imageio_ffmpeg

        candidats.append(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError):
        pass

    if not candidats:
        raise RuntimeError(
            "FFmpeg est introuvable. Installez imageio-ffmpeg ou ajoutez ffmpeg au PATH."
        )
    for candidat in candidats:
        if has_drawtext(candidat):
            return candidat
    return candidats[0]


def has_drawtext(ffmpeg: str) -> bool:
    try:
        result = runtime.lancer(
            [ffmpeg, "-hide_banner", "-filters"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "drawtext" in (result.stdout or "")


def check_drawtext_available(ffmpeg: str) -> None:
    """Certains builds ffmpeg minimalistes (dont certains binaires portables)
    omettent libfreetype et donc le filtre drawtext. On le détecte tôt avec
    un message clair plutôt que de laisser échouer chaque fusion."""
    result = runtime.lancer(
        [ffmpeg, "-hide_banner", "-filters"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if "drawtext" not in result.stdout:
        raise RuntimeError(
            "Le binaire ffmpeg utilisé n'inclut pas le filtre drawtext "
            "(compilation sans libfreetype). Utilisez un build ffmpeg complet "
            "(ex. gyan.dev sous Windows, ou 'apt install ffmpeg' sous Linux)."
        )


def check_timestamp_rendering(ffmpeg: str, font_path: Path) -> None:
    """Incruste un horodatage de test sur une image noire et vérifie qu'il en
    reste quelque chose.

    C'est un garde-fou de bout en bout : drawtext ne signale ni un chemin de
    police mal encodé dans le filtergraph (fontconfig prend le relais en
    silence), ni un format strftime que la libc locale ignore (le texte rendu
    est alors simplement vide). Sans ce test, ces deux pannes ne se voient
    qu'en regardant la vidéo finale."""
    graph = (
        "color=c=black:s=640x120:d=0.1,"
        + drawtext_chain(quote_filter_path(font_path), 40, 0)
        + ",format=gray"
    )
    result = runtime.lancer(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-filter_complex", graph,
         "-frames:v", "1", "-f", "rawvideo", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(__import__("os").environ, TZ="UTC0"),
        check=False,
    )
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if result.returncode != 0 or stderr:
        raise RuntimeError(
            f"L'incrustation de l'horodatage échoue avec la police {font_path} :\n"
            f"{stderr or 'échec sans message'}"
        )
    if not any(result.stdout):
        raise RuntimeError(
            f"L'incrustation de l'horodatage ne dessine rien avec la police "
            f"{font_path} (image de test entièrement noire). Police illisible "
            f"par drawtext, ou format de date non supporté par la libc."
        )


def find_font(explicit: Path | None) -> Path:
    if explicit is not None:
        if explicit.is_file():
            return explicit
        raise RuntimeError(f"Police introuvable : {explicit}")

    candidates = [
        BASE_DIR / "DejaVuSans-Bold.ttf",
        BASE_DIR / "fonts" / "DejaVuSans-Bold.ttf",
    ]
    system = platform.system()
    if system == "Windows":
        windir = Path(__import__("os").environ.get("WINDIR", r"C:\Windows"))
        candidates += [
            windir / "Fonts" / "arialbd.ttf",
            windir / "Fonts" / "arial.ttf",
            windir / "Fonts" / "consolab.ttf",
        ]
    elif system == "Darwin":
        candidates += [
            Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/Library/Fonts/Arial Bold.ttf"),
        ]
    else:
        candidates += [
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
            Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        ]

    for candidate in candidates:
        if candidate.is_file():
            return candidate

    raise RuntimeError(
        "Aucune police TrueType trouvée pour l'incrustation de l'horodatage. "
        f"Placez un fichier .ttf (ex. DejaVuSans-Bold.ttf) à côté de "
        f"{Path(__file__).name}, ou passez --font /chemin/vers/police.ttf."
    )


def quote_filter_path(path: Path) -> str:
    """Encode un chemin pour une option de filtre ffmpeg (ex. drawtext:fontfile).

    Le texte d'un filtergraph subit DEUX passes de dé-échappement successives :
    d'abord le découpage de la chaîne de filtres, puis celui des arguments d'un
    filtre. Un chemin Windows ('C:\\Windows\\Fonts\\arialbd.ttf') doit donc :
      - être entre quotes simples, ce qui le protège de la 1re passe ;
      - avoir ses ':' échappés en '\\:', consommés par la 2e passe ;
      - n'utiliser que des '/' comme séparateur, sinon les '\\' sont consommés
        de façon incohérente entre les deux passes (Windows accepte '/').
    """
    text = path.as_posix().replace("'", "'\\''").replace(":", "\\:")
    return f"'{text}'"


def parse_created_at(value: str) -> dt.datetime:
    created = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    return created.astimezone(dt.timezone.utc)


def load_groups(input_dir: Path, timezone: ZoneInfo) -> dict:
    """Regroupe les fichiers enregistrés par le téléchargeur incrémental."""
    state_path = input_dir / DOWNLOAD_STATE
    state = load_json(state_path, {})
    entries = state.get("clips")
    if not isinstance(entries, dict):
        raise RuntimeError(f"Registre de téléchargement absent ou invalide : {state_path}")

    root = input_dir.resolve()
    groups = defaultdict(list)
    for entry in entries.values():
        try:
            if entry.get("excluded"):
                continue
            source = (root / entry["path"]).resolve()
            if not source.is_relative_to(root) or not valid_mp4(source):
                continue
            created = parse_created_at(entry["created_at"])
            camera = str(entry.get("camera") or "camera").strip() or "camera"
        except (KeyError, TypeError, ValueError, OSError):
            continue

        local_day = created.astimezone(timezone).date().isoformat()
        groups[(camera, local_day)].append((created, source))

    for clips in groups.values():
        clips.sort(key=lambda item: (item[0], str(item[1]).casefold()))
    return dict(groups)


def group_fingerprint(keys: list) -> str:
    """Empreinte d'une journée : la liste ordonnée des clés de rendu de ses
    segments normalisés. La journalière n'étant qu'une concaténation de ces
    segments, elle est à jour exactement quand cette liste n'a pas bougé."""
    digest = hashlib.sha256()
    digest.update((RENDER_VERSION + "\n").encode("utf-8"))
    for key in keys:
        digest.update((key + "\n").encode("utf-8"))
    return digest.hexdigest()


def probe_clip_info(ffmpeg: str, source: Path) -> tuple[float, int, int, float, bool]:
    """Lit durée, résolution, fps et présence d'audio depuis l'en-tête du MP4."""
    result = runtime.lancer(
        [ffmpeg, "-hide_banner", "-i", str(source)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    stderr = result.stderr

    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
    if not duration_match:
        raise RuntimeError(f"durée introuvable pour {source.name}")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    if duration <= 0:
        raise RuntimeError(f"durée invalide pour {source.name}")

    video_line_match = re.search(r"Stream #\d+:\d+[^\n]*Video:[^\n]*", stderr)
    if not video_line_match:
        raise RuntimeError(f"flux vidéo introuvable pour {source.name}")
    video_line = video_line_match.group(0)

    size_match = re.search(r"(\d{2,5})x(\d{2,5})", video_line)
    if not size_match:
        raise RuntimeError(f"résolution introuvable pour {source.name}")
    width, height = int(size_match.group(1)), int(size_match.group(2))

    fps_match = re.search(r"([\d.]+)\s*fps", video_line)
    fps = float(fps_match.group(1)) if fps_match else 15.0

    has_audio = bool(re.search(r"Stream #\d+:\d+[^\n]*Audio:", stderr))

    return duration, width, height, fps, has_audio


def resolve_identity(
    input_dir: Path, normalized_dir: Path, excluded_dir: Path, value: str
) -> str:
    """Retrouve l'identité d'un clip à partir d'un chemin donné à la main.

    On accepte le brut, le normalisé ou l'écarté indifféremment : les trois
    arborescences sont des miroirs, la partie relative est la même. En pratique
    on désigne le clip qu'on vient de regarder."""
    # Espaces et fins de ligne rognés : un chemin arrive souvent d'un
    # copier-coller ou d'une liste, et un « \r » traînant est invisible à
    # l'affichage tout en faisant échouer la correspondance.
    path = Path(value.strip()).resolve()
    roots = (input_dir.resolve(), normalized_dir.resolve(), excluded_dir.resolve())
    for root in roots:
        if path.is_relative_to(root):
            return path.relative_to(root).as_posix()
    raise RuntimeError(
        "Chemin situé hors de "
        + ", ".join(root.name for root in roots)
        + f" : {value}"
    )


def move_aside(source: Path, destination: Path) -> None:
    """Déplace un fichier en créant l'arborescence, sans jamais écraser."""
    if not source.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        source.unlink()
        return
    shutil.move(str(source), str(destination))


def set_excluded(
    input_dir: Path,
    normalized_dir: Path,
    excluded_dir: Path,
    values: list,
    excluded: bool,
) -> int:
    """Écarte des clips, ou les réintègre.

    Exclure ne détruit rien : le brut est déplacé dans Blink_Excluded, sa
    version normalisée est effacée (elle se refabrique à partir du brut), et
    une pierre tombale est posée dans le registre de téléchargement pour que
    blink.py ne le rapatrie plus. La journalière concernée voit alors sa liste
    de segments changer, donc son empreinte, donc elle est réassemblée au
    passage suivant, et les agrégats avec elle.

    Réintégrer fait le chemin inverse et ramène le brut à sa place : le clip
    est re-normalisé au même passage, sans dépendre de la rétention du hub.
    Vider Blink_Excluded reste un geste séparé et explicite."""
    state_path = input_dir / DOWNLOAD_STATE
    state = load_json(state_path, {})
    clips = state.get("clips")
    if not isinstance(clips, dict):
        raise RuntimeError(f"Registre de téléchargement absent ou invalide : {state_path}")

    changed = 0
    for value in values:
        identity = resolve_identity(input_dir, normalized_dir, excluded_dir, value)
        keys = [
            key
            for key, entry in clips.items()
            if isinstance(entry, dict) and entry.get("path") == identity
        ]
        if not keys:
            print(f"  Inconnu du registre, ignoré : {identity}")
            continue
        for key in keys:
            if excluded:
                clips[key]["excluded"] = True
                clips[key]["excluded_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                move_aside(input_dir / identity, excluded_dir / identity)
                (normalized_dir / identity).unlink(missing_ok=True)
                print(f"  Exclu : {identity}")
            else:
                clips[key].pop("excluded", None)
                clips[key].pop("excluded_at", None)
                move_aside(excluded_dir / identity, input_dir / identity)
                if (input_dir / identity).exists():
                    print(f"  Réintégré : {identity}")
                else:
                    print(f"  Réintégré : {identity} (brut absent, relancer blink.py)")
            changed += 1

    save_json(state_path, state)
    return changed


def clip_identity(input_dir: Path, source: Path) -> str:
    """Identité stable d'un clip : son chemin relatif au dossier de
    téléchargement. C'est aussi le chemin de sa version normalisée, les deux
    arborescences étant des miroirs : la correspondance entre un brut et son
    segment se lit à l'œil, sans consulter de registre."""
    return source.resolve().relative_to(input_dir.resolve()).as_posix()


def stat_tag(source: Path) -> str:
    stat = source.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def clip_info(ffmpeg: str, registry: dict, identity: str, created, source: Path) -> ClipInfo:
    """Caractéristiques d'un clip, mémorisées dans le registre.

    Analyser un clip coûte un lancement de ffmpeg ; comme le résultat ne change
    pas tant que le fichier ne change pas, on le garde. C'est aussi ce qui
    permettra plus tard de raisonner sur un clip dont le brut a été effacé."""
    entry = registry["clips"].get(identity) or {}
    tag = stat_tag(source)
    probe = entry.get("probe")
    if entry.get("stat") != tag or not isinstance(probe, dict):
        duration, width, height, fps, has_audio = probe_clip_info(ffmpeg, source)
        probe = {
            "duration": duration, "width": width, "height": height,
            "fps": fps, "has_audio": has_audio,
        }
        entry.update({"stat": tag, "probe": probe})
        registry["clips"][identity] = entry
    return ClipInfo(
        created=created,
        source=source,
        duration=probe["duration"],
        width=probe["width"],
        height=probe["height"],
        fps=probe["fps"],
        has_audio=probe["has_audio"],
    )


def camera_target(registry: dict, camera: str, infos: list) -> tuple:
    """Résolution et cadence cibles d'une caméra, mémorisées et monotones.

    Les calculer par journée les ferait osciller : un jour sans clip haute
    définition ramènerait la cible vers le bas, ce qui invaliderait tous les
    segments déjà encodés puis les ré-invaliderait au clip suivant. On les fixe
    donc par caméra, et on ne les laisse que monter. Un clip de meilleure
    définition relève la cible et provoque un ré-encodage complet de la caméra,
    ce qui est le comportement voulu : mieux vaut letterboxer les anciens que
    rétrécir les nouveaux."""
    stored = registry["cameras"].get(camera) or {}
    width = max([info.width for info in infos] + [int(stored.get("width", 0))])
    height = max([info.height for info in infos] + [int(stored.get("height", 0))])
    fps = round(max([info.fps for info in infos] + [float(stored.get("fps", 0))]), 3)
    registry["cameras"][camera] = {"width": width, "height": height, "fps": fps}
    return width, height, fps


def wall_clock_epoch(moment_utc: dt.datetime, timezone: ZoneInfo) -> int:
    """Convertit un instant UTC en epoch « déguisé » : les chiffres de
    l'heure locale (calculés correctement via zoneinfo côté Python) sont
    réinterprétés comme si c'était de l'UTC. En lançant ensuite ffmpeg avec
    TZ=UTC0, drawtext affiche exactement cette heure locale sans jamais
    consulter de base de fuseaux horaires côté ffmpeg/OS."""
    local = moment_utc.astimezone(timezone)
    naive_as_utc = local.replace(tzinfo=dt.timezone.utc)
    return int(naive_as_utc.timestamp())


def drawtext_chain(font_value: str, fontsize: int, epoch: int) -> str:
    """Filtre drawtext affichant une horloge murale partant de `epoch`.

    %X (heure complète) plutôt que %T ou %H:%M:%S : d'une part le strftime de
    MSVC ne connaît pas %T et rend alors une chaîne vide sans la moindre
    erreur, d'autre part un ':' littéral ne survit pas aux dé-échappements
    successifs du filtergraph. ffmpeg n'appelant pas setlocale, %X reste en
    locale « C », soit HH:MM:SS partout."""
    text_expr = f"'%{{pts\\:localtime\\:{epoch}\\:%d/%m/%Y %X}}'"
    return (
        f"drawtext=fontfile={font_value}:fontsize={fontsize}:fontcolor=white:"
        f"box=1:boxcolor=black@0.55:boxborderw=8:x=20:y=h-th-20:text={text_expr}"
    )


def build_batch_filter(
    batch: list,
    target_w: int,
    target_h: int,
    target_fps: float,
    timezone: ZoneInfo,
    font_value: str | None,
) -> str:
    """Construit le filtergraph d'un lot : normalisation + horodatage incrusté
    sur chaque clip, puis concaténation.

    Avec font_value=None l'horodatage est omis : c'est le mode utilisé pour
    recoller des vidéos déjà horodatées (agrégats hebdo / mensuel) dont les
    paramètres diffèrent et qui doivent donc être ré-encodées.

    Les clips sans piste audio reçoivent un silence généré *dans* le graphe par
    une source anullsrc dédiée. Une entrée lavfi partagée ne conviendrait pas :
    une sortie de filtre ne peut être branchée qu'une seule fois, donc deux
    clips muets dans le même lot rendraient le graphe invalide.
    """
    fontsize = max(18, target_h // 18)
    chains = []
    for i, clip in enumerate(batch):
        overlay = ""
        if font_value is not None:
            epoch = wall_clock_epoch(clip.created, timezone)
            overlay = "," + drawtext_chain(font_value, fontsize, epoch)
        chains.append(
            f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,"
            f"fps={target_fps},setpts=PTS-STARTPTS"
            + overlay
            + f"[v{i}]"
        )
        if clip.has_audio:
            chains.append(
                f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )
        else:
            chains.append(
                f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                f"atrim=0:{clip.duration:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )

    concat_inputs = "".join(f"[v{i}][a{i}]" for i in range(len(batch)))
    chains.append(f"{concat_inputs}concat=n={len(batch)}:v=1:a=1[outv][outa]")
    return ";".join(chains)


def run_ffmpeg_batch(
    ffmpeg: str,
    batch: list,
    target_w: int,
    target_h: int,
    target_fps: float,
    timezone: ZoneInfo,
    font_value: str,
    preset: str,
    crf: int,
    output_path: Path,
    on_progress=None,
) -> tuple[bool, str]:
    filter_graph = build_batch_filter(
        batch, target_w, target_h, target_fps, timezone, font_value
    )

    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    for clip in batch:
        command += ["-i", str(clip.source)]
    command += [
        "-filter_complex", filter_graph,
        "-map", "[outv]",
        "-map", "[outa]",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-preset", preset,
        "-crf", str(crf),
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "48000",
        "-movflags", "+faststart",
        "-max_muxing_queue_size", "9999",
        # -progress écrit un flux clé=valeur lisible par un programme, sans
        # rien changer au reste. C'est l'option prévue pour ça depuis 2012 ;
        # analyser la ligne d'état habituelle serait fragile (elle change de
        # forme selon la version et n'est pas faite pour être relue).
        "-progress", "pipe:1", "-nostats",
        str(output_path),
    ]
    env = dict(__import__("os").environ, TZ="UTC0")
    process = runtime.demarrer(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        # bufsize=1 : lecture ligne par ligne. Sans lui Python remplit un
        # tampon avant de rendre la main, et l'avancement arrive par paquets.
        bufsize=1,
        env=env,
    )

    # stderr est vidé par un fil dédié : s'il se remplit pendant qu'on lit
    # stdout, ffmpeg se bloque sur son écriture et le tout s'immobilise.
    captured = []
    drain = threading.Thread(target=lambda: captured.append(process.stderr.read()))
    drain.start()

    total = sum(clip.duration for clip in batch) or 1.0
    for line in process.stdout:
        if on_progress is None:
            continue
        name, _, value = line.strip().partition("=")
        # out_time_us et out_time_ms sont tous deux en microsecondes ; le
        # second est mal nommé et conservé par compatibilité.
        if name in ("out_time_us", "out_time_ms") and value.isdigit():
            on_progress(min(int(value) / 1_000_000 / total, 1.0))

    process.wait()
    drain.join(timeout=5)
    stderr = "".join(part for part in captured if part).strip()
    if process.returncode != 0 or not valid_mp4(output_path):
        return False, stderr or "FFmpeg n'a pas produit un MP4 valide"
    return True, ""


def concat_copy(ffmpeg: str, parts: list, destination: Path) -> tuple[bool, str]:
    """Recolle des lots déjà ré-encodés (même codec/résolution/fps) par
    simple copie de flux : rapide, sans perte."""
    list_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".ffconcat",
            prefix="blink_",
            dir=destination.parent,
            encoding="utf-8",
            newline="\n",
            delete=False,
        ) as concat_file:
            list_path = Path(concat_file.name)
            for part in parts:
                escaped = part.resolve().as_posix().replace("'", "'\\''")
                concat_file.write(f"file '{escaped}'\n")

        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-c", "copy", "-movflags", "+faststart",
            str(destination),
        ]
        result = runtime.lancer(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode != 0 or not valid_mp4(destination):
            message = result.stderr.strip() or "FFmpeg n'a pas produit un MP4 valide"
            return False, message
        return True, ""
    finally:
        if list_path is not None:
            list_path.unlink(missing_ok=True)


def progress_printer(label: str):
    """Fabrique un rapporteur d'avancement pour un encodage.

    Sur un terminal la ligne est réécrite en place, comme n'importe quel outil
    en ligne de commande. Dans un tube (review.py qui relit la sortie), un
    simple retour chariot n'émettrait aucune ligne et rien n'arriverait avant
    la fin : on écrit alors des lignes complètes, que le lecteur reconnaît à
    leur forme et n'ajoute pas au journal."""
    interactive = sys.stdout.isatty()
    state = {"percent": -1, "when": 0.0}

    def report(fraction: float) -> None:
        percent = int(fraction * 100)
        now = time.monotonic()
        if percent < state["percent"] + 2 and percent < 100:
            return
        if now - state["when"] < 0.5 and percent < 100:
            return
        if percent == state["percent"]:
            return
        state["percent"], state["when"] = percent, now
        if interactive:
            print(f"\r  {label} {percent:3d}%", end="", flush=True)
            if percent >= 100:
                print()
        else:
            print(f"  {label} {percent}%", flush=True)

    return report


def render_key(
    identity: str,
    clip: ClipInfo,
    target: tuple,
    epoch: int,
    font_path: Path,
    preset: str,
    crf: int,
) -> str:
    """Empreinte de rendu d'un segment normalisé : tout ce qui change son
    contenu doit figurer ici, sinon un segment périmé serait réutilisé
    silencieusement."""
    target_w, target_h, target_fps = target
    payload = "|".join(
        [
            NORMALIZE_VERSION,
            identity,
            stat_tag(clip.source),
            f"{target_w}x{target_h}@{target_fps}",
            str(epoch),
            font_path.as_posix(),
            preset,
            str(crf),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_clip(
    ffmpeg: str,
    timezone: ZoneInfo,
    registry: dict,
    normalized_dir: Path,
    identity: str,
    clip: ClipInfo,
    target: tuple,
    key: str,
    font_path: Path,
    preset: str,
    crf: int,
    force: bool,
    on_progress=None,
) -> tuple[bool, str, bool]:
    """Produit la version normalisée et horodatée d'un clip, si nécessaire.

    Renvoie (ok, erreur, ré-encodé). Un segment déjà présent dont la clé de
    rendu correspond est laissé tel quel : c'est ce qui rend la fusion
    réellement incrémentale."""
    destination = normalized_dir / identity
    entry = registry["clips"].get(identity) or {}
    if not force and entry.get("key") == key and valid_mp4(destination):
        return True, "", False

    destination.parent.mkdir(parents=True, exist_ok=True)
    # Extension .mp4 conservée : ffmpeg choisit son format de sortie d'après
    # elle, un simple suffixe .tmp le laisserait sans muxer.
    pending = destination.with_name(destination.stem + ".tmp.mp4")
    pending.unlink(missing_ok=True)
    target_w, target_h, target_fps = target
    ok, error = run_ffmpeg_batch(
        ffmpeg, [clip], target_w, target_h, target_fps, timezone,
        quote_filter_path(font_path), preset, crf, pending, on_progress,
    )
    if not ok:
        pending.unlink(missing_ok=True)
        return False, error, False

    pending.replace(destination)
    entry["key"] = key
    entry["normalized_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    registry["clips"][identity] = entry
    return True, "", True


def merge_group(
    ffmpeg: str,
    segments: list,
    destination: Path,
) -> tuple[bool, str]:
    """Assemble la vidéo d'une journée à partir de ses segments normalisés.

    Il n'y a plus rien à encoder ici : les segments partagent codec, résolution
    et cadence, donc une simple copie de flux suffit. La journalière est une
    vue du stock normalisé, au même titre que l'hebdomadaire et la mensuelle."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_video = destination.with_name(destination.stem + ".tmp.mp4")
    temporary_video.unlink(missing_ok=True)
    try:
        ok, error = concat_copy(ffmpeg, segments, temporary_video)
        if not ok:
            return False, error
        if not valid_mp4(temporary_video):
            return False, "FFmpeg n'a pas produit un MP4 valide"
        temporary_video.replace(destination)
        return True, ""
    finally:
        temporary_video.unlink(missing_ok=True)


def prune_normalized(normalized_dir: Path, registry: dict, keep: set) -> int:
    """Supprime les segments qu'aucun clip connu ne revendique plus, ainsi que
    les entrées de registre correspondantes.

    On ne le fait qu'après un passage complet : avec un filtrage --camera ou
    --date, les groupes non traités n'auraient pas déclaré leurs segments et
    seraient effacés à tort."""
    if not normalized_dir.is_dir():
        return 0
    removed = 0
    for candidate in normalized_dir.rglob("*.mp4*"):
        if candidate.is_file() and candidate not in keep:
            candidate.unlink(missing_ok=True)
            removed += 1
    for directory in sorted(
        (p for p in normalized_dir.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    known = {
        identity
        for identity in registry["clips"]
        if (normalized_dir / identity) in keep
    }
    for identity in list(registry["clips"]):
        if identity not in known:
            del registry["clips"][identity]
    return removed


def collect_dailies(output_dir: Path) -> dict:
    """Inventorie les vidéos journalières déjà produites, par caméra.

    On lit le disque plutôt que le registre de fusion : les agrégats restent
    ainsi corrects même si les journalières ont été construites par plusieurs
    exécutions filtrées (--date, --camera)."""
    dailies = defaultdict(list)
    if not output_dir.is_dir():
        return {}
    for camera_dir in sorted(
        p for p in output_dir.iterdir() if p.is_dir() and not p.name.startswith(".")
    ):
        for video in sorted(camera_dir.glob("*.mp4")):
            match = DAILY_NAME.match(video.name)
            if not match or not valid_mp4(video):
                continue
            try:
                day = dt.date.fromisoformat(match.group("day"))
            except ValueError:
                continue
            dailies[camera_dir.name].append((day, video))
    return dict(dailies)


def period_label(day: dt.date, period: str) -> str:
    """Étiquette de la période contenant `day`.

    Semaine ISO 8601 (lundi-dimanche, format YYYY-Www) : c'est la convention
    des dates ISO déjà utilisée partout ailleurs dans le projet, et elle évite
    l'ambiguïté du « début de semaine » selon les pays."""
    if period == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    return f"{day.year}-{day.month:02d}"


def period_fingerprint(parts: list) -> str:
    digest = hashlib.sha256()
    digest.update((PERIOD_VERSION + "\n").encode("utf-8"))
    for part in parts:
        stat = part.stat()
        digest.update(f"{part.name}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8"))
    return digest.hexdigest()


def concat_videos(
    ffmpeg: str,
    timezone: ZoneInfo,
    parts: list,
    destination: Path,
    preset: str,
    crf: int,
) -> tuple[bool, str]:
    """Recolle des vidéos déjà horodatées.

    Chemin rapide : si toutes partagent résolution et cadence, une simple copie
    de flux suffit (pas de perte, quelques secondes). Sinon on ré-encode via le
    filtre concat, seule façon fiable d'assembler des sources hétérogènes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.stem + ".tmp.mp4")
    temporary.unlink(missing_ok=True)

    try:
        # On tente la copie de flux d'abord, sans rien mesurer : les
        # journalières d'une caméra sortent toutes du même stock normalisé,
        # donc du même codec, de la même résolution et de la même cadence.
        #
        # Surtout, ne pas décider d'après les cadences mesurées : la cadence
        # moyenne d'un fichier concaténé vaut images / durée, et elle varie de
        # quelques centièmes d'une journée à l'autre (30.00, 30.03, 30.04 ici)
        # parce qu'une caméra Blink ne livre pas un flux parfaitement régulier.
        # Ce test déclarait donc « hétérogène » un stock parfaitement homogène,
        # et ré-encodait la semaine et le mois entiers à chaque passage.
        ok, error = concat_copy(ffmpeg, parts, temporary)
        if not ok:
            print("  Copie de flux refusée, ré-encodage de la période")
            try:
                infos = []
                for part in parts:
                    duration, width, height, fps, has_audio = probe_clip_info(ffmpeg, part)
                    infos.append(
                        ClipInfo(None, part, duration, width, height, fps, has_audio)
                    )
            except RuntimeError as error:
                return False, str(error)
            ok, error = run_ffmpeg_batch(
                ffmpeg, infos,
                max(i.width for i in infos),
                max(i.height for i in infos),
                round(max(i.fps for i in infos), 3),
                timezone, None, preset, crf, temporary,
            )
        if not ok:
            return False, error
        if not valid_mp4(temporary):
            return False, "FFmpeg n'a pas produit un MP4 valide"
        temporary.replace(destination)
        return True, ""
    finally:
        temporary.unlink(missing_ok=True)


def build_periods(
    ffmpeg: str,
    timezone: ZoneInfo,
    output_dir: Path,
    period_dir: Path,
    period: str,
    force: bool,
    preset: str,
    crf: int,
) -> tuple[int, int, int]:
    """Assemble les vidéos journalières en agrégats hebdomadaires ou mensuels."""
    dailies = collect_dailies(output_dir)
    if not dailies:
        return 0, 0, 0

    state_path = period_dir / MERGE_STATE
    state = load_json(state_path, {"version": 1, "groups": {}})
    state.setdefault("groups", {})

    built = skipped = failed = 0
    for camera, entries in sorted(dailies.items()):
        buckets = defaultdict(list)
        for day, video in entries:
            buckets[period_label(day, period)].append(video)

        for label, parts in sorted(buckets.items()):
            destination = period_dir / camera / f"{label}_{camera}.mp4"
            key = f"{camera}|{label}"
            fingerprint = period_fingerprint(parts)

            if (
                not force
                and state["groups"].get(key, {}).get("fingerprint") == fingerprint
                and valid_mp4(destination)
            ):
                print(f"Déjà à jour : {destination.name} ({len(parts)} jour(s))")
                skipped += 1
                continue

            print(f"Assemblage : {label} / {camera} / {len(parts)} jour(s)")
            ok, error = concat_videos(
                ffmpeg, timezone, parts, destination, preset, crf
            )
            if not ok:
                print(f"  Échec : {error}")
                failed += 1
                continue

            state["groups"][key] = {
                "fingerprint": fingerprint,
                "path": destination.relative_to(period_dir).as_posix(),
                "days": len(parts),
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            save_json(state_path, state)
            print(f"  Créé : {destination}")
            built += 1

    return built, skipped, failed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fusion incrémentale des clips Blink par caméra : une vidéo "
                    "par jour, puis agrégats par semaine ISO et par mois."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--weekly-output", type=Path, default=DEFAULT_WEEKLY)
    parser.add_argument("--monthly-output", type=Path, default=DEFAULT_MONTHLY)
    parser.add_argument(
        "--normalized-output", type=Path, default=DEFAULT_NORMALIZED,
        help="stock des clips normalisés et horodatés, dont toutes les vidéos "
             "assemblées sont issues",
    )
    parser.add_argument(
        "--no-periods", action="store_true",
        help="ne pas (re)construire les agrégats hebdomadaires et mensuels",
    )
    parser.add_argument(
        "--excluded-output", type=Path, default=DEFAULT_EXCLUDED,
        help="dossier où sont mis de côté les clips écartés",
    )
    parser.add_argument(
        "--exclude", nargs="+", metavar="CLIP", default=[],
        help="écarter des clips (chemin sous Blink_Clips, Blink_Normalized ou "
             "Blink_Excluded) : le brut part dans Blink_Excluded, le segment "
             "est effacé, le clip n'est plus retéléchargé ni assemblé",
    )
    parser.add_argument(
        "--include", nargs="+", metavar="CLIP", default=[],
        help="annuler une exclusion : le brut revient de Blink_Excluded et le clip est re-normalisé",
    )
    parser.add_argument("--timezone", default="Europe/Paris")
    runtime.ajouter_boucle(parser)
    parser.add_argument("--date", help="limiter à une date locale YYYY-MM-DD")
    parser.add_argument("--camera", help="limiter à une caméra")
    parser.add_argument(
        "--force", action="store_true", help="reconstruire même si rien n'a changé"
    )
    parser.add_argument(
        "--font", type=Path, default=None,
        help="chemin vers une police .ttf pour l'horodatage incrusté",
    )
    parser.add_argument(
        "--preset", default="veryfast",
        help="preset libx264 (ultrafast..veryslow), défaut veryfast",
    )
    parser.add_argument(
        "--crf", type=int, default=21,
        help="qualité libx264 (0-51, plus bas = meilleure qualité), défaut 21",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return runtime.repeter(lambda: _executer(args), args.loop)


def _executer(args) -> int:
    input_dir = args.input.resolve()
    output_dir = args.output.resolve()
    normalized_dir = args.normalized_output.resolve()
    excluded_dir = args.excluded_output.resolve()

    # Les exclusions s'appliquent avant tout le reste : la suite du passage
    # reconstruit alors naturellement ce qui en dépend.
    if args.exclude or args.include:
        try:
            if args.exclude:
                set_excluded(input_dir, normalized_dir, excluded_dir, args.exclude, True)
            if args.include:
                set_excluded(input_dir, normalized_dir, excluded_dir, args.include, False)
        except RuntimeError as error:
            print(f"Erreur : {error}")
            return 1

    try:
        timezone = ZoneInfo(args.timezone)
        ffmpeg = find_ffmpeg()
        check_drawtext_available(ffmpeg)
        font_path = find_font(args.font)
        check_timestamp_rendering(ffmpeg, font_path)
        groups = load_groups(input_dir, timezone)
    except (RuntimeError, ZoneInfoNotFoundError) as error:
        print(f"Erreur : {error}")
        return 1

    selected = {
        key: clips
        for key, clips in groups.items()
        if (not args.date or key[1] == args.date)
        and (not args.camera or key[0].casefold() == args.camera.casefold())
    }
    if not selected:
        print("Aucun groupe de clips à fusionner.")

    merge_state_path = output_dir / MERGE_STATE
    merge_state = load_json(merge_state_path, {"version": 1, "groups": {}})
    merge_state.setdefault("version", 1)
    merge_state.setdefault("groups", {})

    registry_path = normalized_dir / NORMALIZED_STATE
    registry = load_json(registry_path, {"version": 1, "cameras": {}, "clips": {}})
    registry.setdefault("version", 1)
    registry.setdefault("cameras", {})
    registry.setdefault("clips", {})

    legacy = output_dir / LEGACY_SEGMENT_DIR
    if legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
        print(f"Ancien cache supprimé : {legacy}")

    # Étape 1 : cibles d'encodage. Elles se calculent sur l'ensemble des clips
    # d'une caméra, y compris ceux qu'un filtre --date ou --camera exclut de la
    # construction, sinon la cible dépendrait du filtre employé.
    try:
        targets = {}
        for camera in sorted({key[0] for key in groups}):
            infos = [
                clip_info(ffmpeg, registry, clip_identity(input_dir, source), created, source)
                for key, clips in groups.items()
                if key[0] == camera
                for created, source in clips
            ]
            targets[camera] = camera_target(registry, camera, infos)
    except RuntimeError as error:
        print(f"Erreur : {error}")
        return 1
    save_json(registry_path, registry)

    # Étape 2 : plan de normalisation. On établit la liste complète avant
    # d'encoder quoi que ce soit, pour pouvoir annoncer « [3/24] » : un
    # compteur sans total connu n'apprend rien sur le temps restant, et c'est
    # ce total qui alimente la barre de progression de review.py.
    plan: dict = {}
    used_segments: set = set()
    pending: set = set()
    for (camera, day), clips in sorted(groups.items(), key=lambda item: item[0]):
        target = targets[camera]
        entries = []
        for created, source in clips:
            identity = clip_identity(input_dir, source)
            info = clip_info(ffmpeg, registry, identity, created, source)
            key = render_key(
                identity, info, target, wall_clock_epoch(created, timezone),
                font_path, args.preset, args.crf,
            )
            # Déclaré même hors sélection, pour échapper au nettoyage.
            used_segments.add(normalized_dir / identity)
            if (camera, day) not in selected:
                continue
            entries.append((identity, info, key))
            known = (registry["clips"].get(identity) or {}).get("key")
            if args.force or known != key or not valid_mp4(normalized_dir / identity):
                pending.add(identity)
        if (camera, day) in selected:
            plan[(camera, day)] = (target, entries)
    save_json(registry_path, registry)

    # Étape 2 bis : encodage des seuls segments manquants ou périmés.
    normalized: dict = {}
    encoded = reused = failed = 0
    position, total = 0, len(pending)
    if total:
        print(f"Normalisation : {total} clip(s) à encoder")
    for (camera, day), (target, entries) in sorted(plan.items(), key=lambda i: i[0]):
        keys, segments = [], []
        for identity, info, key in entries:
            report = None
            if identity in pending:
                position += 1
                print(f"  [{position}/{total}] {identity}", flush=True)
                report = progress_printer(f"[{position}/{total}]")
            ok, error, did_encode = normalize_clip(
                ffmpeg, timezone, registry, normalized_dir, identity, info,
                target, key, font_path, args.preset, args.crf, args.force,
                report,
            )
            if not ok:
                print(f"    Échec : {error}")
                failed += 1
                continue
            encoded += did_encode
            reused += not did_encode
            keys.append(key)
            segments.append(normalized_dir / identity)

        normalized[(camera, day)] = (keys, segments)
        save_json(registry_path, registry)

    print(f"Normalisation : {encoded} clip(s) encodé(s), {reused} réutilisé(s).")

    # Étape 3 : assemblage des journalières, par simple copie de flux.
    built = skipped = 0
    todo = sorted(normalized.items(), key=lambda item: item[0])
    for index, ((camera, day), (keys, segments)) in enumerate(todo, start=1):
        if not segments:
            continue
        camera_path = safe_name(camera)
        destination = output_dir / camera_path / f"{day}_{camera_path}.mp4"
        state_key = f"{camera}|{day}"
        fingerprint = group_fingerprint(keys)
        previous = merge_state["groups"].get(state_key, {})

        if (
            not args.force
            and previous.get("fingerprint") == fingerprint
            and valid_mp4(destination)
        ):
            print(f"Déjà à jour : {destination.name} ({len(segments)} clip(s))")
            skipped += 1
            continue

        print(f"  [{index}/{len(todo)}] Assemblage : {day} / {camera} / "
              f"{len(segments)} clip(s)")
        ok, error = merge_group(ffmpeg, segments, destination)
        if not ok:
            print(f"  Échec : {error}")
            failed += 1
            continue

        merge_state["groups"][state_key] = {
            "fingerprint": fingerprint,
            "path": destination.relative_to(output_dir).as_posix(),
            "clips": len(segments),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        save_json(merge_state_path, merge_state)
        print(f"  Créé : {destination}")
        built += 1

    if not args.date and not args.camera and not failed:
        removed = prune_normalized(normalized_dir, registry, used_segments)
        if removed:
            print(f"Stock normalisé : {removed} segment(s) obsolète(s) supprimé(s)")
        save_json(registry_path, registry)

    print(f"Journalières : {built} créée(s), {skipped} déjà à jour, {failed} échec(s).")

    if not args.no_periods:
        for period, period_dir in (
            ("weekly", args.weekly_output.resolve()),
            ("monthly", args.monthly_output.resolve()),
        ):
            label = "Hebdomadaires" if period == "weekly" else "Mensuelles"
            print(f"\n{label} :")
            p_built, p_skipped, p_failed = build_periods(
                ffmpeg, timezone, output_dir, period_dir, period,
                args.force, args.preset, args.crf,
            )
            failed += p_failed
            print(
                f"{label} : {p_built} créée(s), {p_skipped} déjà à jour, "
                f"{p_failed} échec(s)."
            )

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
