"""Registre incrémental des clips acquis : chargement, fusion, identité,
corrélation USB/cloud, réparation d'un média absent ou tronqué.

Extrait de blink2video.py à l'étape 8 (AUDIT-2026-08-13.md, section 20, 8.3).
Ne connaît ni la session Blink, ni comment un clip se télécharge : uniquement
comment le savoir, l'identifier et l'écrire de façon durable."""

from __future__ import annotations  # Python 3.8 (build Windows 7) : les annotations "X | None" ne s'évaluent qu'à l'écriture des chaînes, jamais à l'exécution.

import bisect
import datetime as dt
import hashlib
import json
import os
import stat as stat_module
from pathlib import Path

import runtime

from blink_models import (
    _apparier_evenements,
    _identifiant_camera,
    _identifiant_reseau,
    _meme_camera,
    clip_datetime_utc,
)

OUTPUT = runtime.app_dir() / "Blink_Clips"
STATE_FILENAME = ".blink_download_state.json"
STATE_V1_BACKUP_FILENAME = ".blink_download_state.v1.backup.json"


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


def _exclusion_a_retenir(precedente: dict, entree: dict) -> tuple:
    """État d'exclusion (excluded, excluded_at) à retenir pour un clip que
    la fusion trouve des deux côtés.

    La décision d'exclusion/réintégration (posée par set_excluded(), jamais
    par le téléchargeur) est un état à part : sur ce champ précis, seule la
    décision la plus récente doit l'emporter, dans les deux sens - une
    priorité fixe à excluded=True protégeait une exclusion contre une copie
    de downloader périmée, mais laissait une réintégration se faire défaire
    par cette même copie arrivée après coup (revue du 27/08, bug 2).

    Un excluded_at absent d'un côté ne prouve rien sur l'ordre réel : il
    peut s'agir d'une entrée neuve qui ne connaît pas ce champ, ou d'une
    donnée antérieure à son introduction. Un côté daté l'emporte donc
    toujours sur un côté muet ; seul le cas où aucun des deux n'est daté
    retombe sur le repli historique (B-04) : ne jamais laisser une copie
    muette annuler une exclusion active."""
    disque_exclu = bool(precedente.get("excluded"))
    entrant_exclu = bool(entree.get("excluded"))
    depuis_disque = precedente.get("excluded_at")
    depuis_entrant = entree.get("excluded_at")

    if disque_exclu == entrant_exclu:
        return entrant_exclu, depuis_entrant or depuis_disque
    if depuis_disque and depuis_entrant:
        if depuis_disque > depuis_entrant:
            return disque_exclu, depuis_disque
        return entrant_exclu, depuis_entrant
    if depuis_disque and not depuis_entrant:
        return disque_exclu, depuis_disque
    if depuis_entrant and not depuis_disque:
        return entrant_exclu, depuis_entrant
    return disque_exclu or entrant_exclu, None


def _ecrire_registre(state_file: Path, state: dict) -> None:
    """Superpose ses propres entrées à celles déjà sur disque, atomiquement."""
    output = state_file.parent
    fusionne = dict(state)
    disque = load_download_state(output)
    clips = dict(disque.get("clips") or {})
    for cle, entree in (state.get("clips") or {}).items():
        precedente = clips.get(cle)
        if isinstance(precedente, dict) and isinstance(entree, dict):
            excluded, excluded_at = _exclusion_a_retenir(precedente, entree)
            if (excluded, excluded_at) != (bool(entree.get("excluded")), entree.get("excluded_at")):
                entree = dict(entree)
                entree["excluded"] = excluded
                if excluded_at is not None:
                    entree["excluded_at"] = excluded_at
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


def camera_setting_key(sync, clip) -> str:
    """Clé stable d'un réglage destructif propre à une caméra."""
    network_id = _identifiant_reseau(clip, sync)
    device_id = _identifiant_camera(clip)
    material = (
        ["device", network_id, device_id]
        if device_id
        else [
            "legacy", network_id, str(getattr(sync, "sync_id", "")),
            str(getattr(clip, "name", "camera")).strip().casefold(),
        ]
    )
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"camera-v2-{digest[:32]}"


def camera_setting_key_from_entry(entry: dict) -> str:
    """Même clé à partir d'une entrée persistée du registre."""
    network_id = str(entry.get("network_id") or "")
    device_id = str(entry.get("device_id") or "")
    material = (
        ["device", network_id, device_id]
        if device_id
        else [
            "legacy", network_id, str(entry.get("sync_id") or ""),
            str(entry.get("camera") or "camera").strip().casefold(),
        ]
    )
    digest = hashlib.sha256(
        json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"camera-v2-{digest[:32]}"


def remember_download(state: dict, sync, hub_name: str, clip, output: Path,
                      target: Path, source: str = "usb") -> None:
    """Marque un clip comme acquis uniquement lorsque son fichier existe.

    La provenance est notée : une caméra couverte par un abonnement enregistre
    dans le cloud, une autre sur la clé du module. L'interface le montre par
    caméra, ce qui évite de chercher pourquoi telle caméra ne produit rien."""
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


class _ClipConnu:
    """Entrée du registre présentée comme un clip, pour le rapprochement."""

    def __init__(self, entree: dict):
        self.name = entree.get("camera") or "camera"
        self.created_at = dt.datetime.fromisoformat(entree["created_at"])
        self.id = entree.get("remote_id") or 0
        self.device_id = entree.get("device_id") or ""
        self.network_id = entree.get("network_id") or ""


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
