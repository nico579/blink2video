#!/usr/bin/env python3
"""Vérifie les garde-fous statiques du bundle expérimental Windows 7."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pefile


DLL_INTERDITE = "api-ms-win-core-path-l1-1-0.dll"
AMD64 = 0x8664
RUNTIME_PYTHON = re.compile(r"python3\d+\.dll$", re.I)


def _lire_pe(chemin: Path) -> tuple:
    try:
        pe = pefile.PE(str(chemin), fast_load=True)
    except pefile.PEFormatError:
        return None, set()
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ]
        )
        resultat = set()
        for attribut in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entree in getattr(pe, attribut, ()):
                resultat.add(entree.dll.decode("ascii", "replace").lower())
        return int(pe.FILE_HEADER.Machine), resultat
    finally:
        pe.close()


def verifier(bundle: Path) -> list:
    erreurs = []
    executable = bundle / "blink2video.exe"
    if not executable.is_file():
        erreurs.append(f"exécutable absent : {executable}")

    python38 = list(bundle.rglob("python38.dll"))
    if len(python38) != 1:
        erreurs.append(
            f"python38.dll attendu une fois, trouvé {len(python38)} fois"
        )
    autres_python = [f for f in bundle.rglob("*.dll")
                      if RUNTIME_PYTHON.match(f.name)
                      and f.name.lower() != "python38.dll"]
    if autres_python:
        erreurs.append(
            "runtime Python inattendu : "
            + ", ".join(str(f.relative_to(bundle)) for f in autres_python)
        )

    binaires = [f for f in bundle.rglob("*")
                if f.is_file() and f.suffix.lower() in (".exe", ".dll", ".pyd")]
    fautifs = []
    mauvaises_architectures = []
    for binaire in binaires:
        machine, imports = _lire_pe(binaire)
        if machine is None:
            continue
        if machine != AMD64:
            mauvaises_architectures.append(str(binaire.relative_to(bundle)))
        if DLL_INTERDITE in imports:
            fautifs.append(str(binaire.relative_to(bundle)))
    if mauvaises_architectures:
        erreurs.append(
            "binaire PE non x86-64 : " + ", ".join(mauvaises_architectures)
        )
    if fautifs:
        erreurs.append(
            f"import interdit {DLL_INTERDITE} : " + ", ".join(fautifs)
        )

    if not (bundle / "_internal" / "windows7-build.txt").is_file():
        erreurs.append("marqueur windows7-build.txt absent du bundle")
    if not erreurs:
        print(
            f"OK : Python 3.8, marqueur Win7 et {len(binaires)} binaires PE "
            f"sans {DLL_INTERDITE}."
        )
    return erreurs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    erreurs = verifier(args.bundle.resolve())
    for erreur in erreurs:
        print(f"ÉCHEC : {erreur}")
    return 1 if erreurs else 0


if __name__ == "__main__":
    raise SystemExit(main())
