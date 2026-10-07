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
import contextlib
import functools
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from nico579_commons import maj_install

import runtime
from blink_tls import contexte_tls

LIBELLES = {
    "fr": {
        "aide_desc": "Installer la dernière version publiée.",
        "aide_check": "dire s'il existe une version plus récente, sans rien installer",
        "message_windows7":
            "Mise à jour automatique désactivée pour l'édition Windows 7 "
            "legacy : une archive Windows standard réinstallerait Python 3.12 "
            "et ne démarrerait plus sur ce système.",
        "empreinte_release_absente":
            "Cette release ne fournit aucune empreinte SHA-256 : mise à jour "
            "automatique refusée. Téléchargez-la manuellement depuis GitHub.",
        "archive_recue": "  archive reçue : {nom} ({mo} Mo)",
        "version_dossier_impropre": "Numéro de version impropre à un dossier : {version!r}",
        "dossier_deja_existant":
            "Le dossier {racine} existe déjà et n'appartient pas à "
            "blink2video. Renommez-le avant de relancer la mise à jour.",
        "echec_binaire_absent": "Échec : {nom} absent de l'archive.",
        "echec_binaire_ne_demarre_pas": "Échec : le nouvel exécutable ne démarre pas ({erreur}).",
        "echec_binaire_annonce":
            "Échec : le nouvel exécutable annonce « {annonce} », "
            "on attendait « {attendue} ».",
        "binaire_verifie": "  vérifié : {annonce}",
        "relance": "Relance : {commande}",
        "hors_du_service":
            "Sous le service systemd : la suite se poursuit hors de l'unité, "
            "pour survivre à son arrêt.",
        "racine_controle_illisible": "Mise à jour interrompue : racine de contrôle illisible.",
        "racines_controle_multiples":
            "Mise à jour interrompue : plusieurs racines de contrôle possibles.",
        "pas_un_depot_git":
            "Ces sources ne viennent pas d'un dépôt git : rien à tirer. "
            "Téléchargez l'archive publiée, ou clonez le dépôt.",
        "deja_a_jour": "blink2video {version} est à jour.",
        "maj_git_pull": "Mise à jour {ancienne} vers {neuve} (git pull)",
        "git_pull_refuse":
            "« git pull » a refusé : des modifications locales attendent "
            "peut-être. Rien n'a changé.",
        "depot_version_inattendue":
            "Le dépôt annonce {obtenue} après le tirage, on attendait {neuve}. "
            "Relance refusée.",
        "passage_nouvelle_version": "Passage à la nouvelle version…",
        "archive_absente_pour_ce_systeme":
            "La version {version} est publiée, mais sans archive "
            "pour ce système. Voir {page}",
        "maj_vers": "Mise à jour {ancienne} vers {neuve}",
        "echec_maj_binaire_incorrect":
            "Échec de la mise à jour : le nouvel exécutable n'a pas démarré correctement.",
        "echec_maj_exception": "Échec de la mise à jour : {type}: {erreur}",
        "version_disponible": "Version {version} disponible (vous avez {actuelle}) : {page}",
    },
    "en": {
        "aide_desc": "Install the latest published release.",
        "aide_check": "say whether a newer release exists, without installing anything",
        "message_windows7":
            "Automatic updates disabled for the legacy Windows 7 "
            "edition: a standard Windows archive would reinstall Python 3.12 "
            "and no longer start on this system.",
        "empreinte_release_absente":
            "This release provides no SHA-256 checksum: automatic update "
            "refused. Download it manually from GitHub.",
        "archive_recue": "  archive received: {nom} ({mo} MB)",
        "version_dossier_impropre": "Version number improper for a folder: {version!r}",
        "dossier_deja_existant":
            "The folder {racine} already exists and does not belong to "
            "blink2video. Rename it before running the update again.",
        "echec_binaire_absent": "Failed: {nom} missing from the archive.",
        "echec_binaire_ne_demarre_pas": "Failed: the new executable does not start ({erreur}).",
        "echec_binaire_annonce":
            "Failed: the new executable reports « {annonce} », "
            "expected « {attendue} ».",
        "binaire_verifie": "  verified: {annonce}",
        "relance": "Relaunching: {commande}",
        "hors_du_service":
            "Under the systemd service: continuing outside the unit, "
            "to survive its stop.",
        "racine_controle_illisible": "Update interrupted: control root unreadable.",
        "racines_controle_multiples":
            "Update interrupted: multiple possible control roots.",
        "pas_un_depot_git":
            "These sources don't come from a git repository: nothing to pull. "
            "Download the published archive, or clone the repository.",
        "deja_a_jour": "blink2video {version} is up to date.",
        "maj_git_pull": "Updating {ancienne} to {neuve} (git pull)",
        "git_pull_refuse":
            "« git pull » refused: local changes may be pending. "
            "Nothing has changed.",
        "depot_version_inattendue":
            "The repository reports {obtenue} after pulling, expected {neuve}. "
            "Relaunch refused.",
        "passage_nouvelle_version": "Switching to the new version…",
        "archive_absente_pour_ce_systeme":
            "Version {version} is published, but with no archive "
            "for this system. See {page}",
        "maj_vers": "Updating {ancienne} to {neuve}",
        "echec_maj_binaire_incorrect":
            "Update failed: the new executable did not start correctly.",
        "echec_maj_exception": "Update failed: {type}: {erreur}",
        "version_disponible": "Version {version} available (you have {actuelle}): {page}",
    },
}

# Les messages de la permutation (arrêt, remplacement, restauration) viennent du
# commun : un seul texte pour les quatre applications.
for _langue, _textes in maj_install.LIBELLES_PERMUTATION.items():
    LIBELLES[_langue].update(_textes)


def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


DEPOT = "nico579/blink2video"
CACHE = Path(".blink_maj.json")
# Une heure. Six heures faisaient attendre une version publiée jusqu'à une
# demi-journée (issue #35) ; une question par heure reste loin des soixante
# que l'API de GitHub accorde par heure et par adresse sans compte. Pour ne
# pas attendre du tout, le bouton « Vérifier les mises à jour » des réglages
# (verifier_maintenant). L'interface, elle, lit le cache sans jamais
# interroger GitHub à l'ouverture d'une page.
FRAICHEUR = 3600
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


# ------------------------------------------------------------------ détection

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
                "sha256": _archive().sha256_normalise(asset.get("digest")),
                "checksum_url": checksum.get("browser_download_url"),
            }
    return {}


# La recherche de la dernière release reste ICI et non dans
# nico579_commons.maj (Verificateur), contrairement à celle des trois autres
# applications : ce module est le programme de mise à jour, il doit s'importer
# et fonctionner sans AUCUNE dépendance installée (c'est justement quand les
# dépendances sont cassées qu'on en a besoin ; test_updater_reste_importable_
# sans_dependances, python -S). Et installer() a besoin de l'archive de ce
# système et du contrôle d'empreinte, que le Verificateur ne porte pas.
# Spécialisation assumée, vue en comparant les jumeaux le 2026-10-06.
def _ouvrir_github(url: str) -> dict:
    """La réponse JSON de GitHub, avec les racines de contexte_tls() (celles du
    système et de certifi : Windows 7 ne reçoit plus toutes les nouvelles)."""
    requete = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json",
                      "User-Agent": f"blink2video/{runtime.VERSION}"})
    with urllib.request.urlopen(requete, timeout=10,
                                context=contexte_tls()) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


@functools.lru_cache(maxsize=None)
def _verificateur():
    """La recherche de la dernière release est celle de nico579_commons.maj
    (Verificateur), la même pour les quatre applications ; ici, le contexte TLS
    et le cache sur disque du dossier de l'application. Importée à l'usage : ce
    module est le programme de mise à jour, il doit s'importer sans aucune
    dépendance (test_updater_reste_importable_sans_dependances)."""
    from nico579_commons import maj as maj_commune
    return maj_commune.Verificateur(
        DEPOT, runtime.VERSION, fraicheur_s=FRAICHEUR, ouvrir=_ouvrir_github,
        cache=runtime.app_dir() / CACHE)


def _version_locale() -> str:
    """La VERSION réellement sur disque, pour qui tourne depuis les sources.

    Un bundle ne peut pas diverger de lui-même en cours de route (voir
    `frozen()`, seule la mise à jour remplace ses fichiers, et elle redémarre
    toujours le processus) : `runtime.VERSION` suffit alors. Depuis les
    sources, ce fichier peut changer sous un processus déjà lancé (tirage ou
    commit pendant que blink2video tourne) ; comparer à la constante chargée
    à l'import ferait croire qu'une version déjà installée reste à venir, et
    la mise à jour déclenchée depuis ce faux positif ne trouverait plus rien
    à faire (constaté en réel : bouton « Installer » qui ne fait jamais
    rien)."""
    if runtime.frozen():
        return runtime.VERSION
    try:
        chemin = Path(__file__).resolve().parent / "runtime.py"
        for ligne in chemin.read_text(encoding="utf-8").splitlines():
            if ligne.startswith("VERSION = "):
                return ligne.split('"')[1]
    except OSError:
        pass
    return runtime.VERSION


def _conclure_sans_relance(message: str) -> None:
    """Publie une conclusion de mise à jour qui n'arrête ni ne relance rien.

    `installer()` et `_depuis_les_sources()` partagent une contrainte : ni un
    « déjà à jour », ni une archive absente, ni un `git pull` refusé ne vont
    arrêter puis relancer le serveur. La page web attend pourtant précisément
    cette disparition-puis-retour pour savoir que c'est fini (voir son
    commentaire « le serveur va disparaître puis revenir »). Sans ce signal,
    le bouton reste bloqué sur « Mise à jour… » indéfiniment, sans qu'aucune
    erreur ni « déjà à jour » ne remonte jamais (constaté en réel : clic sans
    effet visible). `total=1` : un seul pas, pas une mesure de progression,
    donc pas de fraction affichée (montrerTravail ne montre un N/N que si
    total > 1)."""
    print(message)
    runtime.travail(message, 1, 1, cle="phase.update_noop")
    runtime.fin_travail(conserver=runtime.TRAVAIL_TERMINE_VISIBLE)


def disponible(force: bool = False, reseau: bool = True) -> dict:
    """La version publiée si elle est plus récente que la nôtre, sinon rien.

    Le cache évite d'appeler GitHub à chaque question, et sert encore quand la
    machine est hors ligne : une mise à jour signalée hier reste vraie. Sans
    `reseau`, on se contente de la dernière réponse : c'est ainsi que
    l'interface répond, une requête de page n'ayant pas à attendre GitHub."""
    if runtime.build_windows7():
        return {}
    verificateur = _verificateur()
    # Depuis les sources, le fichier peut changer sous un processus lancé.
    verificateur.version_locale = _version_locale()
    verifie = verificateur.verifie_a
    if reseau and (force or verifie is None or time.time() - verifie > FRAICHEUR):
        verificateur.verifier()
    trouvee = verificateur.disponible()
    if not trouvee:
        return {}
    return {"version": trouvee["version"], "page": trouvee["page"],
            "archive": _archive_de_ce_systeme(trouvee["assets"])}


def verifier_maintenant() -> tuple:
    """Pour le bouton « Vérifier les mises à jour » des réglages : interroge
    GitHub sans attendre que le cache vieillisse (issue #35). Rend la version
    plus récente, ou {}, et si GitHub a bien répondu : la page distingue
    ainsi « déjà à jour » de « GitHub injoignable », que disponible() confond
    exprès pour le fil de fond. L'édition Windows 7 n'en propose aucune."""
    if runtime.build_windows7():
        return {}, True
    repondu = _verificateur().verifier()
    return disponible(reseau=False), repondu


# --------------------------------------------------------------- installation

def _archive():
    """nico579_commons.maj_archive : choix et validation du fichier de release,
    téléchargement vérifié, extraction sans risque. Importé à l'usage, pas à
    l'import : ce module doit rester importable sans aucune dépendance
    (test_updater_reste_importable_sans_dependances)."""
    from nico579_commons import maj_archive
    return maj_archive


@contextlib.contextmanager
def _en_langue_courante():
    """Les refus du commun portent une clé et ses valeurs ; ici ils redeviennent
    l'OSError au texte de la langue de la page, comme avant."""
    try:
        yield
    except _archive().ErreurMiseAJour as erreur:
        raise OSError(erreur.message(runtime.lire_langue())) from erreur


def _agent() -> str:
    return f"blink2video/{runtime.VERSION}"


def _url_release_officielle(url: str, nom: str) -> bool:
    """Lie l'URL initiale au dépôt, au format de tag et au fichier attendus : le
    cache de mise à jour vit dans le dossier de données, son contenu ne
    constitue pas une autorité."""
    return _archive().url_de_release(url, DEPOT, nom)


def _lire_empreinte(url: str, nom_archive: str) -> str:
    """Lit le petit fichier ``<archive>.sha256`` publié avec l'archive."""
    with _en_langue_courante():
        return _archive().lire_empreinte(
            url, nom_archive, DEPOT, agent=_agent(), ouvrir=urllib.request.urlopen,
            contexte=contexte_tls())


def _empreinte_attendue(archive: dict) -> str:
    empreinte = _archive().sha256_normalise(archive.get("sha256"))
    if empreinte:
        return empreinte
    checksum_url = str(archive.get("checksum_url") or "")
    if checksum_url:
        return _lire_empreinte(checksum_url, str(archive.get("nom") or ""))
    raise OSError(msg("empreinte_release_absente"))


def _nom_archive_sur(nom) -> str:
    with _en_langue_courante():
        return _archive().nom_archive_sur(nom)


def _telecharger(url: str, destination: Path, taille: int, sha256: str) -> None:
    """Rapatrie l'archive en publiant son avancement.

    Le même canal que le téléchargement des clips et l'assemblage : l'interface
    montre déjà cette barre, il n'y avait rien à inventer."""
    def progression(recu: int, total: int) -> None:
        runtime.travail(
            f"Téléchargement de la mise à jour ({recu // (1024 * 1024)} Mo)",
            recu / (1024 * 1024), total / (1024 * 1024), cle="phase.update_download")

    with _en_langue_courante():
        _archive().telecharger(
            url, destination, taille, sha256, depot=DEPOT, agent=_agent(),
            ouvrir=urllib.request.urlopen, contexte=contexte_tls(),
            progression=progression, taille_max=MAX_ARCHIVE_BYTES)
    print(msg("archive_recue", nom=destination.name,
              mo=destination.stat().st_size // (1024 * 1024)))


def _extraire(archive: Path, vers: Path) -> Path:
    """Déballe l'archive et rend le dossier du bundle qu'elle contenait (les
    archives publiées contiennent un unique dossier « blink2video »)."""
    with _en_langue_courante():
        return _archive().extraire(archive, vers, taille_max=MAX_EXTRACTED_BYTES,
                                   membres_max=MAX_ARCHIVE_MEMBERS)


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
        raise OSError(msg("version_dossier_impropre", version=version))

    racine = installe / DOSSIER_TRAVAIL
    marqueur = racine / MARQUEUR_TRAVAIL
    if racine.exists():
        if not racine.is_dir():
            raise OSError(msg("dossier_deja_existant", racine=racine))
        if not marqueur.is_file():
            # Une interruption entre mkdir() et l'écriture du marqueur laisse
            # un dossier vide : il est sûr de reprendre ce cas précis.
            if any(racine.iterdir()):
                raise OSError(msg("dossier_deja_existant", racine=racine))
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
        print(msg("echec_binaire_absent", nom=binaire.name))
        return False
    try:
        sortie = runtime.lancer([str(binaire), "--version"], capture_output=True,
                                text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError) as erreur:
        print(msg("echec_binaire_ne_demarre_pas", erreur=erreur))
        return False
    annonce = (sortie.stdout or "").strip()
    annonce_attendue = f"blink2video {attendue}"
    if sortie.returncode != 0 or annonce != annonce_attendue:
        print(msg("echec_binaire_annonce", annonce=annonce or "?", attendue=annonce_attendue))
        return False
    print(msg("binaire_verifie", annonce=annonce))
    return True


# Ce qui appartient au programme, et que la mise à jour remplace. Tout le reste
# du dossier (clips, vidéos, registres, session Blink) appartient à
# l'utilisateur et n'est jamais touché.
CONTENU_DU_PROGRAMME = ("blink2video.exe", "blink2video", "_internal")
# Noms historiques, gardés pour reconnaître l'état laissé par une version plus
# ancienne : le commun ajoute « .lock » au nom de la réservation.
MARQUEUR_PERMUTATION = ".blink_maj_permutation.json"
NOM_RESERVATION = ".blink_maj-installation"

RestaurationIncomplete = maj_install.RestaurationIncomplete


def _langue() -> str:
    return runtime.lire_langue()


@contextlib.contextmanager
def _reservation_installation(installe: Path):
    """Sérialise nettoyage et permutation, indépendamment du stockage."""
    with maj_install.reservation(installe, NOM_RESERVATION, langue=_langue()):
        yield


def _poser(source: Path, cible: Path) -> None:
    maj_install.poser(source, cible)


def _permuter(neuf: Path, installe: Path) -> bool:
    """Met les fichiers neufs à la place des anciens, ou remet tout en l'état
    (maj_install.permuter)."""
    return maj_install.permuter(
        neuf, installe, CONTENU_DU_PROGRAMME, marqueur=MARQUEUR_PERMUTATION,
        nom_reservation=NOM_RESERVATION, poser=lambda source, cible: _poser(source, cible),
        ecrire=lambda message: print(message, flush=True), langue=_langue())


def _nettoyer_travail(installe: Path) -> None:
    """Ce qui est propre à blink2video dans le ménage : le dossier de préparation
    et les préparations créées à côté de l'installation par les versions
    antérieures (elles portaient toutes ce préfixe réservé)."""
    travail = installe / DOSSIER_TRAVAIL
    if (travail / MARQUEUR_TRAVAIL).is_file():
        shutil.rmtree(travail, ignore_errors=True)
    for reste in installe.parent.glob(f"{PREFIXE_TRAVAIL_HISTORIQUE}*"):
        shutil.rmtree(reste, ignore_errors=True)


def _nettoyer(installe: Path) -> None:
    """Efface les restes d'une mise à jour précédente, au début de la suivante :
    le programme qui permute tourne depuis ``update``, et sous Windows un
    exécutable ne peut pas effacer le dossier dont il est issu."""
    maj_install.nettoyer_restes(
        installe, CONTENU_DU_PROGRAMME, marqueur=MARQUEUR_PERMUTATION,
        nom_reservation=NOM_RESERVATION, apres=_nettoyer_travail, langue=_langue())


def _relancer(installe: Path, verbes: list) -> None:
    """Rend la machine dans l'état où la mise à jour l'a trouvée.

    On relance ce qui tournait, verbe pour verbe, plutôt que la composition
    recommandée : quelqu'un qui n'avait lancé que l'interface ne veut pas se
    retrouver avec quatre boucles."""
    ligne = _ligne(installe, *[mot for groupe in verbes for mot in groupe])
    if not verbes:
        ligne.append("start")
    print(msg("relance", commande=" ".join(ligne)), flush=True)
    env = dict(os.environ)
    if env.pop("BLINK_UPDATE_AUTO_HOME", "") == "1":
        # Le finaliseur seul avait besoin d'une racine de données forcée.
        # Le programme installé doit de nouveau trouver seul son état.
        env.pop("BLINK_HOME", None)
    # Même chose pour les fiches : seul le finaliseur devait les chercher là
    # où la version remplacée les rangeait. Jusqu'à 0.13, c'était de toute
    # façon la racine par défaut ; depuis 0.14, garder celle d'une version
    # antérieure éloignerait l'instance relancée du dossier d'état, où stop
    # la cherchera ensuite.
    env.pop("BLINK_CONTROL_HOME", None)
    maj_install.relancer_verbes(
        Path(ligne[0]), [ligne[1:]], cwd=installe, env=env,
        demarrer=lambda commande, **options: runtime.demarrer(
            commande, start_new_session=(os.name != "nt"), **options))


def finaliser(cible: Path) -> int:
    """Retrouve les fiches de la version à remplacer, où qu'elle les range.

    Un lanceur récent fournit BLINK_CONTROL_HOME. Un ancien ne passe que
    BLINK_HOME (données), même quand les fiches sont à l'installation, et
    « update » depuis les sources ne passe rien. Candidats, donc : la racine
    par défaut (le dossier d'état depuis 0.14, dont la reprise y déplace
    aussi les fiches d'une instance plus ancienne encore en cours),
    l'installation (≤ 0.13) et BLINK_HOME. Ne pas confondre une recherche
    vide avec un arrêt réussi. Si plusieurs racines portent des fiches,
    refuser de deviner quel ensemble arrêter.
    """
    # Sous le service systemd, ce processus fait partie de ce que « stop »
    # va faire tomber : la suite se poursuit hors de l'unité (issue #35).
    # Ici plutôt qu'au lancement du finaliseur, parce que c'est l'ancienne
    # version qui le lance : une mise à jour depuis une version qui ne sait
    # pas sortir du service en profite ainsi quand même.
    import autostart
    if autostart.sortir_du_service(
            runtime.self_command("update", "--finaliser", str(cible))):
        print(msg("hors_du_service"), flush=True)
        return 0
    if os.environ.get("BLINK_CONTROL_HOME"):
        return _finaliser(cible)
    installe = cible.resolve()
    home = os.environ.get("BLINK_HOME")
    candidats = [runtime._dossier_controle(), installe]
    if home:
        candidats.append(Path(home).expanduser().resolve())
    candidats = list(dict.fromkeys(candidats))
    try:
        references = [racine for racine in candidats
                      if any((racine / runtime.INSTANCES).glob("*.json"))]
    except OSError:
        print(msg("racine_controle_illisible"), flush=True)
        return 1
    if len(references) > 1:
        print(msg("racines_controle_multiples"), flush=True)
        return 1
    controle = references[0] if references else candidats[0]
    ancien_controle = os.environ.get("BLINK_CONTROL_HOME")
    ancien_auto = os.environ.get("BLINK_UPDATE_AUTO_HOME")
    os.environ["BLINK_CONTROL_HOME"] = str(controle)
    if (home and controle == installe and Path(home).expanduser().resolve()
            == runtime.app_dir_depuis(installe)):
        os.environ["BLINK_UPDATE_AUTO_HOME"] = "1"
    try:
        return _finaliser(cible)
    finally:
        for nom, ancienne in (("BLINK_CONTROL_HOME", ancien_controle),
                               ("BLINK_UPDATE_AUTO_HOME", ancien_auto)):
            if ancienne is None:
                os.environ.pop(nom, None)
            else:
                os.environ[nom] = ancienne


def _compositions_en_cours() -> list:
    """Ce qui tourne, noté avant l'arrêt : c'est ce qu'il faudra relancer."""
    fiches = runtime.lire_instances()
    # Les enfants d'un superviseur (start) ont aussi leur fiche, mais c'est
    # lui qui les recrée : les relancer en plus doublait watch et download
    # après chaque mise à jour. Un enfant orphelin, que ne liste aucune
    # fiche, reste relancé.
    enfants = {pid for fiche in fiches for pid in (fiche.get("enfants") or [])}
    compositions = []
    vues = set()
    for fiche in fiches:
        if fiche.get("pid") in enfants:
            continue
        verbes = fiche.get("verbes") or []
        signature = tuple(tuple(groupe) for groupe in verbes)
        if signature and signature not in vues:
            vues.add(signature)
            compositions.append(verbes)
    if not compositions:
        compositions.append([])  # Même repli sur start en l'absence d'instance.
    return compositions


def _finaliser(cible: Path) -> int:
    """Second temps, exécuté par la nouvelle version depuis son dossier
    temporaire : arrêter, remplacer, relancer (maj_install.finaliser)."""
    installe = cible.resolve()
    neuf = Path(sys.executable).resolve().parent if runtime.frozen() \
        else Path(__file__).resolve().parent

    def arreter() -> bool:
        # L'ancienne version de stop ne connaît pas BLINK_CONTROL_HOME. Lui
        # transmettre aussi cette racine via BLINK_HOME évite qu'elle cherche
        # les fiches dans les données redirigées, puis annonce « rien ne tourne ».
        env_arret = dict(os.environ, BLINK_HOME=str(runtime._dossier_controle()))
        arret = runtime.lancer(_ligne(installe, "stop"), cwd=str(installe),
                               env=env_arret, stdin=subprocess.DEVNULL, check=False)
        return arret.returncode == 0

    def dire(cle: str, echec: bool, **valeurs) -> None:
        texte = valeurs["texte"] if cle == "brut" else msg(cle, **valeurs)
        if echec:
            _conclure_sans_relance(texte)
        else:
            print(texte, flush=True)

    # Depuis les sources, « git pull » a déjà mis les fichiers en place : il n'y
    # a rien à permuter, seulement à relancer (neuf == installe).
    return maj_install.finaliser(
        installe, neuf, CONTENU_DU_PROGRAMME,
        noter=_compositions_en_cours, arreter=arreter,
        # lire_instances garde aussi les fiches dont seul un enfant ou un
        # ffmpeg survit : la mort du superviseur ne suffit pas.
        vivants=lambda: bool(runtime.lire_instances()),
        relancer=lambda compositions: _relancer_tout(installe, compositions),
        dire=dire, marqueur=MARQUEUR_PERMUTATION, nom_reservation=NOM_RESERVATION,
        permutation=lambda source, destination: _permuter(source, destination),
        dormir=lambda secondes: time.sleep(secondes), langue=_langue())


def _relancer_tout(installe: Path, compositions: list) -> None:
    """Relance ce qui tournait. Sorti d'une unité systemd pour survivre à
    son arrêt (autostart.sortir_du_service), c'est elle qu'on relance :
    blink2video retrouve la garde de son service, plutôt que de tourner
    détaché dans le scope transitoire. Sinon, chaque composition, comme
    avant."""
    import autostart
    unite = autostart.relancer_service()
    if unite:
        print(msg("relance", commande=f"systemctl --user start {unite}"), flush=True)
        return
    for verbes in compositions:
        _relancer(installe, verbes)


def _depuis_les_sources() -> int:
    """Mise à jour d'une installation en clair : le dépôt fait office d'archive.

    Même déroulé que pour un bundle, avec « git pull » à la place du
    téléchargement, et la même règle : on ne touche à rien tant que la nouvelle
    version n'est pas là, et c'est elle qui arrête et relance."""
    dossier = Path(__file__).resolve().parent
    if not (dossier / ".git").exists():
        _conclure_sans_relance(msg("pas_un_depot_git"))
        return 2

    neuve = disponible(force=True)
    if not neuve:
        _conclure_sans_relance(msg("deja_a_jour", version=_version_locale()))
        return 0

    print(msg("maj_git_pull", ancienne=_version_locale(), neuve=neuve['version']))
    tire = runtime.lancer(["git", "pull", "--ff-only"], cwd=str(dossier),
                          capture_output=True, text=True, check=False)
    print((tire.stdout or "").strip() or (tire.stderr or "").strip())
    if tire.returncode != 0:
        _conclure_sans_relance(msg("git_pull_refuse"))
        return 1

    # Notre propre VERSION est celle d'avant le tirage : c'est le fichier sur
    # disque qui dit ce qui vient d'arriver.
    obtenue = _version_locale()
    if obtenue != neuve["version"]:
        _conclure_sans_relance(
            msg("depot_version_inattendue", obtenue=obtenue or "?", neuve=neuve['version']))
        return 1

    print(msg("passage_nouvelle_version"))
    runtime.demarrer(
        [sys.executable, "-u", str(dossier / "maj.py"), "--finaliser", str(dossier)],
        cwd=str(dossier), stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def installer(force: bool = False) -> int:
    """Premier temps : chercher, télécharger, vérifier, puis passer la main."""
    if runtime.build_windows7():
        print(msg("message_windows7"))
        return 0
    if not runtime.frozen():
        return _depuis_les_sources()

    installe = Path(sys.executable).resolve().parent
    try:
        _nettoyer(installe)
    except RestaurationIncomplete as erreur:
        _conclure_sans_relance(str(erreur))
        return 1

    neuve = disponible(force=True)
    if not neuve:
        _conclure_sans_relance(msg("deja_a_jour", version=runtime.VERSION))
        return 0
    archive = neuve.get("archive") or {}
    if not archive.get("url"):
        _conclure_sans_relance(
            msg("archive_absente_pour_ce_systeme", version=neuve['version'],
                page=neuve.get('page')))
        return 1

    print(msg("maj_vers", ancienne=runtime.VERSION, neuve=neuve['version']))
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
            _conclure_sans_relance(msg("echec_maj_binaire_incorrect"))
            return 1
        fichier.unlink(missing_ok=True)
    except (OSError, urllib.error.URLError, zipfile.BadZipFile,
            tarfile.TarError) as erreur:
        _conclure_sans_relance(
            msg("echec_maj_exception", type=type(erreur).__name__, erreur=erreur))
        if travail is not None:
            shutil.rmtree(travail, ignore_errors=True)
        return 1
    runtime.fin_travail()

    # La suite appartient à la nouvelle version : elle seule peut remplacer
    # celle-ci sans se scier la branche. Détachée, car ce processus fait partie
    # de ce qu'elle va arrêter.
    print(msg("passage_nouvelle_version"))
    # Le finaliseur temporaire doit connaître séparément les données et les
    # fiches de contrôle. Imposer les données via BLINK_HOME seul changeait
    # aussi la recherche des processus et rendait l'ancienne instance invisible.
    # Les nommer explicitement protège aussi d'un changement d'emplacement par
    # défaut dans la version qui arrive (0.14 a déplacé l'état). Respecter un
    # BLINK_HOME fourni par l'utilisateur, sinon retirer cette surcharge
    # temporaire à la relance.
    env = dict(os.environ, BLINK_HOME=str(runtime.app_dir()),
               BLINK_CONTROL_HOME=str(runtime._dossier_controle()))
    if not os.environ.get("BLINK_HOME"):
        env["BLINK_UPDATE_AUTO_HOME"] = "1"
    runtime.demarrer(
        [str(_executable(dossier)), "update", "--finaliser", str(installe)],
        cwd=str(dossier), env=env,
        stdin=subprocess.DEVNULL,
        stdout=(runtime.app_dir() / "maj.log").open("ab"), stderr=subprocess.STDOUT,
        start_new_session=(os.name != "nt"))
    return 0


def main() -> int:
    analyseur = argparse.ArgumentParser(
        prog="blink2video update",
        description=msg("aide_desc"))
    analyseur.add_argument("--check", action="store_true", help=msg("aide_check"))
    analyseur.add_argument("--finaliser", metavar="DOSSIER",
                           help=argparse.SUPPRESS)  # usage interne
    arguments = analyseur.parse_args()

    if arguments.finaliser:
        return finaliser(Path(arguments.finaliser))
    if runtime.build_windows7():
        print(msg("message_windows7"))
        return 0
    if arguments.check:
        neuve = disponible(force=True)
        if neuve:
            print(msg("version_disponible", version=neuve['version'],
                      actuelle=runtime.VERSION, page=neuve.get('page')))
        else:
            print(msg("deja_a_jour", version=runtime.VERSION))
        return 0
    return installer()


if __name__ == "__main__":
    sys.exit(main())
