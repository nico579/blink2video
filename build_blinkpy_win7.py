#!/usr/bin/env python3
"""Fabrique la roue blinkpy rétroportée utilisée par le bundle Windows 7.

Le code de blinkpy 0.25.9 fonctionne sous Python 3.8, mais sa roue déclare
Python 3.10 et des versions d'aiohttp/requests qui n'existent pas pour 3.8.
Cette recette ne modifie aucun fichier Python : elle vérifie octet par octet la
roue publiée, corrige uniquement ses métadonnées, puis recalcule RECORD.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


UPSTREAM_VERSION = "0.25.9"
WIN7_VERSION = "0.25.9+win7.1"
UPSTREAM_WHEEL = "blinkpy-0.25.9-py3-none-any.whl"
WIN7_WHEEL = "blinkpy-0.25.9+win7.1-py3-none-any.whl"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/9f/47/"
    "309a4b53b2178d361f4fd0bbd2ebb01af0d0acd4584ba490c2f462a1a808/"
    + UPSTREAM_WHEEL
)
UPSTREAM_SHA256 = (
    "ef2d987f291d1f892f8a998686088a1a722c63a147b6c4588b41a639977a34c8"
)

# Bornes réellement installables avec le dernier CPython officiel pour Win7.
# Les autres dépendances déclarées par blinkpy 0.25.9 sont déjà compatibles.
METADATA_REPLACEMENTS = {
    "Version: 0.25.9": "Version: 0.25.9+win7.1",
    "Requires-Python: >=3.10.0": "Requires-Python: >=3.8,<3.9",
    "Requires-Dist: requests>=2.34.2":
        "Requires-Dist: requests>=2.32.4,<2.32.5",
    "Requires-Dist: aiohttp>=3.14.1":
        "Requires-Dist: aiohttp>=3.10.11,<3.11",
}


def _sha256(fichier: Path) -> str:
    empreinte = hashlib.sha256()
    with fichier.open("rb") as flux:
        for bloc in iter(lambda: flux.read(1024 * 1024), b""):
            empreinte.update(bloc)
    return empreinte.hexdigest()


def _telecharger(destination: Path) -> None:
    requete = urllib.request.Request(
        UPSTREAM_URL,
        headers={"User-Agent": "blink2video windows7 build"},
    )
    with urllib.request.urlopen(requete, timeout=60) as reponse, \
            destination.open("wb") as sortie:
        shutil.copyfileobj(reponse, sortie)
    obtenue = _sha256(destination)
    if obtenue != UPSTREAM_SHA256:
        destination.unlink(missing_ok=True)
        raise SystemExit(
            "La roue blinkpy reçue ne correspond pas à la publication attendue "
            f"(SHA-256 {obtenue}, attendu {UPSTREAM_SHA256})."
        )


def _remplacer_metadata(fichier: Path) -> None:
    lignes = fichier.read_text(encoding="utf-8").splitlines()
    vues = set()
    nouvelles = []
    for ligne in lignes:
        if ligne in METADATA_REPLACEMENTS:
            vues.add(ligne)
            ligne = METADATA_REPLACEMENTS[ligne]
        nouvelles.append(ligne)
    manquantes = set(METADATA_REPLACEMENTS) - vues
    if manquantes:
        raise SystemExit(
            "Métadonnées blinkpy inattendues, rétroportage interrompu : "
            + ", ".join(sorted(manquantes))
        )
    nouvelles.insert(
        next(i for i, ligne in enumerate(nouvelles)
             if ligne.startswith("Summary:")) + 1,
        "X-Blink2Video-Windows7-Backport: metadata-only",
    )
    fichier.write_text("\n".join(nouvelles) + "\n", encoding="utf-8")


def _record(racine: Path, fichier: Path) -> None:
    tampon = io.StringIO(newline="")
    sortie = csv.writer(tampon, lineterminator="\n")
    for chemin in sorted(racine.rglob("*")):
        if not chemin.is_file() or chemin == fichier:
            continue
        relatif = chemin.relative_to(racine).as_posix()
        contenu = chemin.read_bytes()
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(contenu).digest()
        ).rstrip(b"=").decode("ascii")
        sortie.writerow((relatif, f"sha256={digest}", len(contenu)))
    sortie.writerow((fichier.relative_to(racine).as_posix(), "", ""))
    with fichier.open("w", encoding="utf-8", newline="") as flux:
        flux.write(tampon.getvalue())


def _emballer(racine: Path, destination: Path) -> None:
    temporaire = destination.with_suffix(destination.suffix + ".tmp")
    temporaire.unlink(missing_ok=True)
    with zipfile.ZipFile(temporaire, "w", zipfile.ZIP_DEFLATED) as archive:
        for chemin in sorted(racine.rglob("*")):
            if not chemin.is_file():
                continue
            # Date et droits fixes : deux constructions rendent la même roue.
            info = zipfile.ZipInfo(
                chemin.relative_to(racine).as_posix(),
                date_time=(2020, 1, 1, 0, 0, 0),
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, chemin.read_bytes())
    temporaire.replace(destination)


def fabriquer(dossier_sortie: Path) -> Path:
    """Télécharge, authentifie et reconditionne blinkpy pour Python 3.8."""
    dossier_sortie.mkdir(parents=True, exist_ok=True)
    destination = dossier_sortie / WIN7_WHEEL
    with tempfile.TemporaryDirectory(prefix="blinkpy_win7_") as temporaire:
        racine = Path(temporaire)
        amont = racine / UPSTREAM_WHEEL
        _telecharger(amont)
        extrait = racine / "extrait"
        with zipfile.ZipFile(amont) as archive:
            archive.extractall(extrait)

        ancien = extrait / f"blinkpy-{UPSTREAM_VERSION}.dist-info"
        nouveau = extrait / f"blinkpy-{WIN7_VERSION}.dist-info"
        if not ancien.is_dir():
            raise SystemExit(f"Dossier de métadonnées absent : {ancien.name}")
        ancien.rename(nouveau)
        _remplacer_metadata(nouveau / "METADATA")
        _record(extrait, nouveau / "RECORD")
        _emballer(extrait, destination)

    print(f"Roue Windows 7 : {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dossier", type=Path, help="dossier où écrire la roue")
    args = parser.parse_args()
    fabriquer(args.dossier.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
