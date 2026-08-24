#!/usr/bin/env python3
"""Mise à jour depuis les releases GitHub.

Deux temps, séparés parce qu'ils n'ont pas le même risque.

Le premier ne coûte rien et n'engage rien : demander à GitHub quelle est la
dernière version publiée, la comparer à la nôtre, garder la réponse en cache.
C'est ce qui allume l'indication dans l'interface.

Le second remplace l'installation. L'ordre y est dicté par une contrainte
simple : un programme ne peut pas se remplacer lui-même pendant qu'il tourne,
et sous Windows il ne peut même pas être déplacé. On télécharge donc l'archive,
on l'extrait à côté, on vérifie que le nouvel exécutable répond, et c'est *lui*
qu'on charge de finir le travail : lancé depuis le dossier temporaire, il ne
tient aucun fichier de l'installation, et peut donc arrêter l'ancienne version,
permuter les fichiers, puis relancer. Rien n'est touché tant que la nouvelle
version n'a pas prouvé qu'elle démarre.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import runtime

DEPOT = "nico579/blink2video"
CACHE = Path(".blink_maj.json")
# Six heures : une version ne sort pas plus souvent, et l'interface ne doit pas
# interroger GitHub à chaque ouverture de page.
FRAICHEUR = 6 * 3600
PREFIXE_TRAVAIL = ".blink_maj_"


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


def _archive_de_ce_systeme(assets: list) -> dict:
    """L'archive publiée qui correspond à cette machine, s'il y en a une."""
    if sys.platform == "win32":
        marque = "windows"
    elif sys.platform == "darwin":
        marque = "macos"
    else:
        marque = "linux"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    for asset in assets:
        nom = str(asset.get("name", ""))
        if marque in nom and (arch in nom or marque == "macos"):
            return {"nom": nom, "url": asset.get("browser_download_url"),
                    "taille": int(asset.get("size") or 0)}
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

def _telecharger(url: str, destination: Path, taille: int) -> None:
    """Rapatrie l'archive en publiant son avancement.

    Le même canal que le téléchargement des clips et l'assemblage : l'interface
    montre déjà cette barre, il n'y avait rien à inventer."""
    requete = urllib.request.Request(
        url, headers={"User-Agent": f"blink2video/{runtime.VERSION}"})
    with urllib.request.urlopen(requete, timeout=60) as reponse, \
            destination.open("wb") as sortie:
        total = int(reponse.headers.get("Content-Length") or taille or 0)
        recu = 0
        dernier = 0.0
        while True:
            bloc = reponse.read(262144)
            if not bloc:
                break
            sortie.write(bloc)
            recu += len(bloc)
            if total and time.time() - dernier > 0.5:
                dernier = time.time()
                mo = recu // (1024 * 1024)
                runtime.travail(f"Téléchargement de la mise à jour ({mo} Mo)",
                                recu / (1024 * 1024), total // (1024 * 1024),
                                cle="phase.update_download")
    print(f"  archive reçue : {destination.name} "
          f"({destination.stat().st_size // (1024 * 1024)} Mo)")


def _extraire(archive: Path, vers: Path) -> Path:
    """Déballe l'archive et rend le dossier du bundle qu'elle contenait."""
    vers.mkdir(parents=True, exist_ok=True)
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zip_:
            zip_.extractall(vers)
    else:
        with tarfile.open(archive) as tar:
            tar.extractall(vers)
    # Les archives publiées contiennent un unique dossier « blink2video ».
    contenu = [chemin for chemin in vers.iterdir() if chemin.is_dir()]
    if len(contenu) == 1:
        return contenu[0]
    return vers


def _executable(dossier: Path) -> Path:
    nom = "blink2video.exe" if sys.platform == "win32" else "blink2video"
    return dossier / nom


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
    if sortie.returncode != 0 or attendue not in annonce:
        print(f"Échec : le nouvel exécutable annonce « {annonce or '?'} », "
              f"on attendait {attendue}.")
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
    permute tourne depuis le dossier temporaire, et sous Windows un exécutable
    ne peut pas effacer le dossier dont il est issu. On le fait donc au début de
    la suivante, quand plus personne n'y tient."""
    for nom in CONTENU_DU_PROGRAMME:
        reste = installe / f"{nom}.ancien"
        try:
            shutil.rmtree(reste, ignore_errors=True) if reste.is_dir() \
                else reste.unlink(missing_ok=True)
        except OSError:
            pass
    for reste in installe.parent.glob(f"{PREFIXE_TRAVAIL}*"):
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
              f"on attendait {neuve['version']}. La relance suit quand même.")

    print("Passage à la nouvelle version…")
    runtime.demarrer(
        [sys.executable, "-u", str(dossier / "maj.py"), "--finaliser", str(dossier)],
        cwd=str(dossier), stdin=subprocess.DEVNULL,
        stdout=(dossier / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def installer(force: bool = False) -> int:
    """Premier temps : chercher, télécharger, vérifier, puis passer la main."""
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
    travail = Path(tempfile.mkdtemp(prefix=PREFIXE_TRAVAIL, dir=str(installe.parent)))
    try:
        fichier = travail / str(archive["nom"])
        _telecharger(str(archive["url"]), fichier, int(archive.get("taille") or 0))
        runtime.travail("Installation de la mise à jour", 0, 0, cle="phase.update_install")
        dossier = _extraire(fichier, travail / "contenu")
        _rendre_executable(dossier)
        if not _verifier(dossier, neuve["version"]):
            return 1
        fichier.unlink(missing_ok=True)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile,
            tarfile.TarError) as erreur:
        print(f"Échec du téléchargement : {type(erreur).__name__}: {erreur}")
        shutil.rmtree(travail, ignore_errors=True)
        return 1
    finally:
        runtime.fin_travail()

    # La suite appartient à la nouvelle version : elle seule peut remplacer
    # celle-ci sans se scier la branche. Détachée, car ce processus fait partie
    # de ce qu'elle va arrêter.
    print("Passage à la nouvelle version…")
    runtime.demarrer(
        [str(_executable(dossier)), "update", "--finaliser", str(installe)],
        cwd=str(dossier), env=dict(os.environ, BLINK_HOME=str(installe)),
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
