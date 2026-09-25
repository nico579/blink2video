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
import hashlib
import json
import os
import platform
import posixpath
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

LIBELLES = {
    "fr": {
        "aide_desc": "Installer la dernière version publiée.",
        "aide_check": "dire s'il existe une version plus récente, sans rien installer",
        "message_windows7":
            "Mise à jour automatique désactivée pour l'édition Windows 7 "
            "legacy : une archive Windows standard réinstallerait Python 3.12 "
            "et ne démarrerait plus sur ce système.",
        "empreinte_url_etrangere": "URL d'empreinte étrangère à la release officielle.",
        "empreinte_redirection": "La redirection de l'empreinte quitte GitHub ou HTTPS.",
        "empreinte_taille_http_invalide": "Taille d'empreinte HTTP invalide.",
        "empreinte_trop_volumineuse": "Fichier d'empreinte anormalement volumineux.",
        "empreinte_non_ascii": "Fichier d'empreinte non ASCII.",
        "empreinte_ambigue": "Fichier d'empreinte ambigu.",
        "empreinte_absente": "Empreinte SHA-256 absente ou invalide.",
        "empreinte_archive_incorrecte": "L'empreinte ne désigne pas l'archive attendue.",
        "empreinte_release_absente":
            "Cette release ne fournit aucune empreinte SHA-256 : mise à jour "
            "automatique refusée. Téléchargez-la manuellement depuis GitHub.",
        "nom_archive_impropre": "Nom d'archive impropre : {nom!r}",
        "archive_url_etrangere": "URL d'archive étrangère à la release officielle.",
        "archive_taille_invalide": "Taille d'archive invalide ou excessive : {taille} octets.",
        "archive_empreinte_invalide": "Empreinte SHA-256 d'archive invalide.",
        "archive_redirection": "La redirection de l'archive quitte GitHub ou HTTPS.",
        "archive_taille_http_invalide": "Taille d'archive HTTP invalide.",
        "archive_taille_http_inattendue": "Taille HTTP inattendue : {annoncee}, attendu {taille}.",
        "archive_depasse_taille": "L'archive dépasse la taille publiée.",
        "archive_tronquee": "Archive tronquée : {recu} octets, attendu {taille}.",
        "archive_empreinte_incorrecte": "Empreinte SHA-256 incorrecte : {obtenue}, attendu {sha256}.",
        "archive_recue": "  archive reçue : {nom} ({mo} Mo)",
        "archive_chemin_dangereux": "Chemin dangereux dans l'archive : {brut!r}",
        "archive_nom_non_portable": "Nom non portable dans l'archive : {brut!r}",
        "archive_chemin_hors_dossier": "Chemin hors du dossier d'extraction : {brut!r}",
        "archive_collision_chemins": "Collision de chemins dans l'archive : {nom!r}",
        "archive_membre_duplique": "Membre dupliqué dans l'archive : {nom!r}",
        "archive_membre_tronque": "Membre tronqué dans l'archive : {nom}",
        "archive_membre_plus_long": "Membre plus long qu'annoncé : {nom}",
        "archive_trop_de_membres": "Archive contenant trop de membres.",
        "archive_membre_zip_chiffre": "Membre ZIP chiffré interdit : {nom!r}",
        "archive_type_zip_dangereux": "Type ZIP dangereux : {nom!r}",
        "archive_lien_type_zip_dangereux": "Lien ou type ZIP dangereux : {nom!r}",
        "archive_zip_trop_volumineux": "Contenu ZIP décompressé trop volumineux.",
        "archive_tar_trop_volumineux": "Contenu TAR décompressé trop volumineux.",
        "archive_lien_type_tar_dangereux": "Lien ou type TAR dangereux : {nom!r}",
        "archive_lien_hors_dossier": "Lien de l'archive hors du dossier d'extraction : {nom!r} -> {cible!r}",
        "archive_membre_tar_illisible": "Membre TAR illisible : {nom!r}",
        "archive_format_inconnu": "Format d'archive inconnu : {nom}",
        "archive_bundle_unique": "L'archive doit contenir un unique dossier de bundle.",
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
        "permutation_non_finalisee":
            "Une permutation non finalisée subsiste : {marqueur}. "
            "Sauvegardes .ancien conservées ; réparation nécessaire.",
        "permutation_preparation_interrompue":
            "Préparation de permutation interrompue : {marqueur}. "
            "Aucun remplacement autorisé avant vérification.",
        "permutation_non_demarree": "Permutation non démarrée : {erreur}",
        "echec_remplacement": "Échec du remplacement ({erreur}). Retour à la version précédente.",
        "restauration_incomplete":
            "Restauration incomplète ; aucune relance ni nouvelle tentative. "
            "Conserver {marqueur} et les sauvegardes .ancien. {echecs}",
        "maj_precedente_non_finalisee":
            "Mise à jour précédente non finalisée : sauvegardes et préparation conservées.",
        "relance": "Relance : {commande}",
        "racine_controle_illisible": "Mise à jour interrompue : racine de contrôle illisible.",
        "racines_controle_multiples":
            "Mise à jour interrompue : plusieurs racines de contrôle possibles.",
        "arret_version_en_place": "Arrêt de la version en place…",
        "arret_echoue": "Mise à jour interrompue : la commande d'arrêt a échoué.",
        "instance_encore_active": "Mise à jour interrompue : une instance est encore active.",
        "version_precedente_intacte": "La version précédente est intacte : rien n'a été remplacé.",
        "installe_dans": "Installé dans {installe}",
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
        "empreinte_url_etrangere": "Checksum URL foreign to the official release.",
        "empreinte_redirection": "The checksum redirect leaves GitHub or HTTPS.",
        "empreinte_taille_http_invalide": "Invalid checksum HTTP size.",
        "empreinte_trop_volumineuse": "Checksum file abnormally large.",
        "empreinte_non_ascii": "Checksum file not ASCII.",
        "empreinte_ambigue": "Ambiguous checksum file.",
        "empreinte_absente": "Missing or invalid SHA-256 checksum.",
        "empreinte_archive_incorrecte": "The checksum does not name the expected archive.",
        "empreinte_release_absente":
            "This release provides no SHA-256 checksum: automatic update "
            "refused. Download it manually from GitHub.",
        "nom_archive_impropre": "Improper archive name: {nom!r}",
        "archive_url_etrangere": "Archive URL foreign to the official release.",
        "archive_taille_invalide": "Invalid or excessive archive size: {taille} bytes.",
        "archive_empreinte_invalide": "Invalid archive SHA-256 checksum.",
        "archive_redirection": "The archive redirect leaves GitHub or HTTPS.",
        "archive_taille_http_invalide": "Invalid archive HTTP size.",
        "archive_taille_http_inattendue": "Unexpected HTTP size: {annoncee}, expected {taille}.",
        "archive_depasse_taille": "The archive exceeds its published size.",
        "archive_tronquee": "Truncated archive: {recu} bytes, expected {taille}.",
        "archive_empreinte_incorrecte": "Incorrect SHA-256 checksum: {obtenue}, expected {sha256}.",
        "archive_recue": "  archive received: {nom} ({mo} MB)",
        "archive_chemin_dangereux": "Dangerous path in the archive: {brut!r}",
        "archive_nom_non_portable": "Non-portable name in the archive: {brut!r}",
        "archive_chemin_hors_dossier": "Path outside the extraction folder: {brut!r}",
        "archive_collision_chemins": "Path collision in the archive: {nom!r}",
        "archive_membre_duplique": "Duplicate member in the archive: {nom!r}",
        "archive_membre_tronque": "Truncated member in the archive: {nom}",
        "archive_membre_plus_long": "Member longer than announced: {nom}",
        "archive_trop_de_membres": "Archive contains too many members.",
        "archive_membre_zip_chiffre": "Encrypted ZIP member forbidden: {nom!r}",
        "archive_type_zip_dangereux": "Dangerous ZIP type: {nom!r}",
        "archive_lien_type_zip_dangereux": "Dangerous ZIP link or type: {nom!r}",
        "archive_zip_trop_volumineux": "Decompressed ZIP content too large.",
        "archive_tar_trop_volumineux": "Decompressed TAR content too large.",
        "archive_lien_type_tar_dangereux": "Dangerous TAR link or type: {nom!r}",
        "archive_lien_hors_dossier": "Archive link outside the extraction folder: {nom!r} -> {cible!r}",
        "archive_membre_tar_illisible": "Unreadable TAR member: {nom!r}",
        "archive_format_inconnu": "Unknown archive format: {nom}",
        "archive_bundle_unique": "The archive must contain a single bundle folder.",
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
        "permutation_non_finalisee":
            "An unfinished swap remains: {marqueur}. "
            ".old backups kept; repair needed.",
        "permutation_preparation_interrompue":
            "Swap preparation interrupted: {marqueur}. "
            "No replacement allowed before verification.",
        "permutation_non_demarree": "Swap not started: {erreur}",
        "echec_remplacement": "Replacement failed ({erreur}). Reverting to the previous version.",
        "restauration_incomplete":
            "Incomplete restoration; no relaunch or further attempt. "
            "Keep {marqueur} and the .old backups. {echecs}",
        "maj_precedente_non_finalisee":
            "Previous update not finalized: backups and preparation kept.",
        "relance": "Relaunching: {commande}",
        "racine_controle_illisible": "Update interrupted: control root unreadable.",
        "racines_controle_multiples":
            "Update interrupted: multiple possible control roots.",
        "arret_version_en_place": "Stopping the current version…",
        "arret_echoue": "Update interrupted: the stop command failed.",
        "instance_encore_active": "Update interrupted: an instance is still active.",
        "version_precedente_intacte": "The previous version is intact: nothing was replaced.",
        "installe_dans": "Installed in {installe}",
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


def msg(cle: str, **valeurs) -> str:
    return runtime.traduire(LIBELLES, cle, **valeurs)


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
MAX_CIBLE_LIEN = 4096
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
VERSION_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
UPDATE_HOST_SUFFIXES = ("github.com", "githubusercontent.com")


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
    locale = _version_locale()
    if not version or _numeros(version) <= _numeros(locale):
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
        raise OSError(msg("empreinte_url_etrangere"))
    requete = urllib.request.Request(
        url, headers={"User-Agent": f"blink2video/{runtime.VERSION}"})
    with urllib.request.urlopen(requete, timeout=15) as reponse:
        finale = getattr(reponse, "geturl", lambda: url)()
        if not _url_mise_a_jour_autorisee(finale):
            raise OSError(msg("empreinte_redirection"))
        annoncee = reponse.headers.get("Content-Length")
        if annoncee:
            try:
                annoncee = int(annoncee)
            except ValueError as erreur:
                raise OSError(msg("empreinte_taille_http_invalide")) from erreur
            if annoncee < 1 or annoncee > MAX_CHECKSUM_BYTES:
                raise OSError(msg("empreinte_trop_volumineuse"))
        corps = reponse.read(MAX_CHECKSUM_BYTES + 1)
        if len(corps) > MAX_CHECKSUM_BYTES:
            raise OSError(msg("empreinte_trop_volumineuse"))

    try:
        lignes = [ligne.strip() for ligne in corps.decode("ascii").splitlines()
                  if ligne.strip()]
    except UnicodeDecodeError as erreur:
        raise OSError(msg("empreinte_non_ascii")) from erreur
    if len(lignes) != 1:
        raise OSError(msg("empreinte_ambigue"))
    champs = lignes[0].split()
    empreinte = _sha256_normalise(champs[0] if champs else "")
    if not empreinte:
        raise OSError(msg("empreinte_absente"))
    if len(champs) > 2 or (len(champs) == 2
                           and champs[1].lstrip("*") != nom_archive):
        raise OSError(msg("empreinte_archive_incorrecte"))
    return empreinte


def _empreinte_attendue(archive: dict) -> str:
    empreinte = _sha256_normalise(archive.get("sha256"))
    if empreinte:
        return empreinte
    checksum_url = str(archive.get("checksum_url") or "")
    if checksum_url:
        return _lire_empreinte(checksum_url, str(archive.get("nom") or ""))
    raise OSError(msg("empreinte_release_absente"))


def _nom_archive_sur(nom) -> str:
    nom = str(nom or "")
    if (not nom or len(nom) > 200 or "/" in nom or "\\" in nom
            or Path(nom).name != nom
            or not (nom.lower().endswith(".zip")
                    or nom.lower().endswith(".tar.gz"))):
        raise OSError(msg("nom_archive_impropre", nom=nom))
    return nom


def _telecharger(url: str, destination: Path, taille: int, sha256: str) -> None:
    """Rapatrie l'archive en publiant son avancement.

    Le même canal que le téléchargement des clips et l'assemblage : l'interface
    montre déjà cette barre, il n'y avait rien à inventer."""
    if not _url_release_officielle(url, destination.name):
        raise OSError(msg("archive_url_etrangere"))
    if not 1 <= taille <= MAX_ARCHIVE_BYTES:
        raise OSError(msg("archive_taille_invalide", taille=taille))
    sha256 = _sha256_normalise(sha256)
    if not sha256:
        raise OSError(msg("archive_empreinte_invalide"))

    requete = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream",
                      "User-Agent": f"blink2video/{runtime.VERSION}"})
    try:
        with urllib.request.urlopen(requete, timeout=60) as reponse:
            finale = getattr(reponse, "geturl", lambda: url)()
            if not _url_mise_a_jour_autorisee(finale):
                raise OSError(msg("archive_redirection"))
            annoncee = reponse.headers.get("Content-Length")
            if annoncee:
                try:
                    annoncee = int(annoncee)
                except ValueError as erreur:
                    raise OSError(msg("archive_taille_http_invalide")) from erreur
                if annoncee != taille:
                    raise OSError(
                        msg("archive_taille_http_inattendue", annoncee=annoncee, taille=taille))

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
                        raise OSError(msg("archive_depasse_taille"))
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
                raise OSError(msg("archive_tronquee", recu=recu, taille=taille))
            obtenue = hacheur.hexdigest()
            if obtenue != sha256:
                raise OSError(
                    msg("archive_empreinte_incorrecte", obtenue=obtenue, sha256=sha256))
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    print(msg("archive_recue", nom=destination.name,
              mo=destination.stat().st_size // (1024 * 1024)))


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
        raise OSError(msg("archive_chemin_dangereux", brut=brut))
    portable = brut.replace("\\", "/").rstrip("/")
    chemin_posix = PurePosixPath(portable)
    morceaux = portable.split("/")
    if (not portable or chemin_posix.is_absolute()
            or any(not morceau or morceau in (".", "..") for morceau in morceaux)):
        raise OSError(msg("archive_chemin_dangereux", brut=brut))
    for morceau in morceaux:
        base = morceau.split(".", 1)[0].upper()
        if (len(morceau) > 255 or ":" in morceau
                or morceau.endswith((" ", "."))
                or any(ord(caractere) < 32 for caractere in morceau)
                or base in _NOMS_WINDOWS_INTERDITS):
            raise OSError(msg("archive_nom_non_portable", brut=brut))
    cible = racine.joinpath(*morceaux).resolve()
    if not runtime.est_relatif_a(cible, racine):
        raise OSError(msg("archive_chemin_hors_dossier", brut=brut))
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
            raise OSError(msg("archive_collision_chemins", nom=nom))
        if courant != "dir":
            raise OSError(msg("archive_membre_duplique", nom=nom))


def _cible_lien_sure(morceaux: tuple, cible_lien: str) -> str:
    """Cible d'un lien symbolique de l'archive, acceptée seulement si elle est
    relative et reste dans le dossier d'extraction : la règle du filtre
    « data » de tarfile (Python 3.12).

    Les bundles PyInstaller publiés en contiennent : bibliothèques de Pillow
    sous Linux, framework Python sous macOS. Les refuser tous faisait échouer
    toute mise à jour sur ces deux systèmes (issue #21). Sous Windows, aucune
    archive publiée n'en contient et en créer demande un privilège : ils y
    restent refusés."""
    nom = "/".join(morceaux)
    if os.name == "nt":
        raise OSError(msg("archive_lien_type_zip_dangereux", nom=nom))
    brut = str(cible_lien or "")
    if (not brut or len(brut) > MAX_CIBLE_LIEN or "\x00" in brut
            or "\\" in brut or PurePosixPath(brut).is_absolute()):
        raise OSError(msg("archive_lien_hors_dossier", nom=nom, cible=brut))
    resolu = posixpath.normpath(posixpath.join(*morceaux[:-1], brut))
    if resolu in (".", "..") or resolu.startswith("../"):
        raise OSError(msg("archive_lien_hors_dossier", nom=nom, cible=brut))
    return brut


def _creer_liens(membres: list) -> None:
    """Liens posés en dernier, une fois dossiers et fichiers écrits : aucun
    chemin validé plus haut n'a pu en traverser un."""
    for _info, cible, genre, lien in membres:
        if genre == "lien":
            cible.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(lien, cible)


def _copier_exactement(source, destination: Path, taille: int) -> None:
    restant = taille
    with destination.open("xb") as sortie:
        while restant:
            bloc = source.read(min(262144, restant))
            if not bloc:
                raise OSError(msg("archive_membre_tronque", nom=destination.name))
            sortie.write(bloc)
            restant -= len(bloc)
        if source.read(1):
            raise OSError(msg("archive_membre_plus_long", nom=destination.name))


def _extraire_zip(archive: Path, racine: Path) -> None:
    with zipfile.ZipFile(archive) as zip_:
        infos = zip_.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            raise OSError(msg("archive_trop_de_membres"))
        registre = {}
        membres = []
        total = 0
        for info in infos:
            mode = (info.external_attr >> 16) & 0xFFFF
            type_mode = stat.S_IFMT(mode)
            dossier = info.is_dir()
            if info.flag_bits & 0x1:
                raise OSError(msg("archive_membre_zip_chiffre", nom=info.filename))
            lien = None
            if dossier:
                if type_mode not in (0, stat.S_IFDIR):
                    raise OSError(msg("archive_type_zip_dangereux", nom=info.filename))
                genre = "dir"
            elif type_mode == stat.S_IFLNK:
                # Un lien ZIP porte sa cible comme contenu du membre.
                if not 0 < info.file_size <= MAX_CIBLE_LIEN:
                    raise OSError(msg("archive_lien_type_zip_dangereux", nom=info.filename))
                genre = "lien"
                try:
                    lien = zip_.read(info).decode("utf-8")
                except UnicodeDecodeError:
                    raise OSError(msg("archive_lien_type_zip_dangereux",
                                      nom=info.filename)) from None
            else:
                if type_mode not in (0, stat.S_IFREG):
                    raise OSError(msg("archive_lien_type_zip_dangereux", nom=info.filename))
                genre = "file"
                total += info.file_size
                if info.file_size < 0 or total > MAX_EXTRACTED_BYTES:
                    raise OSError(msg("archive_zip_trop_volumineux"))
            cible, morceaux = _destination_archive(racine, info.filename)
            if genre == "lien":
                lien = _cible_lien_sure(morceaux, lien)
            # Un lien est une feuille : rien ne peut être rangé « sous » lui.
            _inscrire_destination(registre, morceaux, "file" if genre == "lien" else genre)
            membres.append((info, cible, genre, lien))

        for _, cible, genre, _lien in membres:
            if genre == "dir":
                cible.mkdir(parents=True, exist_ok=True)
        for info, cible, genre, _lien in membres:
            if genre != "file":
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            with zip_.open(info, "r") as source:
                _copier_exactement(source, cible, info.file_size)
        _creer_liens(membres)


def _extraire_tar(archive: Path, racine: Path) -> None:
    with tarfile.open(archive) as tar:
        infos = []
        for info in tar:
            infos.append(info)
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise OSError(msg("archive_trop_de_membres"))
        registre = {}
        membres = []
        total = 0
        for info in infos:
            lien = None
            if info.isdir():
                genre = "dir"
            elif info.isfile() and not getattr(info, "sparse", None):
                genre = "file"
                total += info.size
                if info.size < 0 or total > MAX_EXTRACTED_BYTES:
                    raise OSError(msg("archive_tar_trop_volumineux"))
            elif info.issym():
                genre = "lien"
            else:
                # Liens physiques, périphériques, FIFO : jamais dans un bundle.
                raise OSError(msg("archive_lien_type_tar_dangereux", nom=info.name))
            cible, morceaux = _destination_archive(racine, info.name)
            if genre == "lien":
                lien = _cible_lien_sure(morceaux, info.linkname)
            # Un lien est une feuille : rien ne peut être rangé « sous » lui.
            _inscrire_destination(registre, morceaux, "file" if genre == "lien" else genre)
            membres.append((info, cible, genre, lien))

        for _, cible, genre, _lien in membres:
            if genre == "dir":
                cible.mkdir(parents=True, exist_ok=True)
        for info, cible, genre, _lien in membres:
            if genre != "file":
                continue
            cible.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(info)
            if source is None:
                raise OSError(msg("archive_membre_tar_illisible", nom=info.name))
            with source:
                _copier_exactement(source, cible, info.size)
            # Pas de propriétaire, setuid/setgid ni mode arbitraire venant de
            # l'archive. Seul le caractère exécutable utile est conservé.
            cible.chmod(0o755 if info.mode & 0o111 else 0o644)
        _creer_liens(membres)


def _extraire(archive: Path, vers: Path) -> Path:
    """Déballe l'archive et rend le dossier du bundle qu'elle contenait."""
    vers.mkdir(parents=True, exist_ok=False)
    racine = vers.resolve()
    if archive.name.lower().endswith(".zip"):
        _extraire_zip(archive, racine)
    elif archive.name.lower().endswith(".tar.gz"):
        _extraire_tar(archive, racine)
    else:
        raise OSError(msg("archive_format_inconnu", nom=archive.name))
    # Les archives publiées contiennent un unique dossier « blink2video ».
    contenu = list(vers.iterdir())
    if (len(contenu) != 1 or not contenu[0].is_dir()
            or contenu[0].is_symlink()):
        raise OSError(msg("archive_bundle_unique"))
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
MARQUEUR_PERMUTATION = ".blink_maj_permutation.json"


class RestaurationIncomplete(RuntimeError):
    """La sauvegarde doit rester intacte jusqu'à une réparation explicite."""


@contextlib.contextmanager
def _reservation_installation(installe: Path):
    """Sérialise nettoyage et permutation, indépendamment du stockage.

    Le verrou empêche un nettoyage concurrent de franchir le contrôle du
    marqueur avant sa création. Le marqueur, lui, survit à un arrêt brutal.
    """
    with contextlib.ExitStack() as reservations:
        try:
            reservations.enter_context(runtime.verrou(
                "maj-installation", "mise à jour", attente=0, racine=installe))
        except (runtime.BusyError, OSError) as erreur:
            raise RestaurationIncomplete(
                "Installation non réservée ; aucun remplacement ni nettoyage "
                f"autorisé ({erreur}).") from erreur
        # Les erreurs du corps ne sont pas des échecs d'acquisition : les
        # laisser suivre leur propre retour arrière, sans les requalifier.
        yield


def _effacer_element_programme(chemin: Path) -> None:
    if chemin.is_dir() and not chemin.is_symlink():
        shutil.rmtree(chemin)
    else:
        chemin.unlink(missing_ok=True)


def _poser(source: Path, cible: Path) -> None:
    """Installe un fichier ou un dossier neuf à sa place définitive.

    Une copie, et non un déplacement : le programme qui exécute cette fonction
    est celui du dossier neuf, ses bibliothèques sont chargées depuis
    `_internal`, et Windows refuse de renommer un dossier dont un fichier est
    mappé en mémoire. Copier ne demande rien d'exclusif sur la source. Le
    dossier temporaire reste derrière, et le ménage se fait au passage
    suivant."""
    if source.is_dir():
        # symlinks=True : les liens internes du bundle (validés à
        # l'extraction) restent des liens au lieu d'être dupliqués ; la
        # structure du framework Python sous macOS en dépend.
        shutil.copytree(source, cible, symlinks=True)
    else:
        shutil.copy2(source, cible)
        if os.name != "nt":
            cible.chmod(0o755)


def _permuter(neuf: Path, installe: Path) -> bool:
    """Met les fichiers neufs à la place des anciens, ou remet tout en l'état.

    Les anciens sont écartés avant d'être supprimés : si une copie échoue à
    mi-chemin, on sait revenir en arrière, ce qu'un effacement préalable
    rendrait impossible."""
    with _reservation_installation(installe):
        return _permuter_reserve(neuf, installe)


def _permuter_reserve(neuf: Path, installe: Path) -> bool:
    marqueur = installe / MARQUEUR_PERMUTATION
    try:
        # Création exclusive AVANT la première mutation : un arrêt brutal
        # laisse aussi le garde-fou empêchant de purger la seule sauvegarde.
        with marqueur.open("x", encoding="utf-8") as fichier:
            json.dump({"elements": list(CONTENU_DU_PROGRAMME)}, fichier)
    except FileExistsError as erreur:
        raise RestaurationIncomplete(
            msg("permutation_non_finalisee", marqueur=marqueur)) from erreur
    except OSError as erreur:
        if marqueur.exists():
            raise RestaurationIncomplete(
                msg("permutation_preparation_interrompue", marqueur=marqueur)) from erreur
        print(msg("permutation_non_demarree", erreur=erreur), flush=True)
        return False

    touches = []
    try:
        for nom in CONTENU_DU_PROGRAMME:
            source = neuf / nom
            if not source.exists():
                continue
            ancien = installe / nom
            retire = None
            if ancien.exists():
                retire = installe / f"{nom}.ancien"
                _effacer_element_programme(retire)
                os.replace(ancien, retire)
            touches.append((retire, ancien))
            _poser(source, installe / nom)
        marqueur.unlink()
        return True
    except OSError as erreur:
        print(msg("echec_remplacement", erreur=erreur))
        echecs = []
        for retire, ancien in reversed(touches):
            try:
                # Supprimer aussi un élément neuf qui n'existait pas avant.
                _effacer_element_programme(ancien)
                if retire is not None:
                    os.replace(retire, ancien)
            except OSError as restauration:
                echecs.append(f"{ancien.name}: {restauration}")
        if not echecs:
            try:
                marqueur.unlink()
            except OSError as restauration:
                echecs.append(str(restauration))
        if echecs:
            raise RestaurationIncomplete(
                msg("restauration_incomplete", marqueur=marqueur,
                    echecs=" ; ".join(echecs))) from erreur
        return False


def _nettoyer(installe: Path) -> None:
    """Efface les restes d'une mise à jour précédente.

    Ce ménage ne peut pas se faire à la fin de l'opération : le programme qui
    permute tourne depuis ``update``, et sous Windows un exécutable ne peut pas
    effacer le dossier dont il est issu. On le fait donc au début de la suivante,
    quand plus personne n'y tient."""
    with _reservation_installation(installe):
        _nettoyer_reserve(installe)


def _nettoyer_reserve(installe: Path) -> None:
    if (installe / MARQUEUR_PERMUTATION).exists():
        raise RestaurationIncomplete(msg("maj_precedente_non_finalisee"))
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
    print(msg("relance", commande=" ".join(commande)), flush=True)
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
    runtime.demarrer(commande, cwd=str(installe),
                     env=env,
                     stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=(os.name != "nt"))


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


def _finaliser(cible: Path) -> int:
    """Second temps, exécuté par la nouvelle version depuis son dossier
    temporaire : arrêter, remplacer, relancer."""
    installe = cible.resolve()
    neuf = Path(sys.executable).resolve().parent if runtime.frozen() \
        else Path(__file__).resolve().parent

    # Ce qui tourne, noté avant l'arrêt : c'est ce qu'il faudra relancer.
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

    # L'ancienne version de stop ne connaît pas BLINK_CONTROL_HOME. Lui
    # transmettre aussi cette racine via BLINK_HOME évite qu'elle cherche
    # les fiches dans les données redirigées, puis annonce « rien ne tourne ».
    env_arret = dict(os.environ, BLINK_HOME=str(runtime._dossier_controle()))

    print(msg("arret_version_en_place"), flush=True)
    arret = runtime.lancer(_ligne(installe, "stop"), cwd=str(installe),
                           env=env_arret, stdin=subprocess.DEVNULL, check=False)
    if arret.returncode != 0:
        print(msg("arret_echoue"), flush=True)
        return 1

    # Les fichiers restent tenus quelques instants après la mort du processus,
    # le temps que le système referme ses poignées.
    for essai in range(20):
        # lire_instances garde aussi les fiches dont seul un enfant ou un
        # ffmpeg survit : la mort du superviseur ne suffit pas.
        vivants = runtime.lire_instances()
        if not vivants:
            break
        time.sleep(1)
    else:
        print(msg("instance_encore_active"), flush=True)
        return 1

    # Depuis les sources, « git pull » a déjà mis les fichiers en place : il n'y
    # a rien à permuter, seulement à relancer.
    if neuf != installe:
        for essai in range(15):
            try:
                reussi = _permuter(neuf, installe)
            except RestaurationIncomplete as erreur:
                print(str(erreur), flush=True)
                return 1
            if reussi:
                break
            time.sleep(2)
        else:
            print(msg("version_precedente_intacte"), flush=True)
            for verbes in compositions:
                _relancer(installe, verbes)
            return 1

    print(msg("installe_dans", installe=installe), flush=True)
    for verbes in compositions:
        _relancer(installe, verbes)
    return 0


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
