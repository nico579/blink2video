#!/usr/bin/env python3
"""Vérifie les garde-fous statiques du bundle legacy Windows 7."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pefile


DLL_INTERDITE = "api-ms-win-core-path-l1-1-0.dll"
# Liste volontairement étroite : ce contrôle ne certifie pas toutes les API
# Windows. Les tables d'import PE (y compris delay-load) sont inspectées, pas
# les chaînes du binaire : une recherche optionnelle par GetProcAddress avec
# repli Win7 ne doit pas être confondue avec une dépendance obligatoire.
# ProcessPrng : https://github.com/pyca/cryptography/issues/10944
# Les autres API nécessitent Windows 8, sauf les descriptions de threads
# (Windows 10 1607). Références Microsoft, rubriques "Requirements" :
# https://learn.microsoft.com/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemtimepreciseasfiletime
# https://learn.microsoft.com/windows/win32/api/ioapiset/nf-ioapiset-getoverlappedresultex
# https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfile2
# https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-waitonaddress
# https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-wakebyaddresssingle
# https://learn.microsoft.com/windows/win32/api/synchapi/nf-synchapi-wakebyaddressall
# https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-setthreaddescription
# https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getthreaddescription
# https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getcurrentthreadstacklimits
API_POST_WIN7 = frozenset({
    "ProcessPrng",
    "GetSystemTimePreciseAsFileTime",
    "GetOverlappedResultEx",
    "CreateFile2",
    "WaitOnAddress",
    "WakeByAddressSingle",
    "WakeByAddressAll",
    "SetThreadDescription",
    "GetThreadDescription",
    "GetCurrentThreadStackLimits",
})
AMD64 = 0x8664
RUNTIME_PYTHON = re.compile(r"python3\d+\.dll$", re.I)


def _lire_pe(chemin: Path) -> tuple:
    """Conserve l'interface historique (architecture, noms des DLL)."""
    machine, dlls, _ = _lire_pe_details(chemin)
    return machine, dlls


def _lire_pe_details(chemin: Path) -> tuple:
    try:
        pe = pefile.PE(str(chemin), fast_load=True)
    except pefile.PEFormatError:
        return None, set(), set()
    try:
        pe.parse_data_directories(
            directories=[
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
            ]
        )
        resultat = set()
        symboles = set()
        for attribut in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entree in getattr(pe, attribut, ()):
                dll = entree.dll.decode("ascii", "replace").lower()
                resultat.add(dll)
                for symbole in entree.imports:
                    # Les imports par ordinal n'ont pas de nom. Ils ne sont
                    # pas résolus ici, faute de DLL de référence Windows 7.
                    if symbole.name is not None:
                        symboles.add((
                            dll, symbole.name.decode("ascii", "replace")
                        ))
        return int(pe.FILE_HEADER.Machine), resultat, symboles
    finally:
        pe.close()


def _api_post_win7(dll: str, symbole: str) -> bool:
    # Ne pas accuser une fonction homonyme fournie par une DLL de l'application.
    systeme = dll in {"kernel32.dll", "kernelbase.dll", "bcryptprimitives.dll"}
    systeme = systeme or dll.startswith(("api-ms-win-", "ext-ms-win-"))
    return systeme and symbole in API_POST_WIN7


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
        machine, imports, symboles = _lire_pe_details(binaire)
        if machine is None:
            continue
        if machine != AMD64:
            mauvaises_architectures.append(str(binaire.relative_to(bundle)))
        if DLL_INTERDITE in imports:
            fautifs.append(str(binaire.relative_to(bundle)))
        for dll, symbole in sorted(symboles):
            if _api_post_win7(dll, symbole):
                erreurs.append(
                    f"API post-Windows 7 importée : "
                    f"{binaire.relative_to(bundle)} : {dll}!{symbole}"
                )
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
    racines = bundle / "_internal" / "certifi" / "cacert.pem"
    if not racines.is_file() or racines.stat().st_size < 100_000:
        erreurs.append("magasin de certificats certifi absent ou incomplet")
    if not erreurs:
        print(
            f"OK : Python 3.8, marqueur Win7, racines TLS et {len(binaires)} "
            f"binaires PE sans {DLL_INTERDITE} ni API post-Win7 de la liste."
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
