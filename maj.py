#!/usr/bin/env python3
"""Mise à jour depuis les releases GitHub.

Deux temps, séparés parce qu'ils n'ont pas le même risque.

Le premier ne coûte rien et n'engage rien : demander à GitHub quelle est la
dernière version publiée, la comparer à la nôtre, garder la réponse en cache.
C'est ce qui allume l'indication dans l'interface.

Le second remplace l'installation. L'ordre y est dicté par une contrainte
simple : un programme ne peut pas se remplacer lui-même pendant qu'il tourne,
et sous Windows il ne peut même pas être déplacé. On télécharge donc l'archive,
on l'extrait dans le sous-dossier ``update`` de l'installation, on vérifie que
le nouvel exécutable répond, et c'est *lui* qu'on charge de finir le travail.
Lancé depuis ce dossier de préparation, hors des trois éléments remplacés, il
peut arrêter l'ancienne version, permuter les fichiers, puis relancer. Rien
n'est touché tant que la nouvelle version n'a pas prouvé qu'elle démarre.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

import runtime

DEPOT = "nico579/blink2video"
CACHE = Path(".blink_maj.json")
# Six heures : une version ne sort pas plus souvent, et l'interface ne doit pas
# interroger GitHub à chaque ouverture de page.
FRAICHEUR = 6 * 3600
DOSSIER_TRAVAIL = "update"
MARQUEUR_TRAVAIL = ".blink2video-update"
# Avant 0.10.5, les mises à jour étaient préparées à côté de l'installation.
# Conserver ce préfixe permet d'effacer leurs éventuels restes une dernière fois.
PREFIXE_TRAVAIL_HISTORIQUE = ".blink_maj_"
# Une archive officielle fait aujourd'hui environ 120 Mo. Ces plafonds ne
# servent pas a deviner sa taille (celle publiee par GitHub doit correspondre
# exactement), mais a refuser avant ecriture une metadonnee manifestement
# aberrante et a borner une archive compressee hostile.
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 100_000
MAX_CHECKSUM_BYTES = 4096
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
UPDATE_HOST_SUFFIXES = ("github.com", "githubusercontent.com")
MESSAGE_WINDOWS7 = (
    "Mise à jour automatique désactivée pour l'édition Windows 7 "
    "expérimentale : une archive Windows standard réinstallerait Python 3.12 "
    "et ne démarrerait plus sur ce système."
)


# ------------------------------------------------------------------ détection

def _numeros(version: str) -> tuple:
    """« v0.5.3 » devient (0, 5, 3), comparable à un autre tuple.

    Comparer des chaînes rangerait 0.5.10 avant 0.5.9."""
    propre = version.strip().lstrip("vV")
    morceaux = []
    for part in propre.split("."):
        chiffres = "".join(c for c in part if c.isdigit())
        morceaux.append(int(chiffres) if chiffres else 0)
    return tuple(morceaux)


def _sha256_normalise(valeur) -> str:
    """Empreinte SHA-256 canonique, ou chaîne vide si elle est impropre."""
    texte = str(valeur or "").strip()
    if texte.lower().startswith("sha256:"):
        texte = texte.split(":", 1)[1].strip()
    return texte.lower() if SHA256_RE.fullmatch(texte) else ""


def _archive_de_ce_systeme(assets: list) -> dict:
    """L'archive publiée qui correspond à cette machine, s'il y en a une."""
    if sys.platform == "win32":
        marque = "windows"
    elif sys.platform == "darwin":
        marque = "macos"
    else:
        marque = "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    par_nom = {str(asset.get("name") or ""): asset for asset in assets
               if isinstance(asset, dict)}
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        nom = str(asset.get("name", ""))
        nom_minuscule = nom.lower()
        # L'artefact Windows 7 reste manuel. Même s'il est ajouté par erreur à
        # une release, un Windows récent ne doit pas le choisir à la place de
        # l'archive officielle, qui porte elle aussi « windows » dans son nom.
        if marque == "windows" and "windows7" in nom_minuscule:
            continue
        suffixe = ".zip" if marque in ("windows", "macos") else ".tar.gz"
        if (marque in nom_minuscule and arch in nom_minuscule
                and nom_minuscule.endswith(suffixe)):
            checksum = par_nom.get(nom + ".sha256") or {}
            return {
                "nom": nom,
                "url": asset.get("browser_download_url"),
                "taille": int(asset.get("size") or 0),
                # GitHub expose aujourd'hui le digest de l'asset. Le fichier
                # compagnon reste un repli pour les réponses d'API qui ne
                # fourniraient pas encore ce champ.
                "sha256": _sha256_normalise(asset.get("digest")),
                "checksum_url": checksum.get("browser_download_url"),
            }
    return {}


def _interroger() -> dict:
    """Demande à GitHub la dernière release publiée."""
    requete = urllib.request.Request(
        f"https://api.github.com/repos/{DEPOT}/releases/latest",
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": f"blink2video/{runtime.VERSION}"})
    with urllib.request.urlopen(requete, timeout=10) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


def disponible(force: bool = False, reseau: bool = True) -> dict:
    """La version publiée si elle est plus récente que la nôtre, sinon rien.

    Le cache évite d'appeler GitHub à chaque question, et sert encore quand la
    machine est hors ligne : une mise à jour signalée hier reste vraie. Sans
    `reseau`, on se contente de ce cache : c'est ainsi que l'interface répond,
    une requête de page n'ayant pas à attendre GitHub."""
    if runtime.build_windows7():
        return {}

    fichier = runtime.app_dir() / CACHE
    cache = {}
    try:
        cache = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}

    age = time.time() - float(cache.get("verifie") or 0)
    if reseau and (force or age > FRAICHEUR or not cache):
        try:
            release = _interroger()
            cache = {"verifie": time.time(),
                     "version": str(release.get("tag_name") or "").lstrip("vV"),
                     "page": release.get("html_url"),
                     "archive": _archive_de_ce_systeme(release.get("assets") or [])}
            try:
                fichier.write_text(json.dumps(cache, ensure_ascii=False),
                                   encoding="utf-8")
            except OSError:
                pass
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            # Hors ligne, ou GitHub indisponible : ce n'est pas une erreur, la
            # mise à jour n'est pas une fonction vitale. On garde le cache.
            pass

    version = str(cache.get("version") or "")
    if not version or _numeros(version) <= _numeros(runtime.VERSION):
        return {}
    return {"version": version, "page": cache.get("page"),
            "archive": cache.get("archive") or {}}


# --------------------------------------------------------------- installation

def _url_mise_a_jour_autorisee(url: str) -> bool:
    """N'accepte que les hôtes HTTPS atteints après redirection par GitHub."""
    try:
        parsed = urllib.parse.urlparse(str(url))
        port = parsed.port
    except ValueError:
        return False
    hote = (parsed.hostname or "").lower().rstrip(".")
    hote_github = any(
        hote == suffixe or hote.endswith("." + suffixe)
        for suffixe in UPDATE_HOST_SUFFIXES
    )
    return (parsed.scheme == "https" and hote_github and port in (None, 443)
            and parsed.username is None and parsed.password is None)


def _url_release_officielle(url: str, nom: str) -> bool:
    """Lie l'URL initiale au dépôt, au format de tag et au fichier attendus.

    Le cache de mise à jour vit dans le dossier de données, donc son contenu ne
    constitue pas une autorité. Accepter n'importe quel dépôt GitHub permettrait
    à un cache modifié de fournir son propre binaire et sa propre empreinte.
    """
    try:
        parsed = urllib.parse.urlparse(str(url))
        chemin = urllib.parse.unquote(parsed.path)
        port = parsed.port
    except (UnicodeError, ValueError):
        return False
    morceaux = chemin.split("/")
    attendu = ["", *DEPOT.split("/"), "releases", "download"]
    return (
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and parsed.username is None and parsed.password is None
        and port is None and not parsed.params
        and not parsed.query and not parsed.fragment
        and len(morceaux) == len(attendu) + 2
        and morceaux[:len(attendu)] == attendu
        and VERSION_TAG_RE.fullmatch(morceaux[-2]) is not None
        and morceaux[-1] == nom
    )


def _lire_empreinte(url: str, nom_archive: str) -> str:
    """Lit le petit fichier ``<archive>.sha256`` publié avec l'archive."""
    if not _url_release_officielle(url, nom_archive + ".sha256"):
        raise OSError("URL d'empreinte étrangère à la release officielle.")
    requete = urllib.request.Request(
        url, headers={"User-Agent": f"blink2video/{runtime.VERSION}"})
    with urllib.request.urlopen(requete, timeout=15) as reponse:
        finale = getattr(reponse, "geturl", lambda: url)()
        if not _url_mise_a_jour_autorisee(finale):
            raise OSError("La redirection de l'empreinte quitte GitHub ou HTTPS.")
        annoncee = reponse.headers.get("Content-Length")
        if annoncee:
            try:
                annoncee = int(annoncee)
            except ValueError as erreur:
                raise OSError("Taille d'empreinte HTTP invalide.") from erreur
            if annoncee < 1 or annoncee > MAX_CHECKSUM_BYTES:
                raise OSError("Fichier d'empreinte anormalement volumineux.")
        corps = reponse.read(MAX_CHECKSUM_BYTES + 1)
        if len(corps) > MAX_CHECKSUM_BYTES:
            raise OSError("Fichier d'empreinte anormalement volumineux.")

    try:
        lignes = [ligne.strip() for ligne in corps.decode("ascii").splitlines()
                  if ligne.strip()]
    except UnicodeDecodeError as erreur:
        raise OSError("Fichier d'empreinte non ASCII.") from erreur
    if len(lignes) != 1:
        raise OSError("Fichier d'empreinte ambigu.")
    champs = lignes[0].split()
    empreinte = _sha256_normalise(champs[0] if champs else "")
    if not empreinte:
        raise OSError("Empreinte SHA-256 absente ou invalide.")
    if len(champs) > 2 or (len(champs) == 2
                           and champs[1].lstrip("*") != nom_archive):
        raise OSError("L'empreinte ne désigne pas l'archive attendue.")
    return empreinte


def _empreinte_attendue(archive: dict) -> str:
    empreinte = _sha256_normalise(archive.get("sha256"))
    if empreinte:
        return empreinte
    checksum_url = str(archive.get("checksum_url") or "")
    if checksum_url:
        return _lire_empreinte(checksum_url, str(archive.get("nom") or ""))
    raise OSError(
        "Cette release ne fournit aucune empreinte SHA-256 : mise à jour "
        "automatique refusée. Téléchargez-la manuellement depuis GitHub."
    )


def _nom_archive_sur(nom) -> str:
    nom = str(nom or "")
    if (not nom or len(nom) > 200 or "/" in nom or "\\" in nom
            or Path(nom).name != nom
            or not (nom.lower().endswith(".zip")
                    or nom.lower().endswith(".tar.gz"))):
        raise OSError(f"Nom d'archive impropre : {nom!r}")
    return nom


def _telecharger(url: str, destination: Path, taille: int, sha256: str) -> None:
    """Rapatrie l'archive en publiant son avancement.

    Le même canal que le téléchargement des clips et l'assemblage : l'interface
    montre déjà cette barre, il n'y avait rien à inventer."""
    if not _url_release_officielle(url, destination.name):
        raise OSError("URL d'archive étrangère à la release officielle.")
    if not 1 <= taille <= MAX_ARCHIVE_BYTES:
        raise OSError(f"Taille d'archive invalide ou excessive : {taille} octets.")
    sha256 = _sha256_normalise(sha256)
    if not sha256:
        raise OSError("Empreinte SHA-256 d'archive invalide.")

    requete = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream",
                      "User-Agent": f"blink2video/{runtime.VERSION}"})
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            finale = getattr(reponse, "geturl", lambda: url)()
            if not _url_mise_a_jour_autorisee(finale):
                raise OSError("La redirection de l'archive quitte GitHub ou HTTPS.")
            annoncee = reponse.headers.get("Content-Length")
            if annoncee:
                try:
                    annoncee = int(annoncee)
                except ValueError as erreur:
                    raise OSError("Taille d'archive HTTP invalide.") from erreur
                if annoncee != taille:
                    raise OSError(
                        f"Taille HTTP inattendue : {annoncee}, attendu {taille}.")

            hacheur = hashlib.sha256()
            recu = 0
            dernier = 0.0
            with destination.open("xb") as sortie:
                while True:
                    bloc = reponse.read(262144)
                    if not bloc:
                        break
                    recu += len(bloc)
                    if recu > taille or recu > MAX_ARCHIVE_BYTES:
                        raise OSError("L'archive dépasse la taille publiée.")
                    sortie.write(bloc)
                    hacheur.update(bloc)
                    if time.time() - dernier > 0.5:
                        dernier = time.time()
                        mo = recu // (1024 * 1024)
                        runtime.travail(
                            f"Téléchargement de la mise à jour ({mo} Mo)",
                            recu / (1024 * 1024), taille / (1024 * 1024),
                            cle="phase.update_download")
            if recu != taille:
                raise OSError(f"Archive tronquée : {recu} octets, attendu {taille}.")
            obtenue = hacheur.hexdigest()
            if obtenue != sha256:
                raise OSError(
                    f"Empreinte SHA-256 incorrecte : {obtenue}, attendu {sha256}.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print(f"  archive reçue : {destination.name} "
          f"({destination.stat().st_size // (1024 * 1024)} Mo)")


_NOMS_WINDOWS_INTERDITS = {
    "CON", "PRN", "AUX", "NUL", "CLOCK$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _destination_archive(racine: Path, nom: str) -> tuple:
    """Destination confinée d'un membre, avec une syntaxe portable stricte."""
    brut = str(nom or "")
    if (not brut or len(brut) > 4096 or "\x00" in brut
            or brut.startswith(("/", "\\"))):
        raise OSError(f"Chemin dangereux dans l'archive : {brut!r}")
    portable = brut.replace("\\", "/").rstrip("/")
    chemin_posix = PurePosixPath(portable)
    morceaux = portable.split("/")
    if (not portable or chemin_posix.is_absolute()
            or any(not morceau or morceau in (".", "..") for morceau in morceaux)):
        raise OSError(f"Chemin dangereux dans l'archive : {brut!r}")
    for morceau in morceaux:
        base = morceau.split(".", 1)[0].upper()
        if (len(morceau) > 255 or ":" in morceau
                or morceau.endswith((" ", "."))
                or any(ord(caractere) < 32 for caractere in morceau)
                or base in _NOMS_WINDOWS_INTERDITS):
            raise OSError(f"Nom non portable dans l'archive : {brut!r}")
    cible = racine.joinpath(*morceaux).resolve()
    if not runtime.est_relatif_a(cible, racine):
        raise OSError(f"Chemin hors du dossier d'extraction : {brut!r}")
    return cible, tuple(morceaux)


def _inscrire_destination(registre: dict, morceaux: tuple, genre: str) -> None:
    """Refuse doublons, collisions de casse et fichier utilisé comme parent."""
    for index in range(1, len(morceaux) + 1):
        nom = "/".join(morceaux[:index])
        cle = nom.casefold()
        courant = genre if index == len(morceaux) else "dir"
        precedent = registre.get(cle)
        if precedent is None:
            registre[cle] = (nom, courant)
            continue
        if precedent[0] != nom or precedent[1] != courant:
            raise OSError(f"Collision de chemins dans l'archive : {nom!r}")
        if courant != "dir":
            raise OSError(f"Membre dupliqué dans l'archive : {nom!r}")


def _copier_exactement(source, destination: Path, taille: int) -> None:
    restant = taille
    with destination.open("xb") as sortie:
        while restant:
            bloc = source.read(min(262144, restant))
            if not bloc:
                raise OSError(f"Membre tronqué dans l'archive : {destination.name}")
            sortie.write(bloc)
            restant -= len(bloc)
        if source.read(1):
            raise OSError(f"Membre plus long qu'annoncé : {destination.name}")


def _extraire_zip(archive: Path, racine: Path) -> None:
    with zipfile.ZipFile(archive) as zip_:
        infos = zip_.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise OSError("Archive contenant trop de membres.")
        registre = {}
        membres = []
        total = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            type_mode = stat.S_IFMT(mode)
            dossier = info.is_dir()
            if info.flag_bits & 0x1:
                raise OSError(f"Membre ZIP chiffré interdit : {info.filename!r}")
            if dossier:
                if type_mode not in (0, stat.S_IFDIR):
                    raise OSError(f"Type ZIP dangereux : {info.filename!r}")
                genre = "dir"
            else:
                if type_mode not in (0, stat.S_IFREG):
                    raise OSError(f"Lien ou type ZIP dangereux : {info.filename!r}")
                genre = "file"
                total += info.file_size
                if info.file_size < 0 or total > MAX_EXTRACTED_BYTES:
                    raise OSError("Contenu ZIP décompressé trop volumineux.")
            cible, morceaux = _destination_archive(racine, info.filename)
            _inscrire_destination(registre, morceaux, genre)
            membres.append((info, cible, genre))

        for _, cible, genre in membres:
            if genre == "dir":
                cible.mkdir(parents=True, exist_ok=True)
        for info, cible, genre in membres:
            if genre != "file":
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            with zip_.open(info, "r") as source:
                _copier_exactement(source, cible, info.file_size)


def _extraire_tar(archive: Path, racine: Path) -> None:
    with tarfile.open(archive) as tar:
        infos = []
        for info in tar:
            infos.append(info)
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise OSError("Archive contenant trop de membres.")
        registre = {}
        membres = []
        total = 0
        for info in infos:
            if info.isdir():
                genre = "dir"
            elif info.isfile() and not getattr(info, "sparse", None):
                genre = "file"
                total += info.size
                if info.size < 0 or total > MAX_EXTRACTED_BYTES:
                    raise OSError("Contenu TAR décompressé trop volumineux.")
            else:
                raise OSError(f"Lien ou type TAR dangereux : {info.name!r}")
            cible, morceaux = _destination_archive(racine, info.name)
            _inscrire_destination(registre, morceaux, genre)
            membres.append((info, cible, genre))

        for _, cible, genre in membres:
            if genre == "dir":
                cible.mkdir(parents=True, exist_ok=True)
        for info, cible, genre in membres:
            if genre != "file":
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(info)
            if source is None:
                raise OSError(f"Membre TAR illisible : {info.name!r}")
            with source:
                _copier_exactement(source, cible, info.size)
            # Pas de propriétaire, setuid/setgid ni mode arbitraire venant de
            # l'archive. Seul le caractère exécutable utile est conservé.
            cible.chmod(0o755 if info.mode & 0o111 else 0o644)


def _extraire(archive: Path, vers: Path) -> Path:
    """Déballe l'archive et rend le dossier du bundle qu'elle contenait."""
    vers.mkdir(parents=True, exist_ok=False)
    racine = vers.resolve()
    if archive.name.lower().endswith(".zip"):
        _extraire_zip(archive, racine)
    elif archive.name.lower().endswith(".tar.gz"):
        _extraire_tar(archive, racine)
    else:
        raise OSError(f"Format d'archive inconnu : {archive.name}")
    # Les archives publiées contiennent un unique dossier « blink2video ».
    contenu = list(vers.iterdir())
    if (len(contenu) != 1 or not contenu[0].is_dir()
            or contenu[0].is_symlink()):
        raise OSError("L'archive doit contenir un unique dossier de bundle.")
    return contenu[0]


def _executable(dossier: Path) -> Path:
    nom = "blink2video.exe" if sys.platform == "win32" else "blink2video"
    return dossier / nom


def _creer_dossier_travail(installe: Path, version: str) -> Path:
    """Crée ``installe/update/<version>`` sans réutiliser une préparation.

    Le marqueur distingue notre répertoire d'un éventuel dossier homonyme créé
    par l'utilisateur : le nettoyage récursif ne touche jamais un dossier qu'il
    ne reconnaît pas comme appartenant à blink2video.
    """
    nom_version = str(version).strip().lstrip("vV")
    if (not nom_version or nom_version in (".", "..")
            or not all(c.isascii() and (c.isalnum() or c in ".-_")
                       for c in nom_version)):
        raise OSError(f"Numéro de version impropre à un dossier : {version!r}")

    racine = installe / DOSSIER_TRAVAIL
    marqueur = racine / MARQUEUR_TRAVAIL
    if racine.exists():
        if not racine.is_dir():
            raise OSError(
                f"Le dossier {racine} existe déjà et n'appartient pas à "
                "blink2video. Renommez-le avant de relancer la mise à jour."
            )
        if not marqueur.is_file():
            # Une interruption entre mkdir() et l'écriture du marqueur laisse
            # un dossier vide : il est sûr de reprendre ce cas précis.
            if any(racine.iterdir()):
                raise OSError(
                    f"Le dossier {racine} existe déjà et n'appartient pas à "
                    "blink2video. Renommez-le avant de relancer la mise à jour."
                )
            marqueur.write_text(
                "Répertoire temporaire de mise à jour.\n", encoding="utf-8"
            )
    else:
        racine.mkdir()
        marqueur.write_text("Répertoire temporaire de mise à jour.\n", encoding="utf-8")
    travail = racine / nom_version
    # Sans exist_ok : une deuxième mise à jour simultanée ne doit jamais écrire
    # dans l'archive ou les fichiers partiels de la première.
    travail.mkdir()
    return travail


def _ligne(dossier: Path, *arguments: str) -> list:
    """Commande qui lance blink2video installé dans ce dossier.

    Depuis un bundle c'est un exécutable ; depuis les sources, l'interpréteur
    et le script. Le reste de ce module ignore la différence."""
    if runtime.frozen():
        return [str(_executable(dossier)), *arguments]
    return [sys.executable, "-u", str(dossier / f"{runtime.ENTREE}.py"), *arguments]


def _rendre_executable(dossier: Path) -> None:
    """Un zip ne transporte pas le bit d'exécution : macOS le perd, et la
    quarantaine Gatekeeper s'ajoute à toute archive téléchargée."""
    if sys.platform == "win32":
        return
    cible = _executable(dossier)
    if cible.is_file():
        cible.chmod(0o755)
    if sys.platform == "darwin":
        runtime.lancer(["xattr", "-dr", "com.apple.quarantine", str(dossier)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)


def _verifier(dossier: Path, attendue: str) -> bool:
    """Le nouvel exécutable démarre-t-il, et annonce-t-il la bonne version ?

    C'est le garde-fou de toute l'opération : tant qu'il n'a pas répondu, rien
    n'est remplacé. Une archive tronquée ou incompatible avec le système
    échoue ici, sur une installation encore intacte."""
    binaire = _executable(dossier)
    if not binaire.is_file():
        print(f"Échec : {binaire.name} absent de l'archive.")
        return False
    try:
        sortie = runtime.lancer([str(binaire), "--version"], capture_output=True,
                                text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as erreur:
        print(f"Échec : le nouvel exécutable ne démarre pas ({erreur}).")
        return False
    annonce = (sortie.stdout or "").strip()
    annonce_attendue = f"blink2video {attendue}"
    if sortie.returncode != 0 or annonce != annonce_attendue:
        print(f"Échec : le nouvel exécutable annonce « {annonce or '?'} », "
              f"on attendait « {annonce_attendue} ».")
        return False
    print(f"  vérifié : {annonce}")
    return True


# Ce qui appartient au programme, et que la mise à jour remplace. Tout le reste
# du dossier (clips, vidéos, registres, session Blink) appartient à
# l'utilisateur et n'est jamais touché.
CONTENU_DU_PROGRAMME = ("blink2video.exe", "blink2video", "_internal")


def _poser(source: Path, cible: Path) -> None:
    """Installe un fichier ou un dossier neuf à sa place définitive.

    Une copie, et non un déplacement : le programme qui exécute cette fonction
    est celui du dossier neuf, ses bibliothèques sont chargées depuis
    `_internal`, et Windows refuse de renommer un dossier dont un fichier est
    mappé en mémoire. Copier ne demande rien d'exclusif sur la source. Le
    dossier temporaire reste derrière, et le ménage se fait au passage
    suivant."""
    if source.is_dir():
        shutil.copytree(source, cible)
    else:
        shutil.copy2(source, cible)
        if os.name != "nt":
            cible.chmod(0o755)


def _permuter(neuf: Path, installe: Path) -> bool:
    """Met les fichiers neufs à la place des anciens, ou remet tout en l'état.

    Les anciens sont écartés avant d'être supprimés : si une copie échoue à
    mi-chemin, on sait revenir en arrière, ce qu'un effacement préalable
    rendrait impossible."""
    ecartes = []
    try:
        for nom in CONTENU_DU_PROGRAMME:
            source = neuf / nom
            if not source.exists():
                continue
            ancien = installe / nom
            if ancien.exists():
                retire = installe / f"{nom}.ancien"
                shutil.rmtree(retire, ignore_errors=True) if retire.is_dir() \
                    else retire.unlink(missing_ok=True)
                os.replace(ancien, retire)
                ecartes.append((retire, ancien))
            _poser(source, installe / nom)
        return True
    except OSError as erreur:
        print(f"Échec du remplacement ({erreur}). Retour à la version précédente.")
        for retire, ancien in ecartes:
            try:
                if ancien.exists():
                    shutil.rmtree(ancien, ignore_errors=True) if ancien.is_dir() \
                        else ancien.unlink(missing_ok=True)
                os.replace(retire, ancien)
            except OSError:
                pass
        return False


def _nettoyer(installe: Path) -> None:
    """Efface les restes d'une mise à jour précédente.

    Ce ménage ne peut pas se faire à la fin de l'opération : le programme qui
    permute tourne depuis ``update``, et sous Windows un exécutable ne peut pas
    effacer le dossier dont il est issu. On le fait donc au début de la suivante,
    quand plus personne n'y tient."""
    for nom in CONTENU_DU_PROGRAMME:
        reste = installe / f"{nom}.ancien"
        try:
            shutil.rmtree(reste, ignore_errors=True) if reste.is_dir() \
                else reste.unlink(missing_ok=True)
        except OSError:
            pass
    travail = installe / DOSSIER_TRAVAIL
    if (travail / MARQUEUR_TRAVAIL).is_file():
        shutil.rmtree(travail, ignore_errors=True)
    # Migration des préparations créées à côté de l'installation par les
    # versions antérieures. Elles portaient toutes ce préfixe réservé.
    for reste in installe.parent.glob(f"{PREFIXE_TRAVAIL_HISTORIQUE}*"):
        shutil.rmtree(reste, ignore_errors=True)


def _relancer(installe: Path, verbes: list) -> None:
    """Rend la machine dans l'état où la mise à jour l'a trouvée.

    On relance ce qui tournait, verbe pour verbe, plutôt que la composition
    recommandée : quelqu'un qui n'avait lancé que l'interface ne veut pas se
    retrouver avec quatre boucles."""
    commande = _ligne(installe, *[mot for groupe in verbes for mot in groupe])
    if not verbes:
        commande.append("start")
    print(f"Relance : {' '.join(commande)}", flush=True)
    runtime.demarrer(commande, cwd=str(installe),
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=(os.name != "nt"))


def finaliser(cible: Path) -> int:
    """Second temps, exécuté par la nouvelle version depuis son dossier
    temporaire : arrêter, remplacer, relancer."""
    installe = cible.resolve()
    neuf = Path(sys.executable).resolve().parent if runtime.frozen() \
        else Path(__file__).resolve().parent

    # Ce qui tourne, noté avant l'arrêt : c'est ce qu'il faudra relancer.
    fiches = runtime.lire_instances()
    verbes = (fiches[0].get("verbes") or []) if fiches else []

    print("Arrêt de la version en place…", flush=True)
    runtime.lancer(_ligne(installe, "stop"), cwd=str(installe),
                   stdin=subprocess.DEVNULL, check=False)

    # Les fichiers restent tenus quelques instants après la mort du processus,
    # le temps que le système referme ses poignées.
    for essai in range(20):
        vivants = [f for f in runtime.lire_instances()
                   if runtime.processus_vivant(int(f.get("pid") or 0))]
        if not vivants:
            break
        time.sleep(1)

    # Depuis les sources, « git pull » a déjà mis les fichiers en place : il n'y
    # a rien à permuter, seulement à relancer.
    if neuf != installe:
        for essai in range(15):
            if _permuter(neuf, installe):
                break
            time.sleep(2)
        else:
            print("La version précédente est intacte : rien n'a été remplacé.", flush=True)
            _relancer(installe, verbes)
            return 1

    print(f"Installé dans {installe}", flush=True)
    _relancer(installe, verbes)
    return 0


def _depuis_les_sources() -> int:
    """Mise à jour d'une installation en clair : le dépôt fait office d'archive.

    Même déroulé que pour un bundle, avec « git pull » à la place du
    téléchargement, et la même règle : on ne touche à rien tant que la nouvelle
    version n'est pas là, et c'est elle qui arrête et relance."""
    dossier = Path(__file__).resolve().parent
    if not (dossier / ".git").exists():
        print("Ces sources ne viennent pas d'un dépôt git : rien à tirer. "
              "Téléchargez l'archive publiée, ou clonez le dépôt.")
        return 2

    neuve = disponible(force=True)
    if not neuve:
        print(f"blink2video {runtime.VERSION} est à jour.")
        return 0

    print(f"Mise à jour {runtime.VERSION} vers {neuve['version']} (git pull)")
    tire = runtime.lancer(["git", "pull", "--ff-only"], cwd=str(dossier),
                          capture_output=True, text=True, check=False)
    print((tire.stdout or "").strip() or (tire.stderr or "").strip())
    if tire.returncode != 0:
        print("« git pull » a refusé : des modifications locales attendent "
              "peut-être. Rien n'a changé.")
        return 1

    # Notre propre VERSION est celle d'avant le tirage : c'est le fichier sur
    # disque qui dit ce qui vient d'arriver.
    obtenue = ""
    for ligne in (dossier / "runtime.py").read_text(encoding="utf-8").splitlines():
        if ligne.startswith("VERSION = "):
            obtenue = ligne.split('"')[1]
            break
    if obtenue != neuve["version"]:
        print(f"Le dépôt annonce {obtenue or '?'} après le tirage, "
              f"on attendait {neuve['version']}. Relance refusée.")
        return 1

    print("Passage à la nouvelle version…")
    runtime.demarrer(
        [sys.executable, "-u", str(dossier / "maj.py"), "--finaliser", str(dossier)],
        cwd=str(dossier), stdin=subprocess.DEVNULL,
        stdout=(dossier / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def installer(force: bool = False) -> int:
    """Premier temps : chercher, télécharger, vérifier, puis passer la main."""
    if runtime.build_windows7():
        print(MESSAGE_WINDOWS7)
        return 0
    if not runtime.frozen():
        return _depuis_les_sources()

    installe = Path(sys.executable).resolve().parent
    _nettoyer(installe)

    neuve = disponible(force=True)
    if not neuve:
        print(f"blink2video {runtime.VERSION} est à jour.")
        return 0
    archive = neuve.get("archive") or {}
    if not archive.get("url"):
        print(f"La version {neuve['version']} est publiée, mais sans archive "
              f"pour ce système. Voir {neuve.get('page')}")
        return 1

    print(f"Mise à jour {runtime.VERSION} vers {neuve['version']}")
    travail = None
    try:
        nom_archive = _nom_archive_sur(archive.get("nom"))
        empreinte = _empreinte_attendue(archive)
        taille = int(archive.get("taille") or 0)
        travail = _creer_dossier_travail(installe, neuve["version"])
        fichier = travail / nom_archive
        _telecharger(str(archive["url"]), fichier, taille, empreinte)
        runtime.travail("Installation de la mise à jour", 0, 0, cle="phase.update_install")
        dossier = _extraire(fichier, travail / "contenu")
        _rendre_executable(dossier)
        if not _verifier(dossier, neuve["version"]):
            return 1
        fichier.unlink(missing_ok=True)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile,
            tarfile.TarError) as erreur:
        print(f"Échec de la mise à jour : {type(erreur).__name__}: {erreur}")
        if travail is not None:
            shutil.rmtree(travail, ignore_errors=True)
        return 1
    finally:
        runtime.fin_travail()

    # La suite appartient à la nouvelle version : elle seule peut remplacer
    # celle-ci sans se scier la branche. Détachée, car ce processus fait partie
    # de ce qu'elle va arrêter.
    print("Passage à la nouvelle version…")
    # BLINK_HOME force le dossier de données de la version relancée, celle-ci
    # tournant depuis un dossier temporaire dont l'ancre naturelle ignore le
    # blink_home.txt de l'installation réelle. Imposer `installe` tel quel
    # ramenait le dossier de données à celui de l'exécutable à chaque mise à
    # jour, même quand l'utilisateur l'avait explicitement redirigé ailleurs
    # (signalé sur Reddit, 2026-08-26) : app_dir_depuis suit ce pointeur
    # depuis `installe` au lieu de l'imposer lui-même.
    reel = runtime.app_dir_depuis(installe)
    runtime.demarrer(
        [str(_executable(dossier)), "update", "--finaliser", str(installe)],
        cwd=str(dossier), env=dict(os.environ, BLINK_HOME=str(reel)),
        stdin=subprocess.DEVNULL,
        stdout=(installe / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def main() -> int:
    analyseur = argparse.ArgumentParser(
        prog="blink2video update",
        description="Installer la dernière version publiée.")
    analyseur.add_argument("--check", action="store_true",
                           help="dire s'il existe une version plus récente, sans rien installer")
    analyseur.add_argument("--finaliser", metavar="DOSSIER",
                           help=argparse.SUPPRESS)  # usage interne
    arguments = analyseur.parse_args()

    if arguments.finaliser:
        return finaliser(Path(arguments.finaliser))
    if runtime.build_windows7():
        print(MESSAGE_WINDOWS7)
        return 0
    if arguments.check:
        neuve = disponible(force=True)
        if neuve:
            print(f"Version {neuve['version']} disponible "
                  f"(vous avez {runtime.VERSION}) : {neuve.get('page')}")
        else:
            print(f"blink2video {runtime.VERSION} est à jour.")
        return 0
    return installer()


if __name__ == "__main__":
    sys.exit(main())
