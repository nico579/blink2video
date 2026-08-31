"""Construit ``dist-xr-tester/Tester-XR.exe`` pour un diagnostic utilisateur."""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

import build
import runtime
from build_support import ecrire_version_info


BASE_DIR = Path(__file__).resolve().parent
VENV = BASE_DIR / "build_venv"
WORK = BASE_DIR / "build-xr-tester"
DIST = BASE_DIR / "dist-xr-tester"
SPEC = BASE_DIR / "xr_tester.spec"
README = BASE_DIR / "README-XR-test.txt"
PACKAGE_REVISION = "r2"


def preparer_version_info() -> Path:
    """Crée la ressource PE du testeur, même depuis un checkout propre."""
    return ecrire_version_info(
        runtime.VERSION,
        BASE_DIR / ".version_info.txt",
        description="blink2video - diagnostic du stockage local XR",
        internal_name="Tester-XR",
        original_filename="Tester-XR.exe",
        product_name="blink2video Tester-XR",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--propre",
        action="store_true",
        help="supprimer uniquement les sorties précédentes du testeur",
    )
    args = parser.parse_args()

    if sys.platform != "win32":
        raise SystemExit("Le testeur direct est actuellement destiné à Windows.")

    preparer_version_info()

    if args.propre:
        for path in (WORK, DIST):
            resolved = path.resolve()
            if resolved.parent != BASE_DIR.resolve():
                raise SystemExit(f"Refus de supprimer un chemin inattendu : {resolved}")
            if resolved.exists():
                shutil.rmtree(resolved)

    python = build._python(VENV)
    if not python.exists():
        build.executer(
            [sys.executable, "-m", "venv", str(VENV)],
            "création de l'environnement de construction",
        )
    build.executer(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        "mise à jour de pip",
    )
    build.executer(
        [str(python), "-m", "pip", "install", "--quiet",
         "-r", str(build.REQUIREMENTS)],
        "installation des dépendances",
    )
    build.executer(
        [
            str(python), "-m", "PyInstaller", "--noconfirm", "--clean",
            "--distpath", str(DIST), "--workpath", str(WORK), str(SPEC),
        ],
        "construction du testeur XR",
    )

    executable = DIST / "Tester-XR.exe"
    if not executable.is_file():
        raise SystemExit(f"Testeur introuvable : {executable}")
    if not README.is_file():
        raise SystemExit(f"Instructions introuvables : {README}")

    package = DIST / (
        f"Tester-XR-blink2video-{runtime.VERSION}-{PACKAGE_REVISION}-windows-x64.zip"
    )
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, executable.name)
        archive.write(README, README.name)

    print(f"\nTesteur construit : {executable}")
    print(f"Paquet transmissible : {package}")
    print("À placer dans le même dossier que blink2video.exe, puis double-cliquer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
